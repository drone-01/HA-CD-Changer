"""Library queries: browsing, locations, history, stats and Vibe Sesh picks."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from .db import Database

SLOTS_PER_CHANGER = 400
LOCATION_TYPES = ("changer", "shelf", "player", "loaned", "unassigned")

SORTS = {
    "artist": "a.artist COLLATE NOCASE, a.year, a.title COLLATE NOCASE",
    "title": "a.title COLLATE NOCASE",
    "year": "a.year DESC, a.artist COLLATE NOCASE",
    "added": "a.created_at DESC, a.id DESC",
    "played": "last_played DESC NULLS LAST, a.artist COLLATE NOCASE",
}

ALBUM_SELECT = """
SELECT a.*, d.id AS disc_id, d.location_type, d.changer_id, d.slot, d.location_note,
       (SELECT MAX(started_at) FROM sessions s WHERE s.album_id = a.id) AS last_played,
       (SELECT COUNT(*) FROM sessions s WHERE s.album_id = a.id AND s.completed = 1) AS full_listens
FROM albums a
LEFT JOIN discs d ON d.album_id = a.id AND d.disc_number = 1
"""


class LocationError(ValueError):
    pass


class Library:
    def __init__(self, db: Database, changer_ids: list[int]):
        self.db = db
        self.changer_ids = changer_ids

    # --- browse ----------------------------------------------------------------
    def albums(self, q: str | None = None, genre: str | None = None, favorites: bool = False,
               media_kind: str | None = "audio", status: str | None = "owned", sort: str = "artist",
               limit: int = 1000, offset: int = 0) -> list[dict]:
        where, params = [], []
        if q:
            where.append("""(a.artist LIKE ? OR a.title LIKE ? OR a.label LIKE ?
                             OR EXISTS (SELECT 1 FROM tracks t WHERE t.album_id = a.id AND t.title LIKE ?))""")
            params += [f"%{q}%"] * 4
        if genre:
            where.append("(a.genre = ? OR a.subgenre = ?)")
            params += [genre, genre]
        if favorites:
            where.append("a.favorite = 1")
        if media_kind:
            where.append("a.media_kind = ?")
            params.append(media_kind)
        if status:
            where.append("a.status = ?")
            params.append(status)
        sql = ALBUM_SELECT + (" WHERE " + " AND ".join(where) if where else "")
        sql += f" ORDER BY {SORTS.get(sort, SORTS['artist'])} LIMIT ? OFFSET ?"
        return self.db.query(sql, (*params, limit, offset))

    def album(self, album_id: int) -> dict | None:
        a = self.db.one(ALBUM_SELECT + " WHERE a.id = ?", (album_id,))
        if not a:
            return None
        a["discs"] = self.db.query("SELECT * FROM discs WHERE album_id = ? ORDER BY disc_number", (album_id,))
        a["tracks"] = self.db.query(
            "SELECT disc_number, number, title, duration_sec FROM tracks WHERE album_id = ? ORDER BY disc_number, number",
            (album_id,),
        )
        a["resume"] = self.db.one(
            """SELECT last_track, ended_at FROM sessions WHERE album_id = ? AND completed = 0
               AND ended_at IS NOT NULL ORDER BY started_at DESC, id DESC LIMIT 1""",
            (album_id,),
        )
        return a

    def genres(self) -> list[dict]:
        return self.db.query(
            """SELECT genre AS name, COUNT(*) AS albums FROM albums
               WHERE genre IS NOT NULL AND media_kind = 'audio' AND status = 'owned'
               GROUP BY genre ORDER BY albums DESC"""
        )

    def set_favorite(self, album_id: int, favorite: bool) -> None:
        self.db.execute("UPDATE albums SET favorite = ? WHERE id = ?", (int(favorite), album_id))

    def set_tags(self, album_id: int, genre: str | None, subgenre: str | None, mood_tags: str | None) -> None:
        self.db.execute(
            "UPDATE albums SET genre = ?, subgenre = ?, mood_tags = ?, updated_at = datetime('now') WHERE id = ?",
            (genre, subgenre, mood_tags, album_id),
        )

    # --- locations -------------------------------------------------------------
    def set_location(self, disc_id: int, location_type: str, changer_id: int | None = None,
                     slot: int | None = None, note: str | None = None) -> dict:
        if location_type not in LOCATION_TYPES:
            raise LocationError(f"Unknown location: {location_type}")
        if location_type == "changer":
            if changer_id not in self.changer_ids:
                raise LocationError("Pick one of the configured changers.")
            if not slot or not 1 <= slot <= SLOTS_PER_CHANGER:
                raise LocationError(f"Slot must be 1-{SLOTS_PER_CHANGER}.")
            taken = self.db.one(
                """SELECT a.artist, a.title FROM discs d JOIN albums a ON a.id = d.album_id
                   WHERE d.changer_id = ? AND d.slot = ? AND d.id != ?""",
                (changer_id, slot, disc_id),
            )
            if taken:
                raise LocationError(f"Slot {slot} already holds {taken['artist']} - {taken['title']}.")
        else:
            changer_id = slot = None
        self.db.execute(
            "UPDATE discs SET location_type = ?, changer_id = ?, slot = ?, location_note = ? WHERE id = ?",
            (location_type, changer_id, slot, note, disc_id),
        )
        return self.db.one("SELECT * FROM discs WHERE id = ?", (disc_id,))

    def free_slot(self, changer_id: int | None = None) -> dict | None:
        """First empty slot, on the given changer or on whichever has the most room."""
        candidates = [changer_id] if changer_id else sorted(
            self.changer_ids, key=lambda c: self._used(c)
        )
        for cid in candidates:
            used = {r["slot"] for r in self.db.query("SELECT slot FROM discs WHERE changer_id = ?", (cid,))}
            for s in range(1, SLOTS_PER_CHANGER + 1):
                if s not in used:
                    return {"changer_id": cid, "slot": s}
        return None

    def _used(self, changer_id: int) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM discs WHERE changer_id = ?", (changer_id,))["n"]

    def slot_map(self) -> list[dict]:
        return [
            {"changer_id": cid, "used": self._used(cid), "capacity": SLOTS_PER_CHANGER}
            for cid in self.changer_ids
        ]

    # --- history and stats -----------------------------------------------------
    def history(self, limit: int = 50) -> list[dict]:
        return self.db.query(
            """SELECT s.*, a.artist, a.title, a.year FROM sessions s JOIN albums a ON a.id = s.album_id
               ORDER BY s.started_at DESC, s.id DESC LIMIT ?""",
            (limit,),
        )

    def continue_listening(self, limit: int = 5) -> list[dict]:
        """Albums whose most recent session stopped partway."""
        return self.db.query(
            """SELECT a.id AS album_id, a.artist, a.title, s.last_track, s.ended_at,
                      (SELECT COUNT(*) FROM tracks t WHERE t.album_id = a.id AND t.disc_number = 1) AS track_count
               FROM sessions s JOIN albums a ON a.id = s.album_id
               WHERE s.id = (SELECT id FROM sessions s2 WHERE s2.album_id = s.album_id
                             ORDER BY s2.started_at DESC, s2.id DESC LIMIT 1)
                 AND s.completed = 0 AND s.ended_at IS NOT NULL AND s.end_reason != 'finished'
               ORDER BY s.ended_at DESC LIMIT ?""",
            (limit,),
        )

    def stats(self, since: str | None = None) -> dict:
        """since: ISO date (inclusive) or None for all time."""
        cond, params = ("WHERE s.started_at >= ?", (since,)) if since else ("", ())
        totals = self.db.one(
            f"""SELECT COALESCE(SUM(s.completed), 0) AS full_listens,
                       COALESCE(SUM(CASE WHEN s.ended_at IS NOT NULL AND s.completed = 0 THEN 1 ELSE 0 END), 0)
                           AS partial_listens,
                       COALESCE(SUM(s.played_sec), 0) AS played_sec,
                       COUNT(DISTINCT s.album_id) AS albums_played
                FROM sessions s {cond}""",
            params,
        )
        owned = self.db.one(
            "SELECT COUNT(*) AS n FROM albums WHERE status = 'owned' AND media_kind = 'audio'"
        )["n"]
        by_genre = self.db.query(
            f"""SELECT COALESCE(a.genre, 'Untagged') AS genre, SUM(s.completed) AS full_listens
                FROM sessions s JOIN albums a ON a.id = s.album_id {cond}
                GROUP BY 1 HAVING SUM(s.completed) > 0 ORDER BY 2 DESC""",
            params,
        )
        most_played = self.db.query(
            f"""SELECT a.id AS album_id, a.artist, a.title, COUNT(*) AS plays, SUM(s.completed) AS full_listens
                FROM sessions s JOIN albums a ON a.id = s.album_id {cond}
                GROUP BY a.id ORDER BY plays DESC, full_listens DESC LIMIT 10""",
            params,
        )
        days = [
            date.fromisoformat(r["d"])
            for r in self.db.query("SELECT DISTINCT date(started_at) AS d FROM sessions ORDER BY d")
        ]
        current, longest = _streaks(days, date.today())
        return {
            "full_listens": totals["full_listens"],
            "partial_listens": totals["partial_listens"],
            "hours": round(totals["played_sec"] / 3600, 1),
            "albums_played": totals["albums_played"],
            "albums_owned": owned,
            "streak_days": current,
            "longest_streak_days": longest,
            "by_genre": by_genre,
            "most_played": most_played,
        }

    # --- pressings ---------------------------------------------------------------
    PRESSING_FILTERS = {
        "review": """a.pressing_locked = 0 AND a.enrich_status IN ('matched', 'manual')
                     AND COALESCE(a.match_method, 'title') != 'catalog'""",
        "catalog": "a.pressing_locked = 0 AND a.enrich_status = 'matched' AND a.match_method = 'catalog'",
        "locked": "a.pressing_locked = 1",
        "unmatched": "a.pressing_locked = 0 AND a.enrich_status IN ('pending', 'not_found', 'error')",
        "skipped": "a.enrich_status = 'skipped'",
    }

    def pressing_counts(self) -> dict:
        base = "FROM albums a WHERE a.media_kind = 'audio' AND a.status = 'owned' AND "
        return {k: self.db.one(f"SELECT COUNT(*) AS n {base} {w}")["n"] for k, w in self.PRESSING_FILTERS.items()}

    def pressings(self, which: str = "review", limit: int = 1000) -> list[dict]:
        where = self.PRESSING_FILTERS.get(which, self.PRESSING_FILTERS["review"])
        rows = self.db.query(
            f"""SELECT a.id, a.artist, a.title, a.year, a.format, a.catalog_no, a.catalog_verified, a.barcode,
                       a.pressing_locked, a.match_method, a.enrich_status, a.enrich_note, a.art_url,
                       a.rel_label, a.rel_catno, a.rel_barcode, a.rel_country, a.rel_date, a.rel_format,
                       a.rel_track_count, a.mbid_release
                FROM albums a WHERE a.media_kind = 'audio' AND a.status = 'owned' AND {where}
                ORDER BY a.artist COLLATE NOCASE, a.year, a.title COLLATE NOCASE LIMIT ?""",
            (limit,),
        )
        for r in rows:
            mine = r["catalog_verified"] or r["catalog_no"]
            r["catalog_mismatch"] = bool(mine and r["rel_catno"] and not (
                set(_cat_forms(mine)) & set(_cat_forms(r["rel_catno"]))))
        return rows

    def export_pressings(self, path) -> int:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        wb = Workbook()
        ws = wb.active
        ws.title = "Pressings"
        cols = ["Artist", "Album", "Your catalog no.", "Verified catalog no.", "Barcode", "Locked",
                "Match", "Label", "Country", "Release date", "Format", "Tracks", "MusicBrainz release"]
        ws.append(cols)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="FF2C2926")
        rows = self.db.query(
            """SELECT * FROM albums WHERE media_kind = 'audio' AND status = 'owned'
               ORDER BY artist COLLATE NOCASE, year, title COLLATE NOCASE""")
        for r in rows:
            ws.append([r["artist"], r["title"], r["catalog_no"], r["catalog_verified"], r["barcode"] or r["rel_barcode"],
                       "Yes" if r["pressing_locked"] else "", r["match_method"] or r["enrich_status"],
                       r["rel_label"], r["rel_country"], r["rel_date"], r["rel_format"], r["rel_track_count"],
                       f"https://musicbrainz.org/release/{r['mbid_release']}" if r["mbid_release"] else None])
        for col, width in zip("ABCDEFGHIJKLM", (24, 34, 18, 18, 16, 8, 10, 22, 8, 12, 12, 7, 60)):
            ws.column_dimensions[col].width = width
        ws.freeze_panes = "A2"
        wb.save(path)
        return len(rows)

    # --- Vibe Sesh -------------------------------------------------------------
    def vibe(self, mood: str | None = None, energy: str | None = None, time_budget: str | None = None,
             familiarity: str | None = None, count: int = 3, seed: int | None = None) -> list[dict]:
        """Three playable picks, each with a reason built only from known facts.

        Mood and energy match against an album's mood tags; until the genre
        and mood tagging pass is done, those filters are skipped rather than
        guessed, and the picks lean on time and familiarity alone.
        """
        rows = self.albums(limit=5000)
        rows = [r for r in rows if r["location_type"] == "changer"]
        rng = random.Random(seed)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
        max_runtime = {"under_45": 45 * 60, "about_hour": 70 * 60}.get(time_budget or "")

        scored = []
        for r in rows:
            reasons, score = [], rng.random()
            tags = {t.strip().lower() for t in (r["mood_tags"] or "").split(",") if t.strip()}
            for want in (mood, energy):
                if want and tags:
                    if want.lower() in tags:
                        score += 2
                        reasons.append(want.capitalize())
                    else:
                        score -= 1
            if max_runtime and r["runtime_sec"]:
                if r["runtime_sec"] <= max_runtime:
                    score += 1
                    reasons.append(f"{round(r['runtime_sec'] / 60)} minutes")
                else:
                    continue
            if familiarity == "rediscover":
                if not r["last_played"]:
                    score += 1.5
                    reasons.append("Not played here yet")
                elif r["last_played"] < cutoff:
                    score += 1
                    reasons.append(f"Last played {r['last_played'][:10]}")
                else:
                    score -= 2
            elif familiarity == "favorite":
                if r["favorite"]:
                    score += 2
                    reasons.append("A favorite")
                if r["full_listens"]:
                    score += min(r["full_listens"], 5) * 0.3
                    reasons.append(f"{r['full_listens']} full listens")
            if r["genre"]:
                reasons.append(r["genre"])
            scored.append((score, r, reasons))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {**r, "reason": " · ".join(reasons) if reasons else "In a changer and ready to play"}
            for _, r, reasons in scored[:count]
        ]


def _cat_forms(s: str) -> list[str]:
    from .enrich import catalog_candidates
    return [c for part in s.split(",") for c in catalog_candidates(part)]


def _streaks(days: list[date], today: date) -> tuple[int, int]:
    if not days:
        return 0, 0
    longest = run = 1
    for prev, cur in zip(days, days[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        longest = max(longest, run)
    current = 0
    if days[-1] in (today, today - timedelta(days=1)):
        current = 1
        for prev, cur in zip(reversed(days[:-1]), reversed(days[1:])):
            if (cur - prev).days == 1:
                current += 1
            else:
                break
    return current, longest
