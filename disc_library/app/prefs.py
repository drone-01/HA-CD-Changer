"""Settings changed from inside the app (kept in the database, no restart needed).

Changer wiring stays in the Home Assistant app configuration because the
drivers are built at start-up.
"""

from __future__ import annotations

from .db import Database

DEFAULTS: dict[str, object] = {
    "full_listen_percent": 90,      # share of an album's runtime that counts as a full listen
    "listen_script": "",            # HA script run before playing; empty = use the app configuration
    "apple_covers": True,           # fall back to Apple Music artwork when the Cover Art Archive has none
    "auto_sync_on_import": True,    # look up new albums right after a spreadsheet import
    "show_value_marks": False,      # $ / $$ / $$$ on library covers
}


class PrefsError(ValueError):
    pass


def _coerce(key: str, value: object) -> object:
    if key == "full_listen_percent":
        try:
            v = int(value)
        except (TypeError, ValueError) as exc:
            raise PrefsError("Full listen must be a whole number.") from exc
        if not 50 <= v <= 100:
            raise PrefsError("Full listen must be between 50% and 100%.")
        return v
    if key == "listen_script":
        v = str(value or "").strip()
        if v and not v.startswith("script."):
            raise PrefsError("The theater script must be a script entity, like script.theater_listen.")
        return v
    if isinstance(DEFAULTS[key], bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    return value


class Prefs:
    def __init__(self, db: Database):
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")

    def all(self) -> dict:
        out = dict(DEFAULTS)
        for r in self.db.query("SELECT key, value FROM app_settings"):
            if r["key"] in DEFAULTS:
                try:
                    out[r["key"]] = _coerce(r["key"], r["value"])
                except PrefsError:
                    pass
        return out

    def get(self, key: str):
        return self.all()[key]

    def update(self, changes: dict) -> dict:
        unknown = [k for k in changes if k not in DEFAULTS]
        if unknown:
            raise PrefsError(f"Unknown setting: {', '.join(unknown)}")
        clean = {k: _coerce(k, v) for k, v in changes.items()}
        for k, v in clean.items():
            stored = ("1" if v else "0") if isinstance(v, bool) else str(v)
            self.db.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, stored))
        return self.all()
