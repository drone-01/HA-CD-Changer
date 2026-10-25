"""Sony BDP-CX7000ES driver (shell).

The changers support network control (System Settings > Custom Installation
Control) with status broadcast, and RS-232 as a fallback (reachable through
a Global Cache iTach IP2SL-P on TCP port 4999).

The command set, port and message framing come from Sony's "RS-232C/Network
Control Protocol Specification" (v0.97, Sept 2009). Until that sheet is in
hand, the commands below are deliberately unimplemented rather than guessed.
Phase 1 fills them in and adds a probe script to verify each one.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import ChangerConfig
from .base import ChangerDriver, ChangerStatus

log = logging.getLogger(__name__)

PENDING = "CX7000ES command set pending Sony's protocol spec (Phase 1)"


class Cx7000esDriver(ChangerDriver):
    def __init__(self, cfg: ChangerConfig):
        super().__init__(cfg)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        if not self.cfg.host or not self.cfg.port:
            log.warning("%s: no host/port configured", self.name)
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.cfg.host, self.cfg.port), timeout=5
            )
            log.info("%s: connected to %s:%s", self.name, self.cfg.host, self.cfg.port)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("%s: connect failed: %s", self.name, exc)

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            self._writer = None

    async def _send(self, payload: bytes) -> None:
        raise NotImplementedError(PENDING)

    async def power(self, on: bool) -> None:
        raise NotImplementedError(PENDING)

    async def load_and_play(self, slot: int, track: int = 1, track_count: int | None = None) -> None:
        raise NotImplementedError(PENDING)

    async def pause(self) -> None:
        raise NotImplementedError(PENDING)

    async def resume(self) -> None:
        raise NotImplementedError(PENDING)

    async def stop(self) -> None:
        raise NotImplementedError(PENDING)

    async def next_track(self) -> None:
        raise NotImplementedError(PENDING)

    async def previous_track(self) -> None:
        raise NotImplementedError(PENDING)

    async def status(self) -> ChangerStatus:
        online = self._writer is not None and not self._writer.is_closing()
        return ChangerStatus(online=online, error=None if online else "not connected")
