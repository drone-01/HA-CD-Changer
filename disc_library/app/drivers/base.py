"""The interface every changer driver implements.

The player only talks to this interface, so the simulated changer and the
real CX7000ES driver are interchangeable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

from ..config import ChangerConfig


@dataclass
class ChangerStatus:
    online: bool = False
    power: bool = False
    state: str = "stopped"          # playing | paused | stopped | loading
    slot: int | None = None
    track: int | None = None
    track_elapsed_sec: int = 0
    disc_finished: bool = False     # True once the last track has ended
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class ChangerDriver(ABC):
    def __init__(self, cfg: ChangerConfig):
        self.cfg = cfg

    @property
    def id(self) -> int:
        return self.cfg.id

    @property
    def name(self) -> str:
        return self.cfg.name

    async def connect(self) -> None:  # optional
        return None

    async def close(self) -> None:  # optional
        return None

    @abstractmethod
    async def power(self, on: bool) -> None: ...

    @abstractmethod
    async def load_and_play(self, slot: int, track: int = 1, track_count: int | None = None) -> None:
        """Load the disc in `slot` and start playing at `track`."""

    @abstractmethod
    async def pause(self) -> None: ...

    @abstractmethod
    async def resume(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def next_track(self) -> None: ...

    @abstractmethod
    async def previous_track(self) -> None: ...

    @abstractmethod
    async def status(self) -> ChangerStatus: ...
