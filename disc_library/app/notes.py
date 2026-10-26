"""Liner notes: who made the record, and a short background.

Fetched when an album is opened and kept for 90 days:
- credits from the matched MusicBrainz release (producer, engineer, mixing, mastering, and the
  players with their instruments, from release- and recording-level relationships);
- a short background from Wikipedia, found through the release group's Wikidata/Wikipedia link,
  shown with a link back to the article.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote

import httpx

from .db import Database
from .enrich import USER_AGENT, Enricher, MusicBrainz

log = logging.getLogger(__name__)
KEEP_DAYS = 90

# MusicBrainz relationship types -> how they read on a sleeve
PRODUCTION = {
    "producer": "Produced by", "co-producer": "Produced by", "executive producer": "Executive producer",
    "engineer": "Engineered by", "recording": "Recorded by", "audio": "Engineered by", "sound": "Engineered by",
    "mix": "Mixed by", "mastering": "Mastered by", "arranger": "Arranged by", "orchestrator": "Arranged by",
    "programming": "Programming", "art direction": "Art direction", "design": "Design", "photography": "Photography",
}
PLAYING = {"instrument", "vocal", "performer", "performing orchestra", "conductor", "chorus master"}


def _credit_name(rel: dict) -> str | None:
    art = rel.get("artist") or {}
    return rel.get("target-credit") or art.get("name")


def _role(rel: dict) -> str:
    t = rel.get("type") or ""
    attrs = [a for a in rel.get("attributes") or [] if a not in ("additional", "guest", "solo", "co")]
    if t == "instrument":
        return ", ".join(attrs) or "instruments"
    if t == "vocal":
        return ", ".join(attrs) if attrs else "vocals"
    if t == "conductor":
        return "conductor"
    return ", ".join(attrs) or t


def credits_from_release(rel: dict) -> dict:
    production: dict[str, list[str]] = {}
    players: dict[str, set[str]] = {}

    def take(relations):
        for r in relations or []:
            if r.get("target-type") != "artist":
                continue
            name = _credit_name(r)
            if not name:
                continue
            t = r.get("type") or ""
            if t in PRODUCTION:
                label = PRODUCTION[t]
                if name not in production.setdefault(label, []):
                    production[label].append(name)
            elif t in PLAYING:
                players.setdefault(name, set()).add(_role(r))

    take(rel.get("relations"))
    for medium in rel.get("media") or []:
        for track in medium.get("tracks") or []:
            take((track.get("recording") or {}).get("relations"))
    order = list(dict.fromkeys(PRODUCTION.values()))
    return {
        "production": [{"role": k, "names": production[k]} for k in order if k in production],
        "personnel": [{"name": n, "roles": sorted(r)} for n, r in sorted(players.items(), key=lambda kv: kv[0].lower())],
    }


class LinerNotes:
    def __init__(self, db: Database, enricher: Enricher):
        self.db, self.enricher = db, enricher

    async def get(self, album_id: int, refresh: bool = False) -> dict:
        row = self.db.one("SELECT data, fetched_at FROM album_notes WHERE album_id = ?", (album_id,))
        if row and not refresh:
            fetched = datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - fetched < timedelta(days=KEEP_DAYS):
                return {**json.loads(row["data"]), "cached": True}
        a = self.db.one("SELECT * FROM albums WHERE id = ?", (album_id,))
        if not a or not a["mbid_release"]:
            return {"available": False, "reason": "Liner notes appear once the album is matched to a release."}
        try:
            data = await self._fetch(a)
        except httpx.HTTPError as exc:
            log.info("liner notes failed for %s: %s", album_id, exc)
            if row:
                return {**json.loads(row["data"]), "cached": True, "stale": True}
            return {"available": False, "reason": "Couldn't reach MusicBrainz just now. Try again later."}
        self.db.execute("INSERT OR REPLACE INTO album_notes (album_id, data, fetched_at) VALUES (?, ?, datetime('now'))",
                        (album_id, json.dumps(data)))
        return data

    async def _fetch(self, a: dict) -> dict:
        async with self.enricher.client_factory() as client:
            mb = MusicBrainz(client, self.enricher.mb_interval)
            rel = await mb._get(f"release/{a['mbid_release']}",
                                {"inc": "artist-rels+recordings+recording-level-rels+release-groups"})
            credits = credits_from_release(rel)
            rg_id = a["mbid_release_group"] or (rel.get("release-group") or {}).get("id")
            background = None
            if rg_id:
                rg = await mb._get(f"release-group/{rg_id}", {"inc": "url-rels"})
                background = await self._wikipedia(client, rg.get("relations") or [])
        return {"available": True, **credits, "background": background,
                "source": f"https://musicbrainz.org/release/{a['mbid_release']}"}

    async def _wikipedia(self, client: httpx.AsyncClient, relations: list[dict]) -> dict | None:
        headers = {"User-Agent": USER_AGENT}
        title = None
        for r in relations:
            url = (r.get("url") or {}).get("resource") or ""
            m = re.match(r"https?://en\.wikipedia\.org/wiki/(.+)", url)
            if m:
                title = unquote(m.group(1))
                break
        if not title:
            qid = next((re.search(r"(Q\d+)", (r.get("url") or {}).get("resource") or "").group(1)
                        for r in relations if r.get("type") == "wikidata"
                        and re.search(r"Q\d+", (r.get("url") or {}).get("resource") or "")), None)
            if qid:
                w = await client.get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json",
                                     headers=headers, timeout=15)
                if w.status_code == 200:
                    title = (((w.json().get("entities") or {}).get(qid) or {}).get("sitelinks") or {}) \
                        .get("enwiki", {}).get("title")
        if not title:
            return None
        s = await client.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'))}",
                             headers=headers, timeout=15)
        if s.status_code != 200:
            return None
        j = s.json()
        if j.get("type") == "disambiguation" or not j.get("extract"):
            return None
        return {"text": j["extract"], "title": j.get("title") or title,
                "url": ((j.get("content_urls") or {}).get("desktop") or {}).get("page")
                or f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"}
