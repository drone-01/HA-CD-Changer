"""The CX7000ES driver against a fake changer that speaks the UPnP CIS protocol."""

import asyncio
import re

import httpx
import pytest

from app.config import ChangerConfig
from app.drivers.cx7000es import Cx7000esDriver, ChangerCommandError, bcd, frame, from_bcd, parse


def run(coro):
    return asyncio.run(coro)


def test_frames_match_sonys_examples():
    assert frame(0x80, 0x60, b"\x01").hex() == "020380600" + "11c"          # POWER ON
    assert frame(0x80, 0xA0).hex() == "020280a0de"                          # MODEL_NAME_REQ
    assert frame(0x80, 0x82).hex() == "02028082fc"                          # STATUS_DATA_REQ
    assert frame(0x80, 0x4A, bcd(2) + bcd(1) + bcd(10) + b"\x00").hex() == "0209804a0002000100100" + "01a"
    assert bcd(388) == b"\x03\x88" and from_bcd(b"\x03\x88") == 388 and from_bcd(b"\xff\xff") is None
    assert parse(bytes.fromhex("020a888203880001000311004c")) == (0x88, 0x82, bytes.fromhex("0388000100031100"))


class FakeChanger:
    """Enough of the unit to exercise the driver."""

    def __init__(self, power=True):
        self.state = 0x10 if power else 0x00
        self.disc, self.track, self.last = 0, 0, 3
        self.sent = []
        self.boot_polls = 0

    def reply(self, pdc, cmd, data=b""):
        body = bytes([pdc + 8, cmd]) + data
        return bytes([2, len(body)]) + body + bytes([(0x100 - ((len(body) + sum(body)) & 0xFF)) & 0xFF])

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/upnp/control/CIS"
        assert "X_CIS_Command" in req.headers["SOAPACTION"]
        f = bytes.fromhex(re.search(r"<CISDATA>(.*)</CISDATA>", req.content.decode()).group(1))
        pdc, cmd, data = f[2], f[3], f[4:-1]
        self.sent.append(cmd)
        out = b"\xfd"
        if cmd == 0x82:
            if self.state == 0x00 and self.boot_polls:
                self.boot_polls -= 1
                if not self.boot_polls:
                    self.state = 0x10
            out = self.reply(pdc, 0x82, bcd(self.disc) + bcd(1) + bcd(self.track) + bytes([self.state, 0]))
        elif cmd == 0xA0:
            out = self.reply(pdc, 0xA0, b"BDP-CX7000ES\x00\x00")
        elif cmd == 0x8A:
            out = self.reply(pdc, 0x8A, bcd(self.disc) + b"\x01" + bcd(1) + bcd(self.last) + b"\x00")
        elif cmd == 0x60:
            if data == b"\x01" and self.state == 0x00:
                self.boot_polls = 2  # takes a couple of polls to wake
        elif self.state == 0x00:
            return httpx.Response(200, text="<Res_CISDATA></Res_CISDATA>")
        elif cmd == 0x4A:
            self.disc, self.track, self.state = int(data[0:2].hex()), int(data[2:4].hex()), 0x20
        elif cmd == 0x13 or cmd == 0x57:
            self.state = 0x12
        elif cmd == 0x12:
            self.state = 0x11
        elif cmd == 0x14:
            self.state = 0x10
        elif cmd == 0xCC and not data:  # slot bitmap: five frames in one answer
            out = b""
            for packet in range(5):
                bits = bytearray(10)
                if packet == 0:
                    bits[0] = 0b00000001          # slot 1
                    bits[1] = 0b00000010          # slot 10
                if packet == 4:
                    bits[9] = 0b10000000          # slot 400
                    bits[4] = 0b00000001          # slot 353
                out += self.reply(pdc, 0xCC, bytes([packet]) + bytes(bits))
        elif cmd == 0x50:
            out = b"\xfe"
        return httpx.Response(200, text=f"<s:Envelope><s:Body><u:X_CIS_CommandResponse><Res_CISDATA>{out.hex()}"
                                        f"</Res_CISDATA></u:X_CIS_CommandResponse></s:Body></s:Envelope>")


def make(fake, clock=None):
    t = {"now": 0.0}
    drv = Cx7000esDriver(ChangerConfig(1, "Changer 1", "cx7000es", host="10.0.0.5"),
                         client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)),
                         clock=lambda: t["now"])
    return drv, t


def test_play_flow_and_finish():
    fake = FakeChanger()
    drv, t = make(fake)

    async def go():
        assert await drv.model_name() == "BDP-CX7000ES"
        await drv.load_and_play(377, 1, 3)
        assert fake.disc == 377 and fake.track == 1
        st = await drv.status()
        assert st.state == "loading" and st.slot == 377
        fake.state = 0x10                     # a moment of "stop" while the disc spins up
        assert (await drv.status()).state == "loading"
        fake.state, fake.track = 0x11, 1
        st = await drv.status()
        assert (st.state, st.track, st.disc_finished) == ("playing", 1, False)
        await drv.pause()
        assert (await drv.status()).state == "paused"
        await drv.resume()
        fake.track = 3
        assert (await drv.status()).track == 3
        fake.state, fake.track = 0x10, 0      # last track ended
        st = await drv.status()
        assert st.state == "stopped" and st.disc_finished is True
    run(go())


def test_wakes_from_standby_then_plays():
    fake = FakeChanger(power=False)
    drv, t = make(fake)

    async def go():
        await drv.load_and_play(353, 2)
        assert 0x60 in fake.sent and 0x4A not in fake.sent
        st = await drv.status()
        assert st.state == "loading"
        st = await drv.status()               # now awake: the play command goes out
        assert 0x4A in fake.sent and fake.disc == 353 and fake.track == 2
        assert st.state == "loading"
    run(go())


def test_offline_and_refused():
    async def go():
        drv = Cx7000esDriver(ChangerConfig(1, "Changer 1", "cx7000es", host="10.0.0.5"),
                             client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(
                                 lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))))
        st = await drv.status()
        assert st.online is False and st.error
        fake = FakeChanger()
        drv2, _ = make(fake)
        with pytest.raises(ChangerCommandError):
            await drv2._command(0x50, b"\x01")
    run(go())


def test_occupied_slots_reads_all_five_packets():
    fake = FakeChanger()
    drv, _ = make(fake)
    assert run(drv.occupied_slots()) == [1, 10, 353, 400]
