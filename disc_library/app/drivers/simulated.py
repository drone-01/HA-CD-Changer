"""A changer that exists only in software.

Every track lasts `track_seconds`, so the whole app (library, play logic,
changer switching, listen tracking, stats) can be exercised before the real
changers are on the network.
"""

from __future__ import annotations

import time

from ..config import ChangerConfig
from .base import ChangerDriver, ChangerStatus

DEFAULT_TRACK_COUNT = 12


class SimulatedChanger(ChangerDriver):
    def __init__(self, cfg: ChangerConfig, track_seconds: int = 30, clock=time.monotonic):
        super().__init__(cfg)
        self.track_seconds = track_seconds
        self.clock = clock
        self._power = False
        self._slot: int | None = None
        self._track_count = DEFAULT_TRACK_COUNT
        self._state = "stopped"
        self._start_track = 1
        self._play_started: float | None = None   # clock at (re)start of playback
        self._banked = 0.0                         # seconds played before the last pause
        self._finished = False

    # --- helpers -------------------------------------------------------------
    def _elapsed(self) -> float:
        running = (self.clock() - self._play_started) if (self._state == "playing" and self._play_started) else 0.0
        return self._banked + running

    def _position(self) -> tuple[int, int]:
        """(track, seconds into track)"""
        e = self._elapsed()
        offset = int(e // self.track_seconds)
        return self._start_track + offset, int(e % self.track_seconds)

    def _restart_at(self, track: int) -> None:
        self._start_track = track
        self._banked = 0.0
        self._play_started = self.clock()
        self._state = "playing"
        self._finished = False

    # --- interface -----------------------------------------------------------
    async def power(self, on: bool) -> None:
        self._power = on
        if not on:
            self._state = "stopped"

    async def load_and_play(self, slot: int, track: int = 1, track_count: int | None = None) -> None:
        self._power = True
        self._slot = slot
        self._track_count = track_count or DEFAULT_TRACK_COUNT
        self._restart_at(max(1, min(track, self._track_count)))

    async def pause(self) -> None:
        if self._state == "playing":
            self._banked = self._elapsed()
            self._play_started = None
            self._state = "paused"

    async def resume(self) -> None:
        if self._state == "paused":
            self._play_started = self.clock()
            self._state = "playing"

    async def stop(self) -> None:
        self._state = "stopped"
        self._play_started = None
        self._banked = 0.0

    async def next_track(self) -> None:
        track, _ = self._position()
        if track < self._track_count:
            self._restart_at(track + 1)

    async def previous_track(self) -> None:
        track, into = self._position()
        self._restart_at(track if into > 3 or track == 1 else track - 1)

    async def status(self) -> ChangerStatus:
        if self._state in ("playing", "paused"):
            track, into = self._position()
            if track > self._track_count:
                self._state = "stopped"
                self._finished = True
                self._play_started = None
                return ChangerStatus(True, self._power, "stopped", self._slot, self._track_count, 0, True)
            return ChangerStatus(True, self._power, self._state, self._slot, track, into, False)
        return ChangerStatus(True, self._power, "stopped", self._slot, None, 0, self._finished)
