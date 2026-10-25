"""SQLite storage.

The spreadsheet stays the catalog master for now. The database adds what the
spreadsheet doesn't hold: where each disc lives, genres, favorites, art,
track lists and listening history. Re-importing the spreadsheet updates
catalog fields and never touches those.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id            INTEGER PRIMARY KEY,
    import_key    TEXT UNIQUE NOT NULL,
    artist        TEXT NOT NULL,
    title         TEXT NOT NULL,
    year          INTEGER,
    label         TEXT,
    catalog_no    TEXT,
    format        TEXT NOT NULL DEFAULT 'CD',
    media_kind    TEXT NOT NULL DEFAULT 'audio',      -- audio | video
    edition_notes TEXT,
    status        TEXT NOT NULL DEFAULT 'owned',      -- owned | on_order
    source_sheet  TEXT,
    genre         TEXT,
    subgenre      TEXT,
    mood_tags     TEXT,                                -- comma-separated
    runtime_sec   INTEGER,
    art_url       TEXT,
    favorite      INTEGER NOT NULL DEFAULT 0,
    rating        INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS discs (
    id            INTEGER PRIMARY KEY,
    album_id      INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    disc_number   INTEGER NOT NULL DEFAULT 1,
    location_type TEXT NOT NULL DEFAULT 'unassigned', -- changer | shelf | player | loaned | unassigned
    changer_id    INTEGER,
    slot          INTEGER,
    location_note TEXT,
    UNIQUE (album_id, disc_number),
    UNIQUE (changer_id, slot)
);

CREATE TABLE IF NOT EXISTS tracks (
    id            INTEGER PRIMARY KEY,
    album_id      INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    disc_number   INTEGER NOT NULL DEFAULT 1,
    number        INTEGER NOT NULL,
    title         TEXT,
    duration_sec  INTEGER,
    UNIQUE (album_id, disc_number, number)
);

CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY,
    album_id      INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    disc_id       INTEGER REFERENCES discs(id) ON DELETE SET NULL,
    changer_id    INTEGER,
    user_name     TEXT,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at      TEXT,
    start_track   INTEGER NOT NULL DEFAULT 1,
    last_track    INTEGER NOT NULL DEFAULT 1,
    played_sec    INTEGER NOT NULL DEFAULT 0,
    completed     INTEGER NOT NULL DEFAULT 0,          -- 1 = full listen (90% rule)
    end_reason    TEXT                                 -- finished | stopped | replaced
);

CREATE INDEX IF NOT EXISTS ix_sessions_album ON sessions(album_id);
CREATE INDEX IF NOT EXISTS ix_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS ix_albums_artist ON albums(artist, year);
"""


# Columns added after v0.1.0; added in place on start-up so existing data is kept.
MIGRATIONS = {
    "albums": {
        "mbid_release": "TEXT",
        "mbid_release_group": "TEXT",
        "original_year": "INTEGER",
        "enrich_status": "TEXT NOT NULL DEFAULT 'pending'",   # pending | matched | manual | not_found | error | skipped
        "enrich_note": "TEXT",
        "enriched_at": "TEXT",
    },
}


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with self.connect() as con:
            con.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
                for name, decl in cols.items():
                    if name not in have:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.connect() as con:
            cur = con.execute(sql, params)
            return cur.lastrowid
