"""Play orchestration and listen tracking.

play_album() finds where the disc lives, stops whatever else is playing
(on either changer), runs the theater Listen script, loads the disc and
opens a listening session. poll() follows the changer and closes the
session, scoring it as a full listen when 90%+ of the album was heard.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import time
from dataclasses import dataclass

from .config import FULL_LISTEN_RATIO
from .db import Database
from .drivers import ChangerDriver
from .ha_client import HAClient

log = logging.getLogger(__name__)


class PlayError(Exception):
    """A request the player can't carry out, with a message fit for the user."""


@dataclass
class Active:
    session_id: int
    album_id: int
    disc_id: int
    changer_id: int
    slot: int
    start_track: int
    track_count: int | None
    runtime_sec: int | None
    last_track: int
    played_sec: float = 0.0
    last_tick: float | None = None
    disc_number: int = 1
    total_tracks: int | None = None      # every disc of the album
    heard_before: int = 0                # tracks heard on earlier discs of this session


def score_full_listen(*, played_sec: float, runtime_sec: int | None, start_track: int,
                      last_track: int, track_count: int | None, finished: bool,
                      ratio: float = FULL_LISTEN_RATIO) -> bool:
    """Apply the 90% rule.

    With a known runtime, compare time actually played. Without one (no
    track list fetched yet), fall back to the share of tracks heard; with
    no track count either, only a start-to-finish play counts.
    """
    if runtime_sec:
        return played_sec >= ratio * runtime_sec
    if track_count:
        heard = (last_track - start_track) + (1 if finished else 0)
        return heard >= ratio * track_count
    return finished and start_track == 1


