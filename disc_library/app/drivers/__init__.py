from ..config import ChangerConfig, Settings
from .base import ChangerDriver, ChangerStatus
from .cx7000es import Cx7000esDriver
from .simulated import SimulatedChanger


def build_driver(cfg: ChangerConfig, settings: Settings) -> ChangerDriver:
    if cfg.driver == "cx7000es":
        return Cx7000esDriver(cfg)
    return SimulatedChanger(cfg, track_seconds=settings.sim_track_seconds)


__all__ = ["ChangerDriver", "ChangerStatus", "build_driver"]
