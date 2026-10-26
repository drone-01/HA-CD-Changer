"""Album metadata: cover art, track lists, runtimes and genres.

Sources (free, no account needed):
  * MusicBrainz (musicbrainz.org) for the release, track list and genres.
    Its rules: a descriptive User-Agent and no more than one request a second.
  * Cover Art Archive (coverartarchive.org) for the cover, saved locally so
    the grid loads fast and works without internet.

Matching uses the spreadsheet's artist, title and catalog number. A catalog
number match (including both numbers on BMG/Columbia House club editions)
pins the exact pressing; otherwise the best-scoring CD release wins. Anything
uncertain is left unmatched rather than guessed, and any album can be fixed
by pasting its MusicBrainz link.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from .db import Database

log = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
APPLE_SEARCH = "https://itunes.apple.com/search"
USER_AGENT = "DiscLibrary/0.3 ( https://github.com/drone-01/HA-CD-Changer )"
MIN_SCORE = 105          # see pick_release(); a clean artist+title hit scores ~135
MBID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# --- cleaning ---------------------------------------------------------------------
_NOTE_WORDS = r"disc|cd\s*\d|cd single|single|ep|untitled|copy|edition|blu-?ray|dvd|version|remaster|reissue|club|bmg|columbia house|import|promo"


def clean_title(title: str) -> str:
    """'Live from Manchester — Yellow/Gold Disc' -> 'Live from Manchester'."""
    t = re.split(r"\s+[—–]\s+|\s+-\s+(?=(?:disc|cd)\b)", title, maxsplit=1, flags=re.I)[0]
    t = re.sub(rf"\s*[\(\[][^\)\]]*\b(?:{_NOTE_WORDS})\b[^\)\]]*[\)\]]\s*$", "", t, flags=re.I)
    return t.strip() or title.strip()


_DISC_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "i": 1, "ii": 2, "iii": 3, "iv": 4,
               "a": 1, "b": 2, "c": 3, "d": 4}


def disc_number_hint(title: str) -> int:
    """'— Disc 2', 'Disc II', 'Disc B', '(Disc One)', '— Book 2' -> 2 (or 1)."""
    m = re.search(r"\b(?:disc|cd|book)\s+(\d+|one|two|three|four|iv|iii|ii|i|[a-d])\b", title, re.I)
    if not m:
        return 1
    v = m.group(1).lower()
    return int(v) if v.isdigit() else _DISC_WORDS.get(v, 1)


def should_skip(album: dict) -> str | None:
    if re.match(r"\s*lot of\b", album["title"], re.I):
        return "a lot of several discs; split it into its albums first"
    return None


def catalog_candidates(catalog: str | None) -> list[str]:
    """'D 103309 (82596-2)' -> ['D103309', '825962']; placeholders give []."""
    if not catalog:
        return []
    parts = re.split(r"[()/,;]|\bor\b", catalog)
    out = []
    for p in parts:
        n = re.sub(r"[^0-9a-z]", "", p.lower())
        if len(n) >= 4 and re.search(r"\d", n):
            out.append(n.upper())
    return out


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"^the\s+|[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# --- choosing a release -------------------------------------------------------------
def score_release(r: dict, artist: str, title: str, catnos: list[str]) -> tuple[float, list[str]]:
    why = []
    score = float(r.get("score", 0))
    t_sim = similar(title, r.get("title", ""))
    credit = "".join((c.get("name") or "") + (c.get("joinphrase") or "") for c in r.get("artist-credit", []))
    a_sim = similar(artist, credit) if credit else 0.0
    if t_sim < 0.8 or (credit and a_sim < 0.6 and _norm(artist) not in ("various artists", "various")):
        return -1, ["title/artist mismatch"]
    score += 20 * t_sim + 15 * a_sim
    rel_cats = {re.sub(r"[^0-9a-z]", "", (li.get("catalog-number") or "").lower()).upper()
                for li in r.get("label-info", []) or []}
    if catnos and rel_cats & set(catnos):
        score += 60
        why.append("catalog number")
    formats = [m.get("format") or "" for m in r.get("media", []) or []]
    if any("CD" in f for f in formats):
        score += 15
    elif formats and all(f in ("Digital Media", "12\" Vinyl", "Vinyl", "Cassette") for f in formats):
        score -= 20
    if r.get("country") == "US":
        score += 5
    if r.get("status") == "Official":
        score += 5
    rg = r.get("release-group", {}) or {}
    if rg.get("primary-type") == "Album":
        score += 5
    if rg.get("secondary-types") and not re.search(r"live|greatest|best of|hits|collection", title, re.I):
        score -= 8
    return score, why


def pick_release(releases: list[dict], artist: str, title: str, catalog: str | None) -> tuple[dict | None, str]:
    catnos = catalog_candidates(catalog)
    scored = []
    for r in releases:
        s, why = score_release(r, artist, title, catnos)
        if s >= 0:
            scored.append((s, r.get("date") or "9999", r, why))
    if not scored:
        return None, "no matching release"
    scored.sort(key=lambda x: (-x[0], x[1]))
    best_score, _, best, why = scored[0]
    if best_score < MIN_SCORE:
        return None, f"best match too weak ({best_score:.0f})"
    return best, ", ".join(why) or "artist and title"


# --- genres ---------------------------------------------------------------------------
GENRE_BUCKETS = [
    ("Industrial", r"industrial"),
    ("Grunge", r"grunge"),
    ("Metal", r"metal"),
    ("Punk", r"punk|hardcore"),
    ("Hip-Hop", r"hip hop|hip-hop|rap|g-funk|trap"),
    ("R&B / Soul", r"r&b|rhythm and blues|soul|funk|new jack|motown|quiet storm"),
    ("Country", r"country|bluegrass|americana|honky"),
    ("Electronic", r"electronic|synth|techno|house|trance|dance|edm|electro|ambient|eurodance|downtempo|trip hop"),
    ("Jazz", r"jazz|bebop|swing|fusion"),
    ("Blues", r"blues"),
    ("Folk", r"folk|singer-songwriter|acoustic"),
    ("Classical", r"classical|orchestral|opera|baroque|symphon"),
    ("Soundtrack", r"soundtrack|score|musical|show tunes"),
    ("Reggae", r"reggae|ska|dub"),
    ("Alternative", r"alternative|indie|britpop|post-grunge|shoegaze|new wave|post-punk|emo|nu metal"),
    ("Pop", r"pop"),
    ("Rock", r"rock|hard rock|glam|aor|arena"),
]


def bucket_genres(genres: list[dict]) -> tuple[str | None, str | None]:
    """MusicBrainz genres (with vote counts) -> (genre bucket, most-voted genre)."""
    ranked = [g["name"] for g in sorted(genres or [], key=lambda g: -(g.get("count") or 0)) if g.get("name")]
    if not ranked:
        return None, None
    for name in ranked:
        for bucket, pattern in GENRE_BUCKETS:
            if re.search(pattern, name, re.I):
                return bucket, ranked[0].title()
    return ranked[0].title(), ranked[0].title()


# --- release details -----------------------------------------------------------------
def norm_catno(s: str | None) -> str:
    return re.sub(r"[^0-9a-z]", "", (s or "").lower()).upper()


def release_summary(r: dict) -> dict:
    """The fields that tell one pressing from another, from a search hit or a lookup."""
    labels = r.get("label-info") or []
    media = r.get("media") or []
    formats = [m.get("format") or "?" for m in media]
    fmt = ", ".join(sorted(set(formats))) if formats else None
    if len(formats) > 1:
        fmt = f"{len(formats)}×{formats[0]}" if len(set(formats)) == 1 else fmt
    tc = r.get("track-count") or sum((m.get("track-count") or len(m.get("tracks") or [])) for m in media) or None
    credit = "".join((c.get("name") or "") + (c.get("joinphrase") or "") for c in r.get("artist-credit", []) or [])
    return {
        "id": r.get("id"),
        "title": r.get("title"),
        "artist": credit or None,
        "date": r.get("date"),
        "country": r.get("country"),
        "status": r.get("status"),
        "label": next((((li.get("label") or {}).get("name")) for li in labels if (li.get("label") or {}).get("name")), None),
        "catnos": [li.get("catalog-number") for li in labels if li.get("catalog-number")],
        "barcode": r.get("barcode") or None,
        "format": fmt,
        "track_count": tc,
        "disambiguation": r.get("disambiguation") or None,
        "score": r.get("score"),
        "url": f"https://musicbrainz.org/release/{r.get('id')}",
    }


def best_apple_album(results: list[dict], artist: str, title: str) -> dict | None:
    best, best_score = None, 0.0
    for r in results:
        s = similar(title, r.get("collectionName", "")) * 0.6 + similar(artist, r.get("artistName", "")) * 0.4
        if s > best_score:
            best, best_score = r, s
    return best if best_score >= 0.8 else None


# --- HTTP -------------------------------------------------------------------------------
class MusicBrainz:
    def __init__(self, client: httpx.AsyncClient, min_interval: float = 1.1, clock=time.monotonic, sleep=asyncio.sleep):
        self.client = client
        self.min_interval = min_interval
        self.clock = clock
        self.sleep = sleep
        self._last = -1e9

    async def _get(self, path: str, params: dict) -> dict:
        for attempt in range(3):
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                await self.sleep(wait)
            self._last = self.clock()
            r = await self.client.get(f"{MB_BASE}/{path}", params={**params, "fmt": "json"},
                                      headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                      timeout=httpx.Timeout(20, connect=8))
            if r.status_code == 503:          # MusicBrainz rate limiting
                await self.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise httpx.HTTPError("MusicBrainz kept answering 503")

    async def search_releases(self, artist: str, title: str) -> list[dict]:
        q = f'release:"{_esc(title)}"'
        if _norm(artist) not in ("various artists", "various"):
            q += f' AND artist:"{_esc(artist)}"'
        return (await self._get("release/", {"query": q, "limit": 25})).get("releases", [])

    async def search_pressings(self, artist: str, title: str, catalog: str | None = None,
                               barcode: str | None = None) -> list[dict]:
        """Candidate releases for one album: by barcode, else catalog number, else artist and title."""
        digits = re.sub(r"\D", "", barcode or "")
        if digits:
            q = f"barcode:{digits}"
        elif catalog and catalog_candidates(catalog):
            forms = {catalog.strip()} | set(catalog_candidates(catalog))
            q = "(" + " OR ".join(f'catno:"{_esc(f)}"' for f in forms) + ")"
            if _norm(artist) not in ("various artists", "various"):
                q += f' AND artist:"{_esc(artist)}"'
        else:
            return await self.search_releases(artist, clean_title(title))
        return (await self._get("release/", {"query": q, "limit": 25})).get("releases", [])

    async def release(self, mbid: str) -> dict:
        return await self._get(f"release/{mbid}", {"inc": "recordings+artist-credits+labels+release-groups"})

    async def release_group(self, mbid: str) -> dict:
        return await self._get(f"release-group/{mbid}", {"inc": "genres"})


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


COVER_TIMEOUT = httpx.Timeout(10, connect=5)


async def fetch_caa_cover(client: httpx.AsyncClient, release_mbid: str, rg_mbid: str | None,
                          prefer_release: bool = False) -> bytes | None:
    """Cover Art Archive. Raises httpx errors on network trouble so the caller can stop trying."""
    urls = [f"{CAA_BASE}/release/{release_mbid}/front-500"]
    if rg_mbid:
        rg_url = f"{CAA_BASE}/release-group/{rg_mbid}/front-500"
        urls = urls + [rg_url] if prefer_release else [rg_url] + urls
    for url in urls:
        r = await client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=COVER_TIMEOUT)
        if r.status_code == 200 and r.content[:3] in (b"\xff\xd8\xff", b"\x89PN"):
            return r.content
    return None


async def fetch_apple_cover(client: httpx.AsyncClient, artist: str, title: str) -> bytes | None:
    """Apple Music artwork (US-hosted), 1200px. Matched on artist and album title."""
    try:
        r = await client.get(APPLE_SEARCH, params={"term": f"{artist} {clean_title(title)}", "entity": "album",
                                                   "media": "music", "limit": 15, "country": "US"},
                             headers={"User-Agent": USER_AGENT}, timeout=COVER_TIMEOUT)
        if r.status_code != 200:
            return None
        hit = best_apple_album(r.json().get("results", []), artist, clean_title(title))
        if not hit or not hit.get("artworkUrl100"):
            return None
        url = re.sub(r"/\d+x\d+bb\.(jpg|png)$", "/1200x1200bb.jpg", hit["artworkUrl100"])
        img = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=COVER_TIMEOUT)
        if img.status_code == 200 and img.content[:3] in (b"\xff\xd8\xff", b"\x89PN"):
            return img.content
    except (httpx.HTTPError, ValueError) as exc:
        log.info("Apple artwork failed for %s - %s: %s", artist, title, exc)
    return None


# --- the job --------------------------------------------------------------------------
@dataclass
class JobState:
    running: bool = False
    total: int = 0
    done: int = 0
    matched: int = 0
    not_found: int = 0
    errors: int = 0
    current: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    stopped_reason: str | None = None
    log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["log"] = self.log[-20:]
        return d


NETWORK_STRIKES = 3   # consecutive network failures before the job stops itself
CAA_STRIKES = 3       # consecutive Cover Art Archive failures before switching to Apple for the rest of the run


class Enricher:
    def __init__(self, db: Database, art_dir: Path, client_factory=None, mb_interval: float = 1.1, prefs=None):
        self.db = db
        self.prefs = prefs
        self.art_dir = Path(art_dir)
        self.art_dir.mkdir(parents=True, exist_ok=True)
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=30))
        self.mb_interval = mb_interval
        self.state = JobState()
        self._task: asyncio.Task | None = None
        self._caa_failures = 0

    # --- public ---------------------------------------------------------------------
    def counts(self) -> dict:
        rows = self.db.query(
            "SELECT enrich_status AS s, COUNT(*) AS n FROM albums WHERE media_kind = 'audio' AND removed_at IS NULL GROUP BY 1"
        )
        return {r["s"]: r["n"] for r in rows}

    def start(self, retry_failed: bool = False) -> JobState:
        if self.state.running:
            return self.state
        statuses = ("pending", "not_found", "error") if retry_failed else ("pending",)
        ids = [r["id"] for r in self.db.query(
            f"""SELECT id FROM albums WHERE media_kind = 'audio' AND pressing_locked = 0 AND removed_at IS NULL
                AND enrich_status IN ({','.join('?' * len(statuses))}) ORDER BY artist, year""",
            statuses,
        )]
        self.state = JobState(running=True, total=len(ids), started_at=time.time())
        self._caa_failures = 0
        self._task = asyncio.create_task(self._run(ids))
        return self.state

    def stop(self) -> JobState:
        if self._task and not self._task.done():
            self._task.cancel()
            self.state.stopped_reason = "Stopped."
        return self.state

    async def wait(self) -> None:
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def enrich_one(self, album_id: int, mbid: str | None = None) -> dict:
        async with self.client_factory() as client:
            mb = MusicBrainz(client, self.mb_interval)
            return await self._enrich(client, mb, album_id, mbid)

    async def search_pressings(self, album_id: int, catalog: str | None, barcode: str | None) -> list[dict]:
        a = self.db.one("SELECT * FROM albums WHERE id = ?", (album_id,))
        if not a:
            return []
        async with self.client_factory() as client:
            mb = MusicBrainz(client, self.mb_interval)
            hits = await mb.search_pressings(a["artist"], a["title"], catalog, barcode)
        want = set(catalog_candidates(catalog)) if catalog else set()
        want_bc = re.sub(r"\D", "", barcode or "")
        out = []
        for h in hits:
            s = release_summary(h)
            s["catalog_match"] = bool(want & {norm_catno(c) for c in s["catnos"]})
            s["barcode_match"] = bool(want_bc and re.sub(r"\D", "", s["barcode"] or "") == want_bc)
            s["is_current"] = h.get("id") == a.get("mbid_release")
            out.append(s)
        out.sort(key=lambda s: (not s["barcode_match"], not s["catalog_match"], not (s["format"] or "").count("CD"),
                                s["country"] != "US", s["date"] or "9999"))
        return out

    async def lock(self, album_id: int, mbid: str, catalog: str | None = None, barcode: str | None = None) -> dict:
        """Pin an album to one MusicBrainz release and pull everything from that exact pressing."""
        result = await self.enrich_one(album_id, mbid)
        if result["status"] not in ("manual", "matched"):
            return result
        a = self.db.one("SELECT rel_catno, rel_barcode FROM albums WHERE id = ?", (album_id,))
        self.db.execute(
            """UPDATE albums SET pressing_locked = 1, locked_at = datetime('now'), enrich_status = 'manual',
               catalog_verified = ?, barcode = ? WHERE id = ?""",
            ((catalog or "").strip() or a["rel_catno"], re.sub(r"\D", "", barcode or "") or a["rel_barcode"], album_id),
        )
        return {**result, "status": "locked"}

    def unlock(self, album_id: int) -> None:
        self.db.execute("UPDATE albums SET pressing_locked = 0, locked_at = NULL WHERE id = ?", (album_id,))

    # --- internals --------------------------------------------------------------------
    async def _run(self, ids: list[int]) -> None:
        strikes = 0
        try:
            async with self.client_factory() as client:
                mb = MusicBrainz(client, self.mb_interval)
                for album_id in ids:
                    result = await self._enrich(client, mb, album_id)
                    self.state.done += 1
                    key = {"matched": "matched", "not_found": "not_found", "skipped": "not_found"}.get(
                        result["status"], "errors")
                    setattr(self.state, key, getattr(self.state, key) + 1)
                    strikes = strikes + 1 if (result.get("note") or "").startswith("Network error") else 0
                    if strikes >= NETWORK_STRIKES:
                        self.state.stopped_reason = (
                            "Stopped: MusicBrainz can't be reached from Home Assistant. "
                            "Check the network or firewall, then try again.")
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("enrichment job failed")
            self.state.stopped_reason = "Stopped by an unexpected error; see the app log."
        finally:
            self.state.running = False
            self.state.current = None
            self.state.finished_at = time.time()

    async def _cover(self, client: httpx.AsyncClient, a: dict, release_id: str, rg_id: str | None,
                     prefer_release: bool) -> tuple[bytes | None, str | None]:
        if self._caa_failures < CAA_STRIKES:
            try:
                img = await fetch_caa_cover(client, release_id, rg_id, prefer_release)
                self._caa_failures = 0
                if img:
                    return img, "caa"
            except httpx.HTTPError as exc:
                self._caa_failures += 1
                log.info("Cover Art Archive unreachable (%s/%s): %s", self._caa_failures, CAA_STRIKES, exc)
        if self.prefs and not self.prefs.get("apple_covers"):
            return None, None
        img = await fetch_apple_cover(client, a["artist"], a["title"])
        return (img, "apple") if img else (None, None)

    async def _enrich(self, client: httpx.AsyncClient, mb: MusicBrainz, album_id: int, mbid: str | None = None) -> dict:
        a = self.db.one("SELECT * FROM albums WHERE id = ?", (album_id,))
        if not a:
            return {"status": "error", "note": "album not found"}
        self.state.current = f"{a['artist']} - {a['title']}"
        try:
            skip = None if mbid else should_skip(a)
            if skip:
                return self._mark(album_id, "skipped", skip)
            if mbid:
                m = MBID_RE.search(mbid)
                if not m:
                    return self._mark(album_id, "error", "That isn't a MusicBrainz release link or ID.")
                release_id, why, status, method = m.group(0).lower(), "chosen by hand", "manual", "manual"
            else:
                title = clean_title(a["title"])
                hits = await mb.search_releases(a["artist"], title)
                best, why = pick_release(hits, a["artist"], title, a.get("catalog_verified") or a["catalog_no"])
                if not best:
                    return self._mark(album_id, "not_found", why)
                release_id, status = best["id"], "matched"
                method = "catalog" if "catalog" in why else "title"

            rel = await mb.release(release_id)
            rg = rel.get("release-group") or {}
            genres = []
            if rg.get("id"):
                genres = (await mb.release_group(rg["id"])).get("genres", [])
            self._store(a, rel, genres, disc_number_hint(a["title"]))
            cover, source = await self._cover(client, a, release_id, rg.get("id"), prefer_release=bool(mbid))
            art_url = None
            if cover:
                (self.art_dir / f"{album_id}.jpg").write_bytes(cover)
                art_url = f"art/{album_id}.jpg?v={int(time.time())}"
            self.db.execute(
                """UPDATE albums SET mbid_release = ?, mbid_release_group = ?, art_url = COALESCE(?, art_url),
                   art_source = COALESCE(?, art_source), match_method = ?,
                   enrich_status = ?, enrich_note = ?, enriched_at = datetime('now') WHERE id = ?""",
                (release_id, rg.get("id"), art_url, source, method, status,
                 why + ("" if cover else "; no cover art found"), album_id),
            )
            self.state.log.append(f"{a['artist']} - {a['title']}: {status} ({why})")
            return {"status": status, "note": why, "cover": bool(cover), "art_source": source}
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return self._mark(album_id, "not_found", "MusicBrainz has no release with that ID.")
            return self._mark(album_id, "error", f"MusicBrainz error {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return self._mark(album_id, "error", f"Network error: {type(exc).__name__} {exc}".strip())

    def _mark(self, album_id: int, status: str, note: str) -> dict:
        self.db.execute(
            "UPDATE albums SET enrich_status = ?, enrich_note = ?, enriched_at = datetime('now') WHERE id = ?",
            (status, note, album_id),
        )
        self.state.log.append(f"#{album_id}: {status} ({note})")
        return {"status": status, "note": note}

    def _store(self, album: dict, rel: dict, genres: list[dict], disc_hint: int) -> None:
        media = rel.get("media") or []
        medium = next((m for m in media if m.get("position") == disc_hint), media[0] if media else None)
        tracks = []
        for t in (medium or {}).get("tracks") or []:
            length = t.get("length") or (t.get("recording") or {}).get("length")
            tracks.append((t.get("position"), t.get("title"), round(length / 1000) if length else None))
        runtime = sum(d for _, _, d in tracks if d) if tracks and all(d for _, _, d in tracks) else None
        bucket, top = bucket_genres(genres)
        rg = rel.get("release-group") or {}
        first = (rg.get("first-release-date") or "")[:4]
        s = release_summary(rel)
        with self.db.connect() as con:
            con.execute("DELETE FROM tracks WHERE album_id = ?", (album["id"],))
            con.executemany(
                "INSERT INTO tracks (album_id, disc_number, number, title, duration_sec) VALUES (?, 1, ?, ?, ?)",
                [(album["id"], n, title, d) for n, title, d in tracks if n],
            )
            con.execute(
                """UPDATE albums SET runtime_sec = ?, original_year = ?,
                   genre = COALESCE(genre, ?), subgenre = COALESCE(subgenre, ?),
                   label = COALESCE(label, ?), year = COALESCE(year, ?),
                   rel_label = ?, rel_catno = ?, rel_barcode = ?, rel_country = ?, rel_date = ?,
                   rel_format = ?, rel_track_count = ? WHERE id = ?""",
                (runtime, int(first) if first.isdigit() else None, bucket, top, s["label"],
                 int(first) if first.isdigit() else None,
                 s["label"], ", ".join(s["catnos"]) or None, s["barcode"], s["country"], s["date"],
                 s["format"], s["track_count"], album["id"]),
            )
