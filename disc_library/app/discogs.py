"""Market values from Discogs.

Each album is matched to a Discogs release (barcode first, then catalog number, then
artist and title), and priced with Discogs' price suggestion for the chosen condition
(VG by default). When Discogs has no suggestion for a release, or the account can't get
suggestions, the lowest current listing is used instead and labelled as such.
Typed-in values are never overwritten. Prices refresh every 30 days.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from .db import Database
from .enrich import catalog_candidates

log = logging.getLogger(__name__)

API = "https://api.discogs.com"
USER_AGENT = "DiscLibrary/0.7 +https://github.com/drone-01/HA-CD-Changer"
CONDITIONS = ["Mint (M)", "Near Mint (NM or M-)", "Very Good Plus (VG+)", "Very Good (VG)",
              "Good Plus (G+)", "Good (G)", "Fair (F)", "Poor (P)"]
DEFAULT_CONDITION = "Very Good (VG)"
REFRESH_DAYS = 30
NETWORK_STRIKES = 3


class DiscogsAuthError(Exception):
    pass


class Discogs:
    def __init__(self, client: httpx.AsyncClient, token: str, min_interval: float = 1.05,
                 clock=time.monotonic, sleep=asyncio.sleep):
        self.client, self.token = client, token
        self.min_interval, self.clock, self.sleep = min_interval, clock, sleep
        self._last = 0.0

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        wait = self._last + self.min_interval - self.clock()
        if wait > 0:
            await self.sleep(wait)
        headers = {"User-Agent": USER_AGENT, "Authorization": f"Discogs token={self.token}"}
        for attempt in range(3):
            r = await self.client.get(API + path, params=params, headers=headers,
                                      timeout=httpx.Timeout(20, connect=8))
            self._last = self.clock()
            if r.status_code == 429 and attempt < 2:
                await self.sleep(15 * (attempt + 1))
                continue
            if r.status_code == 401:
                raise DiscogsAuthError("Discogs didn't accept the token.")
            return r
        return r

    async def identity(self) -> str:
        r = await self._get("/oauth/identity")
        r.raise_for_status()
        return r.json().get("username") or ""

    async def find_release(self, album: dict) -> dict | None:
        fmt = "Blu-ray" if "blu" in (album.get("format") or "").lower() else \
              "DVD" if album.get("media_kind") == "video" and "dvd" in (album.get("format") or "").lower() else "CD"
        tries: list[dict] = []
        barcode = re.sub(r"\D", "", album.get("barcode") or album.get("rel_barcode") or "")
        if len(barcode) >= 8:
            tries.append({"barcode": barcode})
        cat = album.get("catalog_verified") or album.get("catalog_no") or ""
        for part in [p.strip() for p in re.split(r"[()/,;]|\bset:?", cat) if p.strip()]:
            if catalog_candidates(part):
                tries.append({"catno": part, "artist": album["artist"]})
        if album.get("rel_catno"):
            for part in album["rel_catno"].split(","):
                if part.strip():
                    tries.append({"catno": part.strip(), "artist": album["artist"]})
        tries.append({"artist": album["artist"], "release_title": album["title"], "format": fmt})
        seen = set()
        for q in tries:
            key = tuple(sorted(q.items()))
            if key in seen:
                continue
            seen.add(key)
            r = await self._get("/database/search", {**q, "type": "release", "per_page": 25})
            if r.status_code != 200:
                r.raise_for_status()
            results = r.json().get("results") or []
            pick = _pick(results, fmt)
            if pick:
                return {"id": pick["id"], "title": pick.get("title"), "how": next(iter(q))}
        return None

    async def price(self, release_id: int | str, condition: str) -> tuple[float | None, str, bool]:
        """(value, basis, suggestions_refused)."""
        refused = False
        r = await self._get(f"/marketplace/price_suggestions/{release_id}")
        if r.status_code == 200:
            entry = (r.json() or {}).get(condition) or {}
            if entry.get("value") is not None:
                return round(float(entry["value"]), 2), _short(condition), False
        elif r.status_code in (403, 422):
            refused = True
        r = await self._get(f"/marketplace/stats/{release_id}", {"curr_abbr": "USD"})
        if r.status_code == 200:
            low = (r.json() or {}).get("lowest_price") or {}
            if low.get("value") is not None:
                return round(float(low["value"]), 2), "lowest listing", refused
        return None, "no price on Discogs", refused


def _short(condition: str) -> str:
    m = re.search(r"\(([^)]+)\)", condition)
    return m.group(1).split(" or ")[0] if m else condition


def _pick(results: list[dict], fmt: str) -> dict | None:
    def fits(r):
        return any(fmt.lower() in f.lower() for f in r.get("format") or [])
    good = [r for r in results if fits(r)]
    if not good:
        return None
    us = [r for r in good if (r.get("country") or "").upper() in ("US", "USA")]
    return (us or good)[0]


@dataclass
class ValueJob:
    running: bool = False
    total: int = 0
    done: int = 0
    priced: int = 0
    missed: int = 0
    current: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    stopped_reason: str | None = None
    notice: str | None = None
    log: list[str] = field(default_factory=list)


class ValueUpdater:
    def __init__(self, db: Database, prefs, client_factory=None, interval: float = 1.05):
        self.db, self.prefs = db, prefs
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=30))
        self.interval = interval
        self.state = ValueJob()
        self._task: asyncio.Task | None = None

    def _due_ids(self, force: bool) -> list[int]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        cond = "" if force else "AND (a.value_at IS NULL OR a.value_at < ?)"
        params = () if force else (cutoff,)
        return [r["id"] for r in self.db.query(
            f"""SELECT a.id FROM albums a WHERE a.status = 'owned' AND a.removed_at IS NULL
                AND COALESCE(a.value_source, '') != 'manual' {cond} ORDER BY a.artist, a.title""", params)]

    def due(self) -> int:
        return len(self._due_ids(False))

    def counts(self) -> dict:
        r = self.db.one(
            """SELECT SUM(value IS NOT NULL AND value_source = 'discogs') AS discogs,
                      SUM(value_source = 'manual') AS manual,
                      SUM(value IS NULL AND value_basis = 'not found') AS not_found,
                      MAX(CASE WHEN value_source = 'discogs' THEN value_at END) AS last
               FROM albums WHERE status = 'owned' AND removed_at IS NULL""")
        return {"discogs": r["discogs"] or 0, "manual": r["manual"] or 0, "not_found": r["not_found"] or 0,
                "last_updated": r["last"], "due": self.due()}

    def start(self, force: bool = False, album_ids: list[int] | None = None) -> ValueJob:
        if self.state.running:
            return self.state
        if not self.prefs.get("discogs_token"):
            self.state = ValueJob(stopped_reason="Connect Discogs in Settings first.")
            return self.state
        ids = album_ids if album_ids is not None else self._due_ids(force)
        self.state = ValueJob(running=True, total=len(ids), started_at=time.time())
        self._task = asyncio.create_task(self._run(ids))
        return self.state

    def stop(self) -> ValueJob:
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

    async def _run(self, ids: list[int]) -> None:
        strikes = 0
        refused = 0
        condition = self.prefs.get("discogs_condition") or DEFAULT_CONDITION
        try:
            async with self.client_factory() as client:
                dg = Discogs(client, self.prefs.get("discogs_token"), self.interval)
                for album_id in ids:
                    a = self.db.one("SELECT * FROM albums WHERE id = ?", (album_id,))
                    if not a:
                        continue
                    self.state.current = f"{a['artist']} - {a['title']}"
                    try:
                        refused += await self._one(dg, a, condition)
                        strikes = 0
                    except DiscogsAuthError as exc:
                        self.state.stopped_reason = f"{exc} Reconnect Discogs in Settings."
                        break
                    except httpx.HTTPError as exc:
                        strikes += 1
                        self.state.log.append(f"{a['artist']} - {a['title']}: {type(exc).__name__}")
                        if strikes >= NETWORK_STRIKES:
                            self.state.stopped_reason = ("Can't reach Discogs (api.discogs.com). Check the network "
                                                         "and try again.")
                            break
                    self.state.done += 1
                if refused:
                    self.state.notice = ("Discogs gave lowest listings instead of condition prices. Filling in your "
                                         "Discogs seller settings (currency is enough) lets it give condition prices.")
        except asyncio.CancelledError:
            raise
        except Exception:  # never take the app down
            log.exception("Value update failed")
            self.state.stopped_reason = "Stopped by an unexpected error; see the app log."
        finally:
            self.state.running = False
            self.state.current = None
            self.state.finished_at = time.time()

    async def _one(self, dg: Discogs, a: dict, condition: str) -> int:
        release = a["discogs_release"]
        if not release:
            hit = await dg.find_release(a)
            if not hit:
                self.db.execute("""UPDATE albums SET value_basis = 'not found', value_at = datetime('now')
                                   WHERE id = ? AND COALESCE(value_source, '') != 'manual'""", (a["id"],))
                self.state.missed += 1
                return 0
            release = str(hit["id"])
            self.db.execute("UPDATE albums SET discogs_release = ? WHERE id = ?", (release, a["id"]))
        value, basis, refused = await dg.price(release, condition)
        self.db.execute(
            """UPDATE albums SET value = ?, value_source = CASE WHEN ? IS NULL THEN NULL ELSE 'discogs' END,
               value_basis = ?, value_at = datetime('now') WHERE id = ? AND COALESCE(value_source, '') != 'manual'""",
            (value, value, basis, a["id"]))
        if value is None:
            self.state.missed += 1
        else:
            self.state.priced += 1
        return int(refused)
