"""The changers as devices in Home Assistant, over MQTT discovery (v0.9.0).

Each changer shows up with a "Now playing" sensor (album, artist, track, slot and
cover as attributes), a "State" sensor, and Play/Pause, Next, Previous and Stop
buttons. The broker details come from the Supervisor's MQTT service (Mosquitto),
so there is nothing to configure. Without a broker the bridge simply stays off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx

log = logging.getLogger(__name__)

PREFIX = "disc_library"
DISCOVERY = "homeassistant"
AVAILABILITY = f"{PREFIX}/status"
BUTTONS = {"play_pause": ("Play/Pause", "mdi:play-pause"), "next": ("Next track", "mdi:skip-next"),
           "previous": ("Previous track", "mdi:skip-previous"), "stop": ("Stop", "mdi:stop")}


async def broker_from_supervisor(token: str | None = None) -> dict | None:
    token = token or os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("http://supervisor/services/mqtt", headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            log.info("No MQTT service from the Supervisor (%s); changers won't appear in Home Assistant.", r.status_code)
            return None
        return (r.json() or {}).get("data") or None
    except httpx.HTTPError as exc:
        log.info("Couldn't ask the Supervisor for MQTT: %s", exc)
        return None


def discovery_messages(changers: list[dict], version: str) -> list[tuple[str, dict]]:
    """(topic, payload) pairs that describe every changer entity to Home Assistant."""
    out = []
    for c in changers:
        cid = c["id"]
        uid = f"{PREFIX}_changer_{cid}"
        device = {"identifiers": [uid], "name": c["name"], "manufacturer": "Sony", "model": c.get("model") or "BDP-CX7000ES",
                  "sw_version": version}
        state = f"{PREFIX}/changer/{cid}/state"
        common = {"availability_topic": AVAILABILITY, "device": device}
        out.append((f"{DISCOVERY}/sensor/{uid}/now_playing/config", {
            **common, "name": "Now playing", "unique_id": f"{uid}_now_playing", "object_id": f"{uid}_now_playing",
            "state_topic": state, "value_template": "{{ value_json.now_playing }}",
            "json_attributes_topic": state, "icon": "mdi:album"}))
        out.append((f"{DISCOVERY}/sensor/{uid}/state/config", {
            **common, "name": "State", "unique_id": f"{uid}_state", "object_id": f"{uid}_state",
            "state_topic": state, "value_template": "{{ value_json.state }}", "icon": "mdi:disc-player"}))
        for key, (name, icon) in BUTTONS.items():
            out.append((f"{DISCOVERY}/button/{uid}/{key}/config", {
                **common, "name": name, "unique_id": f"{uid}_{key}", "object_id": f"{uid}_{key}",
                "command_topic": f"{PREFIX}/changer/{cid}/command", "payload_press": key, "icon": icon}))
    return out


async def changer_states(player, drivers: dict) -> dict[int, dict]:
    """What each changer is doing right now, in the shape published to Home Assistant."""
    now = await player.now_playing()
    out = {}
    for cid, d in drivers.items():
        if now.get("playing") and now["changer"]["id"] == cid:
            a = now["album"]
            st = now.get("status") or {}
            track = now.get("track")
            title = next((t["title"] for t in now.get("tracks") or []
                          if t["disc_number"] == now.get("disc_number") and t["number"] == track), None)
            out[cid] = {"state": st.get("state") or "playing", "now_playing": f"{a['artist']} – {a['title']}"[:250],
                        "album": a["title"], "artist": a["artist"], "album_id": a["id"], "track": track,
                        "track_title": title, "track_count": now.get("track_count"), "slot": now["changer"]["slot"],
                        "disc_number": now.get("disc_number"), "disc_count": now.get("disc_count"),
                        "media_kind": a.get("media_kind"), "art_url": a.get("art_url"), "managed": True}
            continue
        try:
            st = (await d.status()).to_dict()
        except NotImplementedError:
            st = {}
        state = st.get("state") or ("unreachable" if st.get("online") is False else "idle")
        if st.get("online") is False:
            state = "unreachable"
        elif st.get("power") is False:
            state = "off"
        out[cid] = {"state": state, "now_playing": "Nothing playing" if state not in ("playing", "paused")
                    else f"Slot {st.get('slot')}", "slot": st.get("slot"), "track": st.get("track"), "managed": False}
    return out


class MqttBridge:
    def __init__(self, player, drivers: dict, version: str, broker: dict | None = None, interval: float = 5.0):
        self.player, self.drivers, self.version = player, drivers, version
        self.broker = broker
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._last: dict[int, str] = {}
        self.connected = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def handle_command(self, changer_id: int, command: str) -> None:
        """A button pressed in Home Assistant."""
        d = self.drivers.get(changer_id)
        if d is None or command not in BUTTONS:
            return
        a = self.player.active
        try:
            if a and a.changer_id == changer_id:
                if command == "play_pause":
                    st = (await d.status()).state
                    await self.player.transport("resume" if st == "paused" else "pause")
                else:
                    await self.player.transport(command)
                return
            if command == "play_pause":
                st = (await d.status()).state
                await (d.resume() if st == "paused" else d.pause())
            else:
                await {"next": d.next_track, "previous": d.previous_track, "stop": d.stop}[command]()
        except Exception as exc:  # a button press must never take the bridge down
            log.warning("MQTT %s on changer %s failed: %s", command, changer_id, exc)

    async def _run(self) -> None:
        broker = self.broker or await broker_from_supervisor()
        if not broker:
            return
        import aiomqtt
        changers = [{"id": d.id, "name": d.name} for d in self.drivers.values()]
        backoff = 5
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=broker.get("host", "core-mosquitto"), port=int(broker.get("port", 1883)),
                    username=broker.get("username"), password=broker.get("password"), identifier="disc_library",
                    will=aiomqtt.Will(AVAILABILITY, "offline", retain=True),
                ) as client:
                    self.connected = True
                    backoff = 5
                    for topic, payload in discovery_messages(changers, self.version):
                        await client.publish(topic, json.dumps(payload), retain=True)
                    await client.publish(AVAILABILITY, "online", retain=True)
                    await client.subscribe(f"{PREFIX}/changer/+/command")
                    self._last.clear()
                    reader = asyncio.create_task(self._read(client))
                    try:
                        while True:
                            await self._publish_states(client)
                            await asyncio.sleep(self.interval)
                    finally:
                        reader.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                log.info("MQTT connection lost (%s); retrying in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _read(self, client) -> None:
        async for msg in client.messages:
            parts = str(msg.topic).split("/")
            if len(parts) == 4 and parts[3] == "command" and parts[2].isdigit():
                payload = msg.payload.decode() if isinstance(msg.payload, (bytes, bytearray)) else str(msg.payload)
                await self.handle_command(int(parts[2]), payload.strip())
                await self._publish_states(client, force=True)

    async def _publish_states(self, client, force: bool = False) -> None:
        try:
            states = await changer_states(self.player, self.drivers)
        except Exception as exc:
            log.debug("state poll failed: %s", exc)
            return
        for cid, st in states.items():
            body = json.dumps(st)
            if force or self._last.get(cid) != body:
                await client.publish(f"{PREFIX}/changer/{cid}/state", body, retain=True)
                self._last[cid] = body
