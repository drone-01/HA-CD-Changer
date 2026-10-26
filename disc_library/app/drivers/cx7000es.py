"""Sony BDP-CX7000ES driver, over the network.

The changer exposes Sony's Custom Installation (CIS) protocol as a UPnP service on port 8888:
one SOAP action, X_CIS_Command, whose CISDATA argument is a protocol frame written as hex.
Frames follow Sony's "RS-232C/Network Control Protocol Specification" v0.97:

    02  LEN  PDC  CMD  DATA...  SUM

LEN counts PDC+CMD+DATA, SUM is the two's complement of the low byte of LEN+PDC+CMD+DATA,
PDC is 0x80/0x81/0x82 for the unit's command set BD1/BD2/BD3 (replies use 0x88/0x89/0x8A),
numbers are BCD, and plain commands answer FD (ACK) or FE (NACK). See docs/cx7000es-control.md.

Starting from standby takes minutes, so a play request on a sleeping changer powers it on and
remembers the disc; the status poll sends the play command once the changer is up.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from ..config import ChangerConfig
from .base import ChangerDriver, ChangerStatus

log = logging.getLogger(__name__)

SOAP_ACTION = '"urn:schemas-sony-com:service:X_CIS:1#X_CIS_Command"'
ENVELOPE = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            '<u:X_CIS_Command xmlns:u="urn:schemas-sony-com:service:X_CIS:1"><CISDATA>{}</CISDATA>'
            '</u:X_CIS_Command></s:Body></s:Envelope>')
ACK, NACK = 0xFD, 0xFE

# commands
PLAY, PAUSE_KEY, STOP, NEXT, PREVIOUS, ONE_ALL = 0x12, 0x13, 0x14, 0x11, 0x10, 0x1E
DISC_DIRECT, PAUSE_SET, POWER_SET = 0x4A, 0x57, 0x60
STATUS_REQ, DISC_DATA_REQ, DISC_INFO_REQ, MODEL_REQ, IP_DISC_EXIST = 0x82, 0x8A, 0x8D, 0xA0, 0xCC

STATES = {0x00: "off", 0x10: "stopped", 0x11: "playing", 0x12: "paused", 0x20: "loading",
          0x30: "door_open", 0xEE: "error"}
LOAD_GRACE_SEC = 120     # after a play request, "stopped" means "still loading" until playback is seen
START_TIMEOUT_SEC = 420  # give up waiting for the changer to come out of standby


class ChangerCommandError(RuntimeError):
    pass


def bcd(n: int, width: int = 2) -> bytes:
    """388 -> b'\\x03\\x88' (width bytes, BCD)."""
    s = str(int(n)).zfill(width * 2)
    return bytes(int(s[i:i + 2], 16) for i in range(0, len(s), 2))


def from_bcd(b: bytes) -> int | None:
    h = b.hex()
    if h == "ff" * len(b) or not h.isdigit():
        return None
    return int(h)


def frame(pdc: int, cmd: int, data: bytes = b"") -> bytes:
    body = bytes([pdc, cmd]) + data
    length = len(body)
    return bytes([0x02, length]) + body + bytes([(0x100 - ((length + sum(body)) & 0xFF)) & 0xFF])


def parse(reply: bytes) -> tuple[int, int, bytes] | None:
    """(pdc, cmd, data) of a data reply, or None for ACK/NACK/garbage."""
    if len(reply) < 5 or reply[0] != 0x02:
        return None
    length = reply[1]
    body = reply[2:2 + length]
    if len(body) != length or len(reply) < 3 + length:
        return None
    if (length + sum(body) + reply[2 + length]) & 0xFF:
        log.debug("checksum mismatch in %s", reply.hex())
        return None
    return body[0], body[1], body[2:]


def parse_all(reply: bytes) -> list[tuple[int, int, bytes]]:
    """Every data frame in a reply (some requests, like the slot bitmap, answer with several frames)."""
    out, i = [], 0
    while i + 4 < len(reply):
        if reply[i] != 0x02:
            i += 1
            continue
        length = reply[i + 1]
        p = parse(reply[i:i + 3 + length])
        if p:
            out.append(p)
            i += 3 + length
        else:
            i += 1
    return out


class Cx7000esDriver(ChangerDriver):
    def __init__(self, cfg: ChangerConfig, client_factory=None, clock=time.monotonic):
        super().__init__(cfg)
        self.port = cfg.port or 8888
        self.pdc = 0x80 + (max(1, min(3, getattr(cfg, "command_set", 1) or 1)) - 1)
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=httpx.Timeout(8, connect=4)))
        self.clock = clock
        self._client: httpx.AsyncClient | None = None
        self._pending: tuple[int, int] | None = None   # (slot, track) waiting for power-up
        self._pending_since: float | None = None
        self._expect_until = 0.0                        # load grace window
        self._seen_play = False
        self._last_track: int | None = None             # last track of the loaded disc (CDs)
        self._track_seen: int | None = None
        self._finished = False
        self._slot: int | None = None

    # --- transport -----------------------------------------------------------------
    async def connect(self) -> None:
        if not self.cfg.host:
            log.warning("%s: no host configured", self.name)
            return
        self._client = self.client_factory()
        try:
            model = await self.model_name()
            log.info("%s: %s at %s:%s (command set BD%s)", self.name, model, self.cfg.host, self.port, self.pdc - 0x7F)
        except (httpx.HTTPError, ChangerCommandError) as exc:
            log.warning("%s: not answering yet at %s:%s (%s)", self.name, self.cfg.host, self.port, exc)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _raw(self, payload: bytes) -> bytes:
        if not self.cfg.host:
            raise ChangerCommandError(f"{self.name} has no network address set.")
        if self._client is None:
            self._client = self.client_factory()
        r = await self._client.post(
            f"http://{self.cfg.host}:{self.port}/upnp/control/CIS",
            content=ENVELOPE.format(payload.hex().upper()).encode(),
            headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": SOAP_ACTION},
        )
        if r.status_code != 200:
            raise ChangerCommandError(f"{self.name} refused the command (HTTP {r.status_code}).")
        m = re.search(r"<Res_CISDATA>\s*([0-9a-fA-F]*)\s*</Res_CISDATA>", r.text)
        if not m:
            raise ChangerCommandError(f"{self.name} gave an answer I couldn't read.")
        return bytes.fromhex(m.group(1)) if m.group(1) else b""

    async def _command(self, cmd: int, data: bytes = b"") -> None:
        reply = await self._raw(frame(self.pdc, cmd, data))
        if reply[:1] == bytes([NACK]):
            raise ChangerCommandError(f"{self.name} said no to that right now.")
        p = parse(reply)
        if p and p[1] == 0x0E:
            raise ChangerCommandError(f"{self.name} can't do that in its current state.")
        if p and p[1] == 0x0F:
            raise ChangerCommandError(f"{self.name} doesn't know that command.")

    async def _request(self, cmd: int, data: bytes = b"") -> bytes:
        reply = await self._raw(frame(self.pdc, cmd, data))
        p = parse(reply)
        if not p or p[1] != cmd:
            raise ChangerCommandError(f"{self.name} didn't answer the request.")
        return p[2]

    # --- queries --------------------------------------------------------------------------
    async def model_name(self) -> str:
        return (await self._request(MODEL_REQ)).rstrip(b"\x00").decode("ascii", "replace")

    async def raw_status(self) -> dict:
        d = await self._request(STATUS_REQ)
        return {"disc": from_bcd(d[0:2]), "title": from_bcd(d[2:4]), "track": from_bcd(d[4:6]),
                "state": STATES.get(d[6], "unknown"), "mode": d[7] if len(d) > 7 else 0}

    async def disc_data(self, slot: int = 0) -> dict:
        d = await self._request(DISC_DATA_REQ, bcd(slot))
        kinds = {0x00: "none", 0x01: "cd", 0x03: "dvd", 0x05: "bd"}
        return {"disc": from_bcd(d[0:2]), "type": kinds.get(d[2], "other"),
                "first_track": from_bcd(d[3:5]), "last_track": from_bcd(d[5:7])}

    async def disc_title(self) -> str | None:
        """Album title / disc name of the disc in the drive (from the changer's own lookup)."""
        d = await self._request(DISC_INFO_REQ, bcd(0) + bytes([0x00]) + bcd(0))
        text = d[7:].split(b"\x00")[0].decode("utf-8", "replace").strip() if len(d) > 7 else ""
        return text or None

    async def occupied_slots(self) -> list[int]:
        """Slots the changer reports as holding a disc (for the slot check).

        Over the network the request names a packet (0-4, one per 80 slots); the answer repeats the
        packet number followed by ten bitmap bytes, bit 0 = the lowest slot.
        """
        slots: list[int] = []
        for packet in range(5):
            reply = await self._raw(frame(self.pdc, IP_DISC_EXIST, bytes([packet])))
            log.info("%s: slot bitmap packet %s reply %s", self.name, packet, reply.hex())
            frames = [f for f in parse_all(reply) if f[1] == IP_DISC_EXIST and len(f[2]) >= 11]
            if not frames:
                p = parse(reply)
                if p and p[1] == 0x0E:
                    raise ChangerCommandError(f"{self.name} can't report its slots right now (is it busy loading?).")
                raise ChangerCommandError(f"{self.name} didn't report its slots.")
            for _, _, data in frames:
                base = data[0] * 80 if data[0] <= 4 else packet * 80
                for i, byte in enumerate(data[1:11]):
                    for bit in range(8):
                        if byte & (1 << bit):
                            slots.append(base + i * 8 + bit + 1)
            await asyncio.sleep(0.15)
        return sorted(set(slots))

    # --- control --------------------------------------------------------------------------
    async def power(self, on: bool) -> None:
        await self._command(POWER_SET, bytes([1 if on else 0]))

    async def load_and_play(self, slot: int, track: int = 1, track_count: int | None = None) -> None:
        self._slot, self._finished, self._seen_play, self._track_seen = slot, False, False, None
        self._last_track = track_count
        st = await self.raw_status()
        if st["state"] == "off":
            await self.power(True)
            self._pending, self._pending_since = (slot, track), self.clock()
            log.info("%s: waking up to play slot %s", self.name, slot)
            return
        await self._play_disc(slot, track, st.get("mode", 0))

    async def _play_disc(self, slot: int, track: int, mode: int = 0) -> None:
        if mode & 0x40:  # "all discs" mode would roll on into the next slot: back to one disc
            await self._command(ONE_ALL)
        await self._command(DISC_DIRECT, bcd(slot) + bcd(max(1, track)) + bcd(0) + b"\x00")
        self._pending = None
        self._expect_until = self.clock() + LOAD_GRACE_SEC

    async def pause(self) -> None:
        await self._command(PAUSE_SET, b"\x01")

    async def resume(self) -> None:
        await self._command(PLAY)

    async def stop(self) -> None:
        self._pending = None
        self._expect_until = 0.0
        await self._command(STOP)

    async def next_track(self) -> None:
        await self._command(NEXT)

    async def previous_track(self) -> None:
        await self._command(PREVIOUS)

    async def status(self) -> ChangerStatus:
        try:
            st = await self.raw_status()
        except (httpx.HTTPError, ChangerCommandError) as exc:
            return ChangerStatus(online=False, error=str(exc) or type(exc).__name__)
        now = self.clock()
        state = st["state"]
        if self._pending:
            if state in ("stopped", "playing", "paused"):
                slot, track = self._pending
                try:
                    await self._play_disc(slot, track, st.get("mode", 0))
                except (httpx.HTTPError, ChangerCommandError) as exc:
                    log.info("%s: not ready to play yet (%s)", self.name, exc)
                state = "loading"
            elif self._pending_since and now - self._pending_since > START_TIMEOUT_SEC:
                self._pending = None
                return ChangerStatus(online=True, power=False, state="stopped",
                                     error=f"{self.name} didn't wake up.")
            else:
                return ChangerStatus(online=True, power=state != "off", state="loading", slot=self._slot)
        if state == "playing":
            if not self._seen_play:
                self._seen_play = True
                if self._last_track is None and st["disc"]:
                    try:
                        self._last_track = (await self.disc_data(0)).get("last_track")
                    except (httpx.HTTPError, ChangerCommandError):
                        pass
            if st["track"]:
                self._track_seen = st["track"]
        if state in ("loading", "door_open"):
            state = "loading"
        elif state == "stopped" and not self._seen_play and now < self._expect_until:
            state = "loading"
        elif state == "stopped" and self._seen_play and self._last_track and self._track_seen == self._last_track:
            self._finished = True
        elif state in ("off", "error", "unknown"):
            state = "stopped"
        return ChangerStatus(
            online=True, power=st["state"] != "off", state=state, slot=st["disc"] or self._slot,
            track=st["track"], disc_finished=self._finished,
            error="The changer reports an error." if st["state"] == "error" else None)