class Player:
    def __init__(self, db: Database, drivers: dict[int, ChangerDriver], ha: HAClient | None = None,
                 listen_script: str | None = None, clock=time.monotonic, prefs=None):
        self.db = db
        self.drivers = drivers
        self.ha = ha
        self.listen_script = listen_script
        self.prefs = prefs
        self.clock = clock
        self.active: Active | None = None
        self._lock = asyncio.Lock()

    # --- commands --------------------------------------------------------------
    async def play_album(self, album_id: int, from_track: int = 1, user: str | None = None,
                         from_disc: int | None = None) -> dict:
        album = self.db.one("SELECT * FROM albums WHERE id = ?", (album_id,))
        if not album:
            raise PlayError("That album isn't in the library.")
        discs = self.db.query("SELECT * FROM discs WHERE album_id = ? ORDER BY disc_number", (album_id,))
        disc = next((d for d in discs if d["disc_number"] == from_disc), None) if from_disc else None
        disc = disc or (discs[0] if discs else None)
        label = f"Disc {disc['disc_number']} of {album['title']}" if disc and len(discs) > 1 else album["title"]
        if not disc or disc["location_type"] != "changer":
            where = self._describe_location(disc)
            raise PlayError(f"{label} isn't in a changer. {where}")
        driver = self.drivers.get(disc["changer_id"])
        if driver is None:
            raise PlayError(f"Changer {disc['changer_id']} isn't configured.")

        all_tracks = self.db.query("SELECT disc_number, duration_sec FROM tracks WHERE album_id = ?", (album_id,))
        tracks = [t for t in all_tracks if t["disc_number"] == disc["disc_number"]]
        track_count = len(tracks) or None
        runtime = album["runtime_sec"] or (
            sum(t["duration_sec"] or 0 for t in all_tracks)
            if all_tracks and all(t["duration_sec"] for t in all_tracks) else None
        )
        if runtime and disc["disc_number"] != discs[0]["disc_number"]:
            # Starting part-way through a set: only the discs from here on count toward the listen.
            later = [t for t in all_tracks if t["disc_number"] >= disc["disc_number"]]
            runtime = sum(t["duration_sec"] or 0 for t in later) or runtime

        async with self._lock:
            if self.active:
                await self._end("replaced")
            for other in self.drivers.values():
                if other.id != driver.id:
                    try:
                        st = await other.status()
                        if st.state in ("playing", "paused"):
                            await other.stop()
                    except NotImplementedError:
                        pass
            await self._scene(album, driver.id)
            try:
                await driver.load_and_play(disc["slot"], from_track, track_count)
            except NotImplementedError as exc:
                raise PlayError(f"{driver.name} can't be controlled yet: {exc}") from exc
            except (RuntimeError, OSError, httpx.HTTPError) as exc:
                raise PlayError(f"{driver.name} didn't respond: {exc}") from exc

            sid = self.db.execute(
                """INSERT INTO sessions (album_id, disc_id, changer_id, user_name, start_track, last_track, last_disc)
                   VALUES (?,?,?,?,?,?,?)""",
                (album_id, disc["id"], driver.id, user, from_track, from_track, disc["disc_number"]),
            )
            later_tracks = [t for t in all_tracks if t["disc_number"] >= disc["disc_number"]]
            self.active = Active(sid, album_id, disc["id"], driver.id, disc["slot"], from_track,
                                 track_count, runtime, from_track, 0.0, self.clock(),
                                 disc_number=disc["disc_number"], total_tracks=len(later_tracks) or None)
        log.info("Playing %s - %s on %s slot %s from track %s",
                 album["artist"], album["title"], driver.name, disc["slot"], from_track)
        return await self.now_playing()

    async def _scene(self, album: dict, changer_id: int) -> None:
        """Run the theater scene in Home Assistant: Listen for audio, Watch for video, on this changer's input."""
        if not self.ha:
            return
        video = album.get("media_kind") == "video"
        prefs = self.prefs.all() if self.prefs else {}
        script = (prefs.get("watch_script") if video else prefs.get("listen_script")) or \
            ("" if video else self.listen_script) or ""
        if not script:
            return
        source = (prefs.get("changer_inputs") or {}).get(str(changer_id))
        await self.ha.run_script(script, {"source": source} if source else None)

    async def resume_album(self, album_id: int, user: str | None = None) -> dict:
        last = self.db.one(
            """SELECT last_track, last_disc FROM sessions WHERE album_id = ? AND completed = 0 AND ended_at IS NOT NULL
               ORDER BY started_at DESC, id DESC LIMIT 1""",
            (album_id,),
        )
        return await self.play_album(album_id, from_track=(last["last_track"] if last else 1), user=user,
                                     from_disc=(last["last_disc"] if last else None))

    async def transport(self, action: str) -> dict:
        if not self.active:
            raise PlayError("Nothing is playing.")
        driver = self.drivers[self.active.changer_id]
        actions = {
            "pause": driver.pause,
            "resume": driver.resume,
            "next": driver.next_track,
            "previous": driver.previous_track,
            "stop": driver.stop,
        }
        if action not in actions:
            raise PlayError(f"Unknown action: {action}")
        async with self._lock:
            await self._tick()
            try:
                await actions[action]()
            except (RuntimeError, OSError, httpx.HTTPError) as exc:
                raise PlayError(f"{driver.name} didn't respond: {exc}") from exc
            if action == "stop":
                await self._end("stopped")
        return await self.now_playing()

    # --- polling ---------------------------------------------------------------
    async def poll(self) -> None:
        async with self._lock:
            await self._tick()

    async def _tick(self) -> None:
        a = self.active
        if not a:
            return
        driver = self.drivers[a.changer_id]
        try:
            st = await driver.status()
        except NotImplementedError:
            return
        now = self.clock()
        if not st.online:  # a network blip: keep the session, don't count the time
            a.last_tick = now
            return
        if a.last_tick is not None and st.state == "playing":
            a.played_sec += now - a.last_tick
        a.last_tick = now
        if st.track:
            a.last_track = max(a.last_track, min(st.track, a.track_count or st.track))
        self.db.execute(
            "UPDATE sessions SET last_track = ?, played_sec = ? WHERE id = ?",
            (a.last_track, int(a.played_sec), a.session_id),
        )
        if st.disc_finished:
            if not await self._next_disc():
                await self._end("finished")
        elif st.state == "stopped":
            await self._end("stopped")

    async def _next_disc(self) -> bool:
        """At the end of one disc of a set, carry on with the next disc, on whichever changer holds it.

        Returns False when there is no next disc. When the next disc isn't in a changer the session
        ends there, so it counts as a partial listen.
        """
        a = self.active
        nxt = self.db.one("SELECT * FROM discs WHERE album_id = ? AND disc_number > ? ORDER BY disc_number LIMIT 1",
                          (a.album_id, a.disc_number))
        if not nxt:
            return False
        driver = self.drivers.get(nxt["changer_id"]) if nxt["location_type"] == "changer" else None
        if driver is None:
            await self._end("next_disc_missing")
            return True
        count = self.db.one("SELECT COUNT(*) AS n FROM tracks WHERE album_id = ? AND disc_number = ?",
                            (a.album_id, nxt["disc_number"]))["n"] or None
        if driver.id != a.changer_id:
            try:
                await self.drivers[a.changer_id].stop()
            except NotImplementedError:
                pass
            album = self.db.one("SELECT * FROM albums WHERE id = ?", (a.album_id,))
            await self._scene(album, driver.id)  # the receiver moves to the other changer's input
        try:
            await driver.load_and_play(nxt["slot"], 1, count)
        except NotImplementedError:
            await self._end("next_disc_missing")
            return True
        a.heard_before += (a.last_track - a.start_track) + 1
        a.disc_id, a.changer_id, a.slot, a.disc_number = nxt["id"], driver.id, nxt["slot"], nxt["disc_number"]
        a.track_count, a.start_track, a.last_track = count, 1, 1
        self.db.execute("UPDATE sessions SET disc_id = ?, changer_id = ?, last_disc = ?, last_track = 1 WHERE id = ?",
                        (nxt["id"], driver.id, nxt["disc_number"], a.session_id))
        log.info("Continuing with disc %s on %s slot %s", nxt["disc_number"], driver.name, nxt["slot"])
        return True

    async def _end(self, reason: str) -> None:
        a = self.active
        if not a:
            return
        full = score_full_listen(
            played_sec=a.played_sec, runtime_sec=a.runtime_sec, start_track=a.start_track - a.heard_before,
            last_track=a.last_track, track_count=a.total_tracks or a.track_count, finished=(reason == "finished"),
            ratio=(self.prefs.get("full_listen_percent") / 100) if self.prefs else FULL_LISTEN_RATIO,
        )
        self.db.execute(
            """UPDATE sessions SET ended_at = datetime('now'), last_track = ?, played_sec = ?,
               completed = ?, end_reason = ? WHERE id = ?""",
            (a.last_track, int(a.played_sec), int(full), reason, a.session_id),
        )
        self.active = None

    # --- views -----------------------------------------------------------------
    async def now_playing(self) -> dict:
        a = self.active
        if not a:
            return {"playing": False}
        album = self.db.one("SELECT * FROM albums WHERE id = ?", (a.album_id,))
        driver = self.drivers[a.changer_id]
        try:
            st = (await driver.status()).to_dict()
        except NotImplementedError:
            st = {}
        tracks = self.db.query(
            "SELECT disc_number, number, title, duration_sec FROM tracks WHERE album_id = ? ORDER BY disc_number, number",
            (a.album_id,),
        )
        return {
            "playing": True,
            "album": album,
            "tracks": tracks,
            "changer": {"id": driver.id, "name": driver.name, "slot": a.slot},
            "disc_number": a.disc_number,
            "disc_count": self.db.one("SELECT COUNT(*) AS n FROM discs WHERE album_id = ?", (a.album_id,))["n"],
            "status": st,
            "track": st.get("track") or a.last_track,
            "track_count": a.track_count,
            "session_id": a.session_id,
        }

    def _describe_location(self, disc: dict | None) -> str:
        if not disc:
            return "No disc is recorded for it."
        t = disc["location_type"]
        note = f" ({disc['location_note']})" if disc.get("location_note") else ""
        return {
            "shelf": f"It's on the shelf{note}.",
            "player": f"It's in another player{note}.",
            "loaned": f"It's loaned out{note}.",
            "to_pull": "It's waiting to be pulled from the changer after being removed.",
        }.get(t, "Assign it a changer slot first.")
