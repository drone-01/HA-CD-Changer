"""Settings changed from inside the app (kept in the database, no restart needed).

Changer wiring stays in the Home Assistant app configuration because the
drivers are built at start-up.
"""

from __future__ import annotations

import json

from .db import Database

DEFAULTS: dict[str, object] = {
    "full_listen_percent": 90,      # share of an album's runtime that counts as a full listen
    "listen_script": "script.theater_listen",   # HA script run before a CD plays; empty = use the app configuration
    "watch_script": "script.theater_watch",     # HA script run before a movie or concert plays
    "changer_inputs": {"1": "SA-CD/CD", "2": "VIDEO"},  # receiver input each changer is wired to (passed as `source`)
    "apple_covers": True,           # fall back to Apple Music artwork when the Cover Art Archive has none
    "auto_sync_on_import": True,    # look up new albums right after a spreadsheet import
    "show_value_marks": False,      # $ / $$ / $$$ on library covers
    "discogs_token": "",            # personal access token; never sent back to the browser
    "discogs_user": "",
    "discogs_condition": "Very Good (VG)",
}
SECRET = ("discogs_token",)


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
    if key == "changer_inputs":
        if isinstance(value, str):
            try:
                value = json.loads(value or "{}")
            except ValueError as exc:
                raise PrefsError("Changer inputs must be a list of changer to receiver input.") from exc
        if not isinstance(value, dict):
            raise PrefsError("Changer inputs must be a list of changer to receiver input.")
        out = {}
        for k, v in value.items():
            if not str(k).isdigit():
                raise PrefsError("Changer inputs are keyed by changer number.")
            v = str(v or "").strip()
            if len(v) > 40:
                raise PrefsError("That receiver input name is too long.")
            if v:
                out[str(int(k))] = v
        return out
    if key in ("listen_script", "watch_script"):
        v = str(value or "").strip()
        if v and not v.startswith("script."):
            raise PrefsError("The theater script must be a script entity, like script.theater_listen.")
        return v
    if key in ("discogs_token", "discogs_user"):
        return str(value or "").strip()
    if key == "discogs_condition":
        from .discogs import CONDITIONS
        if value not in CONDITIONS:
            raise PrefsError("Pick one of the Discogs conditions.")
        return value
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

    def public(self) -> dict:
        """Settings safe to send to the browser: secrets become has_<name>."""
        out = self.all()
        for k in SECRET:
            out["has_" + k] = bool(out.pop(k))
        return out

    def get(self, key: str):
        return self.all()[key]

    def update(self, changes: dict) -> dict:
        unknown = [k for k in changes if k not in DEFAULTS]
        if unknown:
            raise PrefsError(f"Unknown setting: {', '.join(unknown)}")
        clean = {k: _coerce(k, v) for k, v in changes.items()}
        for k, v in clean.items():
            stored = ("1" if v else "0") if isinstance(v, bool) else json.dumps(v) if isinstance(v, dict) else str(v)
            self.db.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, stored))
        return self.all()
