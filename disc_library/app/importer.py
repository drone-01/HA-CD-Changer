"""Import CD_Collection.xlsx into the library.

Matches columns by header name, so it copes with the three tabs (CD
Collection, Non-CD Formats, Video) and with columns being reordered.
Re-running it is safe: catalog fields are refreshed, while locations,
genres, favorites, art and history are left alone.
"""

from __future__ import annotations

import re
from pathlib import Path

from openpyxl import load_workbook

from .db import Database

# header text (lower-case, trimmed) -> field
HEADER_MAP = {
    "artist": "artist",
    "album": "title",
    "title": "title",
    "format": "format",
    "year": "year",
    "label": "label",
    "status": "status",
    "edition / notes": "notes",
    "notes": "notes",
    "edition": "notes",
}

STATUS_MAP = {
    "in collection": "owned",
    "owned": "owned",
    "on order": "on_order",
}


def _field_for(header: str) -> str | None:
    h = header.strip().lower()
    if "catalog" in h:
        return "catalog_no"
    return HEADER_MAP.get(h)


def _year(value) -> int | None:
    if value is None:
        return None
    m = re.search(r"(19|20)\d{2}", str(value))
    return int(m.group(0)) if m else None


def _clean(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def import_key(artist: str, title: str, fmt: str, catalog: str | None) -> str:
    norm = lambda s: re.sub(r"\s+", " ", (s or "").strip().lower())
    return "|".join([norm(artist), norm(title), norm(fmt), norm(catalog)])


def parse_workbook(path: Path | str) -> list[dict]:
    wb = load_workbook(path, read_only=True, data_only=True)
    rows: list[dict] = []
    for ws in wb.worksheets:
        header_idx = None
        columns: dict[int, str] = {}
        for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
            if header_idx is None:
                labels = [str(v).strip().lower() if v is not None else "" for v in row]
                if "artist" in labels:
                    header_idx = r_idx
                    for c_idx, v in enumerate(row):
                        if v is None:
                            continue
                        f = _field_for(str(v))
                        if f and f not in columns.values():
                            columns[c_idx] = f
                continue
            rec = {f: row[c] if c < len(row) else None for c, f in columns.items()}
            artist, title = _clean(rec.get("artist")), _clean(rec.get("title"))
            if not artist or not title:
                continue
            fmt = _clean(rec.get("format"))
            is_video = "video" in ws.title.lower() or bool(fmt and re.search(r"video|\bdvd\b", fmt, re.I))
            fmt = fmt or ("DVD/Blu-ray" if is_video else "CD")
            status_raw = (_clean(rec.get("status")) or "").lower()
            rows.append(
                {
                    "artist": artist,
                    "title": title,
                    "format": fmt,
                    "media_kind": "video" if is_video else "audio",
                    "catalog_no": _clean(rec.get("catalog_no")),
                    "year": _year(rec.get("year")),
                    "label": _clean(rec.get("label")),
                    "edition_notes": _clean(rec.get("notes")),
                    "status": STATUS_MAP.get(status_raw, "owned"),
                    "source_sheet": ws.title,
                }
            )
    wb.close()
    return rows


def import_workbook(db: Database, path: Path | str) -> dict:
    rows = parse_workbook(path)
    added = updated = 0
    by_sheet: dict[str, int] = {}
    with db.connect() as con:
        for r in rows:
            key = import_key(r["artist"], r["title"], r["format"], r["catalog_no"])
            by_sheet[r["source_sheet"]] = by_sheet.get(r["source_sheet"], 0) + 1
            existing = con.execute("SELECT id FROM albums WHERE import_key = ?", (key,)).fetchone()
            if existing:
                con.execute(
                    """UPDATE albums SET artist=?, title=?, year=?, label=?, catalog_no=?, format=?,
                       media_kind=?, edition_notes=?, status=?, source_sheet=?, updated_at=datetime('now')
                       WHERE id=?""",
                    (r["artist"], r["title"], r["year"], r["label"], r["catalog_no"], r["format"],
                     r["media_kind"], r["edition_notes"], r["status"], r["source_sheet"], existing["id"]),
                )
                updated += 1
            else:
                cur = con.execute(
                    """INSERT INTO albums (import_key, artist, title, year, label, catalog_no, format,
                       media_kind, edition_notes, status, source_sheet)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, r["artist"], r["title"], r["year"], r["label"], r["catalog_no"], r["format"],
                     r["media_kind"], r["edition_notes"], r["status"], r["source_sheet"]),
                )
                con.execute("INSERT INTO discs (album_id, disc_number) VALUES (?, 1)", (cur.lastrowid,))
                added += 1
    return {"rows": len(rows), "added": added, "updated": updated, "by_sheet": by_sheet}
