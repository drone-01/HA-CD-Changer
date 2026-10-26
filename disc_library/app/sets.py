"""Multi-disc sets.

The spreadsheet lists each disc of a set as its own row ("All Eyez on Me — Book 1",
"... — Book 2"). The app treats a set as one album with one disc row per disc, and
each disc keeps its own location, so the discs of one set can sit in different slots
and on different changers.
"""

from __future__ import annotations

import re

from .db import Database

_NUM = r"(\d+|one|two|three|four|five|six|iv|v|vi|iii|ii|i)"
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "a": 1, "b": 2, "c": 3, "d": 4}
# "— Disc 1", " - Book 2", "(Disc One)", "[CD 2]", "— Disc B"; introduced by a dash or bracket so a
# title like "HIStory: Past, Present and Future, Book I" keeps its name; must end the title.
_PART = re.compile(
    rf"\s*(?:[—–-]\s*|[\(\[]\s*)\b(?:(?:disc|cd|book)\s+{_NUM}|disc\s+([a-d]))\b[^\)\]—–]*[\)\]]?\s*$",
    re.I,
)
# "U.S. 2-CD set — DISC 2", "2-CD set, Disc One"
_NOTES = re.compile(rf"\bset\b.{{0,40}}?\bdisc\s+{_NUM}\b|\bdisc\s+{_NUM}\b.{{0,40}}?\b\d-cd set\b", re.I)


def disc_part(title: str) -> tuple[str, int] | None:
    """'All Eyez on Me — Book 2' -> ('All Eyez on Me', 2); None when the title names no disc."""
    m = _PART.search(title or "")
    if not m:
        return None
    base = title[: m.start()].strip(" —–-")
    if not base:
        return None
    v = (m.group(1) or m.group(2) or "").lower()
    n = int(v) if v.isdigit() else _WORDS.get(v)
    if not n or n > 20:
        return None
    return base, n


def disc_from_notes(notes: str | None) -> int | None:
    m = _NOTES.search(notes or "")
    if not m:
        return None
    v = (m.group(1) or m.group(2) or "").lower()
    n = int(v) if v.isdigit() else _WORDS.get(v)
    return n if n and n <= 20 else None


def set_member(title: str, notes: str | None) -> tuple[str, int] | None:
    """(base title, disc number) when a spreadsheet row is one disc of a set."""
    part = disc_part(title)
    if part:
        return part
    n = disc_from_notes(notes)
    return (title.strip(), n) if n else None


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def set_key(artist: str, base_title: str, fmt: str) -> str:
    return "set|" + "|".join([_norm(artist), _norm(base_title), _norm(fmt)])


def merge_sets(db: Database) -> list[int]:
    """Fold albums imported one-row-per-disc into one album per set. Returns the set album ids changed.

    Safe to run on every start: albums already merged carry a set| import key and are left alone.
    """
    rows = db.query(
        """SELECT id, artist, title, format, import_key, edition_notes, paid, paid_source, value, value_source, created_at
           FROM albums WHERE removed_at IS NULL AND import_key NOT LIKE 'set|%' ORDER BY id""")
    groups: dict[str, list[tuple[int, dict, str]]] = {}
    for r in rows:
        part = set_member(r["title"], r["edition_notes"])
        if part:
            base, n = part
            groups.setdefault(set_key(r["artist"], base, r["format"]), []).append((n, r, base))
    changed = []
    with db.connect() as con:
        for key, members in groups.items():
            members.sort(key=lambda m: (m[0], m[1]["id"]))
            existing = con.execute("SELECT id FROM albums WHERE import_key = ?", (key,)).fetchone()
            primary_id = existing["id"] if existing else members[0][1]["id"]
            base = members[0][2]
            taken = {d["disc_number"] for d in con.execute(
                "SELECT disc_number FROM discs WHERE album_id = ?", (primary_id,)).fetchall()} if existing else set()
            paid = next((m[1]["paid"] for m in members if m[1]["paid"] is not None), None)
            paid_src = next((m[1]["paid_source"] for m in members if m[1]["paid"] is not None), None)
            value = next((m[1]["value"] for m in members if m[1]["value"] is not None), None)
            value_src = next((m[1]["value_source"] for m in members if m[1]["value"] is not None), None)
            for n, r, _ in members:
                if n in taken:
                    continue  # a second "Disc 1" for the same set: leave that row as its own album
                taken.add(n)
                con.execute("UPDATE discs SET album_id = ?, disc_number = ? WHERE album_id = ? AND disc_number = 1",
                            (primary_id, n, r["id"]) if r["id"] != primary_id else (primary_id, n, primary_id))
                if r["id"] != primary_id:
                    con.execute("UPDATE sessions SET album_id = ? WHERE album_id = ?", (primary_id, r["id"]))
                    con.execute("DELETE FROM tracks WHERE album_id = ?", (r["id"],))
                    con.execute("DELETE FROM albums WHERE id = ?", (r["id"],))
            con.execute("DELETE FROM tracks WHERE album_id = ?", (primary_id,))
            con.execute(
                """UPDATE albums SET title = ?, import_key = ?, paid = COALESCE(paid, ?), paid_source = COALESCE(paid_source, ?),
                   value = COALESCE(value, ?), value_source = COALESCE(value_source, ?),
                   enrich_status = CASE WHEN enrich_status = 'skipped' THEN 'skipped' ELSE 'pending' END,
                   pressing_locked = 0, runtime_sec = NULL, updated_at = datetime('now') WHERE id = ?""",
                (base, key, paid, paid_src, value, value_src, primary_id))
            changed.append(primary_id)
    return changed
