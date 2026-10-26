"""Loading discs into the changers.

The loop is built for speed: pick a changer, then for each disc scan its barcode
(or type a catalog number, artist or title), and the app answers with the slot to
put it in. A barcode also pins the exact pressing: when it matches the release the
album is already matched to, the pressing is locked on the spot; when it doesn't,
the album is re-matched to the release with that barcode. Scanning a disc that was
on order marks it as arrived.
"""

from __future__ import annotations

import logging
import re
import uuid

import httpx

from .db import Database
from .enrich import Enricher, clean_title, norm_catno, release_summary, similar
from .library import SLOTS_PER_CHANGER, Library, LocationError

log = logging.getLogger(__name__)


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "").lstrip("0")


class OnboardError(ValueError):
    pass


# Formats offered when adding a disc that isn't in the catalog, and whether each is audio or video.
NEW_FORMATS = {"CD": "audio", "SACD": "audio", "DVD-Audio": "audio", "Blu-ray Audio": "audio",
               "DVD": "video", "Blu-ray": "video", "4K Blu-ray": "video"}


def guess_format(formats: list[str] | str | None, title: str = "") -> str:
    """Map MusicBrainz / Discogs format words to one of NEW_FORMATS."""
    words = " ".join(formats) if isinstance(formats, list) else (formats or "")
    w = f"{words} {title}".lower()
    if "dvd-audio" in w or "dvd audio" in w:
        return "DVD-Audio"
    if "blu" in w:
        if "pure audio" in w or "blu-ray audio" in w:
            return "Blu-ray Audio"
        return "4K Blu-ray" if ("4k" in w or "uhd" in w or "ultra hd" in w) else "Blu-ray"
    if "dvd" in w:
        return "DVD"
    if "sacd" in w or "hybrid" in w:
        return "SACD"
    return "CD"


