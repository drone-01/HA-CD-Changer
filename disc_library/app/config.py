"""Add-on options and paths.

The Supervisor writes validated options to /data/options.json at start-up.
For local development, point DL_OPTIONS / DL_DATA_DIR somewhere else.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

FULL_LISTEN_RATIO = 0.90  # decided: 90%+ of the album's runtime in one sitting


@dataclass
class ChangerConfig:
    id: int
    name: str
    driver: str = "simulated"
    host: str | None = None
    port: int | None = None


@dataclass
class Settings:
    data_dir: Path
    www_dir: Path
    changers: list[ChangerConfig] = field(default_factory=list)
    listen_script: str | None = None
    sim_track_seconds: int = 30
    log_level: str = "info"
    poll_seconds: float = 2.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "disc_library.db"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("DL_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    www_dir = Path(os.environ.get("DL_WWW", Path(__file__).resolve().parent.parent / "www"))
    opts_path = Path(os.environ.get("DL_OPTIONS", data_dir / "options.json"))

    opts: dict = {}
    if opts_path.exists():
        opts = json.loads(opts_path.read_text())

    raw_changers = opts.get("changers") or [
        {"name": "Changer 1", "driver": "simulated"},
        {"name": "Changer 2", "driver": "simulated"},
    ]
    changers = [
        ChangerConfig(
            id=i + 1,
            name=c.get("name") or f"Changer {i + 1}",
            driver=c.get("driver", "simulated"),
            host=c.get("host") or None,
            port=c.get("port") or None,
        )
        for i, c in enumerate(raw_changers)
    ]
    return Settings(
        data_dir=data_dir,
        www_dir=www_dir,
        changers=changers,
        listen_script=(opts.get("listen_script") or None),
        sim_track_seconds=int(opts.get("sim_track_seconds", 30)),
        log_level=opts.get("log_level", "info"),
    )
