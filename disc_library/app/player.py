"""Play orchestration and listen tracking.

play_album() finds where the disc lives, stops whatever else is playing
(on either changer), runs the theater Listen script, loads the disc and
opens a listening session. poll() follows the changer and closes the
session, scoring it as a full listen when 90%+ of the album was heard.
"""

from __future__ import annotations

import asyncio
import logging
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
    async def play_album(self, album_id: int, from_track: int = 1, user: str | None = None) -> dict:
        album = self.db.one("SELECT * FROM albums WHERE id = ?", (album_id,))
        if not album:
            raise PlayError("That album isn't in the library.")
        disc = self.db.one(
            "SELECT * FROM discs WHERE album_id = ? ORDER BY disc_number LIMIT 1", (album_id,)
        )
        if not disc or disc["location_type"] != "changer":
            where = self._describe_location(disc)
            raise PlayError(f"{album['title']} isn't in a changer. {where}")
        driver = self.drivers.get(disc["changer_id"])
        if driver is None:
            raise PlayError(f"Changer {disc['changer_id']} isn't configured.")

        tracks = self.db.query(
            "SELECT duration_sec FROM tracks WHERE album_id = ? AND disc_number = ?",
            (album_id, disc["disc_number"]),
        )
        track_count = len(tracks) or None
        runtime = album["runtime_sec"] or (
            sum(t["duration_sec"] or 0 for t in tracks) if tracks and all(t["duration_sec"] for t in tracks) else None
        )

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
            script = (self.prefs.get("listen_script") if self.prefs else "") or self.listen_script
            if self.ha and script:
                await self.ha.run_script(script)
            try:
                await driver.load_and_play(disc["slot"], from_track, track_count)
            except NotImplementedError as exc:
                raise PlayError(f"{driver.name} can't be controlled yet: {exc}") from exc

            sid = self.db.execute(
                """INSERT INTO sessions (album_id, disc_id, changer_id, user_name, start_track, last_track)
                   VALUES (?,?,?,?,?,?)""",
                (album_id, disc["id"], driver.id, user, from_track, from_track),
            )
            self.active = Active(sid, album_id, disc["id"], driver.id, disc["slot"], from_track,
                                 track_count, runtime, from_track, 0.0, self.clock())
        log.info("Playing %s - %s on %s slot %s from track %s",
                 album["artist"], album["title"], driver.name, disc["slot"], from_track)
        return await self.now_playing()

    async def resume_album(self, album_id: int, user: str | None = None) -> dict:
        last = self.db.one(
            """SELECT last_track FROM sessions WHERE album_id = ? AND completed = 0 AND ended_at IS NOT NULL
               ORDER BY started_at DESC, id DESC LIMIT 1""",
            (album_id,),
        )
        return await self.play_album(album_id, from_track=(last["last_track"] if last else 1), user=user)

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
            await actions[action]()
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
            await self._end("finished")
        elif st.state == "stopped":
            await self._end("stopped")

    async def _end(self, reason: str) -> None:
        a = self.active
        if not a:
            return
        full = score_full_listen(
            played_sec=a.played_sec, runtime_sec=a.runtime_sec, start_track=a.start_track,
            last_track=a.last_track, track_count=a.track_count, finished=(reason == "finished"),
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
            "SELECT number, title, duration_sec FROM tracks WHERE album_id = ? ORDER BY disc_number, number",
            (a.album_id,),
        )
        return {
            "playing": True,
            "album": album,
            "tracks": tracks,
            "changer": {"id": driver.id, "name": driver.name, "slot": a.slot},
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
        }.get(t, "Assign it a changer slot first.")