class Onboarding:
    def __init__(self, db: Database, library: Library, enricher: Enricher, prefs=None, discogs_factory=None):
        self.db, self.library, self.enricher = db, library, enricher
        self.prefs = prefs
        self.discogs_factory = discogs_factory or (lambda: httpx.AsyncClient(timeout=httpx.Timeout(20, connect=8)))

    # --- what is this disc? -----------------------------------------------------------
    def _summary(self, a: dict, reason: str) -> dict:
        discs = self.db.query("SELECT id, disc_number, location_type, changer_id, slot FROM discs WHERE album_id = ? "
                              "ORDER BY disc_number", (a["id"],))
        return {"id": a["id"], "artist": a["artist"], "title": a["title"], "year": a.get("year"),
                "format": a.get("format"), "art_url": a.get("art_url"), "status": a["status"],
                "catalog": a.get("catalog_verified") or a.get("catalog_no"), "discs": discs,
                "placed": bool(discs) and all(d["location_type"] in ("changer", "to_pull") for d in discs),
                "pressing_locked": a.get("pressing_locked"), "reason": reason}

    async def identify(self, code: str) -> dict:
        code = (code or "").strip()
        if not code:
            return {"kind": "empty", "matches": []}
        digits = _digits(code)
        if re.fullmatch(r"[\d\s-]{8,18}", code) and 7 <= len(digits) <= 14:
            return await self._by_barcode(digits)
        return {"kind": "text", "matches": self._by_text(code)}

    async def _by_barcode(self, digits: str) -> dict:
        rows = self.db.query(
            """SELECT * FROM albums WHERE removed_at IS NULL AND (barcode IS NOT NULL OR rel_barcode IS NOT NULL)""")
        hits = [self._summary(a, "barcode") for a in rows
                if digits in (_digits(a["barcode"]), _digits(a["rel_barcode"]))]
        if hits:
            return {"kind": "barcode", "code": digits, "matches": hits}
        # Not on any matched release: ask MusicBrainz what it is, then find it in the library by name.
        try:
            async with self.enricher.client_factory() as client:
                from .enrich import MusicBrainz
                mb = MusicBrainz(client, self.enricher.mb_interval)
                releases = await mb.search_pressings("", "", None, digits)
        except httpx.HTTPError as exc:
            log.info("barcode lookup failed: %s", exc)
            return {"kind": "barcode", "code": digits, "matches": [], "lookup_failed": True}
        if not releases:
            return {"kind": "barcode", "code": digits, "matches": [], "release": await self._discogs_release(digits)}
        rel = release_summary(releases[0])
        artist = rel.get("artist") or ""
        title = clean_title(releases[0].get("title") or "")
        library = self.db.query("SELECT * FROM albums WHERE removed_at IS NULL")
        scored = sorted(((similar(a["artist"], artist) + similar(clean_title(a["title"]), title), a) for a in library),
                        key=lambda x: -x[0])
        hits = [self._summary(a, "name") for s, a in scored[:5] if s >= 1.6]
        date = rel.get("date") or ""
        return {"kind": "barcode", "code": digits, "matches": hits,
                "release": {"artist": artist, "title": releases[0].get("title"), "id": releases[0].get("id"),
                            "source": "musicbrainz", "year": int(date[:4]) if date[:4].isdigit() else None,
                            "catno": (rel.get("catnos") or [None])[0],
                            "disc_count": max(1, len(releases[0].get("media") or [])),
                            "suggested_format": guess_format(rel.get("format"), releases[0].get("title") or ""),
                            **{k: rel.get(k) for k in ("label", "country", "date", "format")}}}

    async def _discogs_release(self, digits: str) -> dict | None:
        """MusicBrainz didn't know the barcode: ask Discogs, which also lists concert and music video discs."""
        token = self.prefs.get("discogs_token") if self.prefs else ""
        if not token:
            return None
        from .discogs import Discogs, DiscogsAuthError
        try:
            async with self.discogs_factory() as client:
                hit = await Discogs(client, token).lookup_barcode(digits)
        except (httpx.HTTPError, DiscogsAuthError) as exc:
            log.info("Discogs barcode lookup failed: %s", exc)
            return None
        if not hit:
            return None
        return {"artist": hit["artist"], "title": hit["title"], "id": hit["id"], "source": "discogs",
                "year": hit["year"], "label": hit["label"], "catno": hit["catno"], "country": hit["country"],
                "format": ", ".join(hit["formats"]), "cover": hit["cover"], "disc_count": 1,
                "suggested_format": guess_format(hit["formats"], hit["title"])}

    def _by_text(self, q: str) -> list[dict]:
        want = norm_catno(q)
        out, seen = [], set()
        if len(want) >= 4 and re.search(r"\d", want):
            for a in self.db.query("SELECT * FROM albums WHERE removed_at IS NULL AND enrich_status != 'skipped'"):
                cats = " ".join(filter(None, [a["catalog_no"], a["catalog_verified"], a["rel_catno"]]))
                if want in norm_catno(cats):
                    out.append(self._summary(a, "catalog"))
                    seen.add(a["id"])
        for a in self.library.albums(q=q, media_kind=None, status=None, limit=25):
            if a["id"] not in seen and a["status"] in ("owned", "on_order") and a["enrich_status"] != "skipped":
                out.append(self._summary(a, "name"))
                seen.add(a["id"])
        out.sort(key=lambda m: (m["placed"], m["reason"] != "catalog"))
        return out[:25]

    # --- put it in a slot ---------------------------------------------------------------
    def status(self, changer_id: int) -> dict:
        if changer_id not in self.library.changer_ids:
            raise OnboardError("Pick one of the configured changers.")
        blocked = self.db.one("SELECT COUNT(*) AS n FROM slot_blocks WHERE changer_id = ?", (changer_id,))["n"]
        used = self.library._used(changer_id)
        unplaced = self.db.one(
            """SELECT COUNT(DISTINCT a.id) AS albums, COUNT(*) AS discs FROM discs d JOIN albums a ON a.id = d.album_id
               WHERE a.removed_at IS NULL AND a.status IN ('owned', 'on_order') AND a.enrich_status != 'skipped'
                 AND d.location_type NOT IN ('changer', 'to_pull')""")
        nxt = self.library.free_slot(changer_id)
        return {"changer_id": changer_id, "next_slot": nxt["slot"] if nxt else None,
                "free": SLOTS_PER_CHANGER - used - blocked, "used": used, "skipped": blocked,
                "unplaced_albums": unplaced["albums"], "unplaced_discs": unplaced["discs"]}

    async def load(self, album_id: int, changer_id: int, barcode: str | None = None) -> dict:
        a = self.db.one("SELECT * FROM albums WHERE id = ? AND removed_at IS NULL", (album_id,))
        if not a:
            raise OnboardError("Album not found.")
        if a["enrich_status"] == "skipped":
            raise OnboardError(f"{a['title']} is a lot of several discs. Split it into its albums in the spreadsheet first.")
        if changer_id not in self.library.changer_ids:
            raise OnboardError("Pick one of the configured changers.")
        discs = self.db.query("SELECT * FROM discs WHERE album_id = ? ORDER BY disc_number", (album_id,))
        todo = [d for d in discs if d["location_type"] not in ("changer", "to_pull")]
        if not todo:
            where = "; ".join(f"disc {d['disc_number']}: changer {d['changer_id']} slot {d['slot']}" for d in discs)
            raise OnboardError(f"{a['title']} is already loaded ({where}).")
        free = self.status(changer_id)["free"]
        if free < len(todo):
            raise OnboardError(f"Only {free} free slot{'s' if free != 1 else ''} left in this changer; "
                               f"{a['title']} needs {len(todo)}.")
        assignments = []
        for d in todo:
            slot = self.library.free_slot(changer_id)
            self.library.set_location(d["id"], "changer", changer_id, slot["slot"])
            assignments.append({"disc_id": d["id"], "disc_number": d["disc_number"], "changer_id": changer_id,
                                "slot": slot["slot"]})
        arrived = a["status"] == "on_order"
        if arrived:
            self.db.execute("UPDATE albums SET status = 'owned', status_override = 'owned' WHERE id = ?", (album_id,))
        pressing = await self._pressing(a, _digits(barcode)) if barcode else None
        return {"album_id": album_id, "title": a["title"], "artist": a["artist"], "art_url": a["art_url"],
                "disc_count": len(discs), "assignments": assignments, "arrived": arrived, "pressing": pressing,
                "status": self.status(changer_id)}

    async def _pressing(self, a: dict, digits: str) -> str | None:
        """Use the barcode on the case to pin the pressing."""
        if not digits:
            return None
        if a["pressing_locked"]:
            locked = _digits(a["barcode"]) or _digits(a["rel_barcode"])
            return "kept" if not locked or locked == digits else "differs"
        if _digits(a["rel_barcode"]) == digits:
            self.db.execute(
                """UPDATE albums SET pressing_locked = 1, locked_at = datetime('now'), barcode = ?,
                   catalog_verified = COALESCE(catalog_verified, rel_catno), match_method = 'barcode' WHERE id = ?""",
                (a["rel_barcode"], a["id"]))
            return "locked"
        try:
            cands = await self.enricher.search_pressings(a["id"], None, digits)
            hit = next((c for c in cands if c["barcode_match"]), None)
            if hit:
                r = await self.enricher.lock(a["id"], hit["id"], None, digits)
                if r.get("status") == "locked":
                    self.db.execute("UPDATE albums SET match_method = 'barcode' WHERE id = ?", (a["id"],))
                    return "rematched"
        except httpx.HTTPError as exc:
            log.info("pressing lookup by barcode failed: %s", exc)
        self.db.execute("UPDATE albums SET barcode = ? WHERE id = ?", (digits, a["id"]))
        return "saved"

    async def add_new(self, d: dict, changer_id: int) -> dict:
        """Add a disc that isn't in the catalog (a concert DVD, a movie, a CD bought since the last import)
        and put it straight into a slot."""
        title = (d.get("title") or "").strip()
        fmt = d.get("format") or "CD"
        if fmt not in NEW_FORMATS:
            raise OnboardError("Pick a format.")
        kind = NEW_FORMATS[fmt]
        artist = (d.get("artist") or "").strip() or ("Movie" if kind == "video" else "")
        if not title or not artist:
            raise OnboardError("Enter the artist and the title.")
        discs = int(d.get("discs") or 1)
        if not 1 <= discs <= 20:
            raise OnboardError("A set can have 1 to 20 discs.")
        year = d.get("year")
        year = int(year) if str(year or "").isdigit() and 1900 <= int(year) <= 2100 else None
        digits = _digits(d.get("barcode"))
        if digits:
            dup = next((a for a in self.db.query(
                "SELECT id, artist, title, barcode, rel_barcode FROM albums WHERE removed_at IS NULL")
                if digits in (_digits(a["barcode"]), _digits(a["rel_barcode"]))), None)
            if dup:
                raise OnboardError(f"That barcode is already in your library: {dup['artist']} - {dup['title']}.")
        if changer_id not in self.library.changer_ids:
            raise OnboardError("Pick one of the configured changers.")
        if self.status(changer_id)["free"] < discs:
            raise OnboardError("Not enough free slots left in this changer.")
        catalog = (d.get("catno") or "").strip() or None
        source = d.get("source")
        key = f"manual|{digits or uuid.uuid4().hex}"
        with self.db.connect() as con:
            cur = con.execute(
                """INSERT INTO albums (import_key, artist, title, year, label, catalog_no, format, media_kind, status,
                       source_sheet, barcode, catalog_verified, enrich_status, pressing_locked, match_method,
                       discogs_release, mbid_release, art_url, edition_notes)
                   VALUES (?,?,?,?,?,?,?,?, 'owned', 'Added in app', ?,?,?,?,?,?,?,?,?)""",
                (key, artist, title, year, (d.get("label") or None), catalog, fmt, kind, digits or None, catalog,
                 "pending" if kind == "audio" else "manual", 0 if kind == "audio" else 1,
                 "barcode" if digits else "manual",
                 str(d["release_id"]) if source == "discogs" and d.get("release_id") else None,
                 str(d["release_id"]) if source == "musicbrainz" and d.get("release_id") else None,
                 d.get("cover") if kind == "video" else None,
                 "Added while loading discs"))
            album_id = cur.lastrowid
            for n in range(1, discs + 1):
                con.execute("INSERT INTO discs (album_id, disc_number) VALUES (?, ?)", (album_id, n))
        out = await self.load(album_id, changer_id)
        out["added"] = True
        return out

    def undo(self, disc_ids: list[int]) -> int:
        n = 0
        for disc_id in disc_ids:
            d = self.db.one("SELECT * FROM discs WHERE id = ?", (disc_id,))
            if d and d["location_type"] == "changer":
                self.library.set_location(disc_id, "unassigned")
                n += 1
        return n

    def skip(self, changer_id: int, slot: int, note: str | None) -> dict:
        if changer_id not in self.library.changer_ids or not 1 <= slot <= SLOTS_PER_CHANGER:
            raise OnboardError("Pick a valid changer and slot.")
        if self.db.one("SELECT 1 FROM discs WHERE changer_id = ? AND slot = ?", (changer_id, slot)):
            raise OnboardError(f"Slot {slot} already has a disc recorded in it.")
        self.db.execute("INSERT OR REPLACE INTO slot_blocks (changer_id, slot, note) VALUES (?, ?, ?)",
                        (changer_id, slot, (note or "").strip() or None))
        return self.status(changer_id)

    def unskip(self, changer_id: int, slot: int) -> dict:
        self.db.execute("DELETE FROM slot_blocks WHERE changer_id = ? AND slot = ?", (changer_id, slot))
        return self.status(changer_id)

    def skipped(self) -> list[dict]:
        return self.db.query("SELECT changer_id, slot, note, created_at FROM slot_blocks ORDER BY changer_id, slot")


__all__ = ["Onboarding", "OnboardError", "LocationError"]
