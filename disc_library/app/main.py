"""HTTP API and web app, served through Home Assistant ingress on port 8099.

Ingress handles login, so there is no auth here; the add-on exposes no
other port. Home Assistant passes the signed-in user in X-Remote-User-*
headers, which tags each listening session with who played it.
"""

from __future__ import annotations

import asyncio

import httpx
import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import Settings, load_settings
from .db import Database
from .drivers import build_driver
from .enrich import Enricher
from .ha_client import HAClient
from .importer import import_workbook
from .prefs import Prefs, PrefsError
from .library import Library, LocationError, RemoveError
from .sets import merge_sets
from .onboard import Onboarding, OnboardError
from .notes import LinerNotes
from .discogs import CONDITIONS, Discogs, DiscogsAuthError, ValueUpdater
from .player import PlayError, Player

log = logging.getLogger("disc_library")


class PlayBody(BaseModel):
    album_id: int
    from_track: int = 1
    disc: int | None = None


class LinkBody(BaseModel):
    other_id: int
    as_disc: int


class PulledBody(BaseModel):
    disc_ids: list[int]


class LoadBody(BaseModel):
    album_id: int
    changer_id: int
    barcode: str | None = None


class SkipBody(BaseModel):
    changer_id: int
    slot: int
    note: str | None = None


class ManualLockBody(BaseModel):
    catalog_no: str | None = None
    barcode: str | None = None


class LocationBody(BaseModel):
    location_type: str
    changer_id: int | None = None
    slot: int | None = None
    note: str | None = None


class FavoriteBody(BaseModel):
    favorite: bool


class TagsBody(BaseModel):
    genre: str | None = None
    subgenre: str | None = None
    mood_tags: str | None = None


class LockBody(BaseModel):
    mbid: str                           # MusicBrainz release ID or link
    catalog_no: str | None = None       # what's printed on your disc, if you typed it
    barcode: str | None = None


class EnrichBody(BaseModel):
    mbid: str | None = None             # a MusicBrainz release ID or link, to fix a wrong match


class VibeBody(BaseModel):
    mood: str | None = None
    energy: str | None = None
    time_budget: str | None = None      # under_45 | about_hour | all_evening
    familiarity: str | None = None      # favorite | rediscover
    seed: int | None = None


def _user(request: Request) -> str | None:
    return request.headers.get("X-Remote-User-Display-Name") or request.headers.get("X-Remote-User-Name")


def _since(range_: str) -> str | None:
    today = date.today()
    if range_ == "month":
        return today.replace(day=1).isoformat()
    if range_ == "year":
        return today.replace(month=1, day=1).isoformat()
    if range_ == "30d":
        return (today - timedelta(days=30)).isoformat()
    return None


def create_app(settings: Settings | None = None, drivers: dict | None = None, ha: HAClient | None = None,
               enricher: Enricher | None = None, values: ValueUpdater | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    merged_sets = merge_sets(db)  # one-row-per-disc albums from before v0.6.0 become one album per set
    # A session still open from before a restart can't be followed any more.
    db.execute("""UPDATE sessions SET ended_at = datetime('now'), end_reason = 'interrupted'
                  WHERE ended_at IS NULL""")
    drivers = drivers or {c.id: build_driver(c, settings) for c in settings.changers}
    library = Library(db, list(drivers))
    prefs = Prefs(db)
    player = Player(db, drivers, ha if ha is not None else HAClient(), settings.listen_script, prefs=prefs)
    art_dir = Path(settings.data_dir) / "art"
    enricher = enricher or Enricher(db, art_dir)
    if enricher.prefs is None:
        enricher.prefs = prefs
    values = values or ValueUpdater(db, prefs)
    onboard = Onboarding(db, library, enricher)
    notes = LinerNotes(db, enricher)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for d in drivers.values():
            await d.connect()

        async def poll_loop():
            while True:
                try:
                    await player.poll()
                except Exception:  # keep polling whatever happens
                    log.exception("poll failed")
                await asyncio.sleep(settings.poll_seconds)

        async def value_loop():
            # Keep Discogs prices fresh: check a few minutes after start, then every 6 hours.
            await asyncio.sleep(300)
            while True:
                try:
                    if prefs.get("discogs_token") and not values.state.running and values.due():
                        values.start()
                except Exception:
                    log.exception("value refresh check failed")
                await asyncio.sleep(6 * 3600)

        task = asyncio.create_task(poll_loop())
        vtask = asyncio.create_task(value_loop())
        if merged_sets:
            log.info("Combined %s multi-disc sets; looking them up again", len(merged_sets))
            enricher.start()
        yield
        task.cancel()
        vtask.cancel()
        for d in drivers.values():
            await d.close()

    app = FastAPI(title="Disc Library", lifespan=lifespan)
    app.state.db, app.state.library, app.state.player = db, library, player
    app.state.enricher = enricher

    # --- metadata (cover art, track lists, genres) -----------------------------
    @app.get("/api/enrich")
    def enrich_status():
        return {"job": enricher.state.to_dict(), "counts": enricher.counts()}

    @app.post("/api/enrich/start")
    async def enrich_start(retry: bool = False):
        enricher.start(retry_failed=retry)
        return {"job": enricher.state.to_dict(), "counts": enricher.counts()}

    @app.post("/api/albums/{album_id}/enrich")
    async def enrich_album(album_id: int, body: EnrichBody):
        result = await enricher.enrich_one(album_id, body.mbid)
        a = library.album(album_id)
        if not a:
            raise HTTPException(404, "Album not found")
        return {"result": result, "album": a}

    @app.post("/api/enrich/stop")
    def enrich_stop():
        enricher.stop()
        return {"job": enricher.state.to_dict(), "counts": enricher.counts()}

    # --- pressings: see, edit, confirm and lock the exact release you own --------
    @app.get("/api/pressings")
    def pressings(filter: str = "review"):
        counts = library.pressing_counts()
        return {"counts": counts, "albums": [] if filter == "none" else library.pressings(filter)}

    @app.get("/api/albums/{album_id}/pressings")
    async def pressing_candidates(album_id: int, catalog_no: str | None = None, barcode: str | None = None):
        try:
            return await enricher.search_pressings(album_id, catalog_no, barcode)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"MusicBrainz couldn't be reached: {type(exc).__name__}") from exc

    @app.post("/api/albums/{album_id}/lock")
    async def lock_pressing(album_id: int, body: LockBody):
        result = await enricher.lock(album_id, body.mbid, body.catalog_no, body.barcode)
        if result["status"] != "locked":
            raise HTTPException(409, result.get("note") or "Couldn't lock that release.")
        return {"result": result, "album": library.album(album_id)}

    @app.post("/api/albums/{album_id}/unlock")
    def unlock_pressing(album_id: int):
        enricher.unlock(album_id)
        return library.album(album_id)

    @app.post("/api/pressings/lock-catalog-matches")
    def lock_catalog_matches():
        ids = [r["id"] for r in library.pressings("catalog")]
        db.execute(
            f"""UPDATE albums SET pressing_locked = 1, locked_at = datetime('now'),
                catalog_verified = COALESCE(catalog_verified, catalog_no),
                barcode = COALESCE(barcode, rel_barcode)
                WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})""",
            tuple(ids),
        )
        return {"locked": len(ids), "counts": library.pressing_counts()}

    @app.get("/api/pressings/export")
    def export_pressings():
        path = Path(settings.data_dir) / "pressings-export.xlsx"
        library.export_pressings(path)
        return FileResponse(path, filename="CD_Collection-pressings.xlsx",
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/art/{name}")
    def art(name: str):
        path = (enricher.art_dir / name).resolve()
        if path.parent != enricher.art_dir.resolve() or not path.is_file():
            raise HTTPException(404, "No cover")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000"})

    # --- library ---------------------------------------------------------------
    @app.get("/api/albums")
    def albums(q: str | None = None, genre: str | None = None, favorites: bool = False,
               media: str = "audio", sort: str = "artist", limit: int = 1000, offset: int = 0,
               decade: int | None = None):
        return library.albums(q=q, genre=genre, favorites=favorites, decade=decade,
                              media_kind=None if media == "all" else media, sort=sort, limit=limit, offset=offset)

    # --- loading discs into the changers ---------------------------------------------
    def _ob(fn, *a):
        try:
            return fn(*a)
        except (OnboardError, LocationError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/onboard/status")
    def onboard_status(changer_id: int):
        return _ob(onboard.status, changer_id)

    @app.get("/api/onboard/identify")
    async def onboard_identify(code: str):
        return await onboard.identify(code)

    @app.get("/api/onboard/unplaced")
    def onboard_unplaced(q: str | None = None, limit: int = 60):
        return library.albums(q=q, media_kind=None, status=None, unplaced=True, limit=limit)

    @app.post("/api/onboard/load")
    async def onboard_load(body: LoadBody):
        try:
            return await onboard.load(body.album_id, body.changer_id, body.barcode)
        except (OnboardError, LocationError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/onboard/undo")
    def onboard_undo(body: PulledBody):
        return {"undone": _ob(onboard.undo, body.disc_ids)}

    @app.post("/api/onboard/skip")
    def onboard_skip(body: SkipBody):
        return _ob(onboard.skip, body.changer_id, body.slot, body.note)

    @app.delete("/api/onboard/skip")
    def onboard_unskip(changer_id: int, slot: int):
        return _ob(onboard.unskip, changer_id, slot)

    @app.get("/api/onboard/skipped")
    def onboard_skipped():
        return onboard.skipped()

    # --- liner notes -------------------------------------------------------------------
    @app.get("/api/albums/{album_id}/notes")
    async def album_notes(album_id: int, refresh: bool = False):
        if not library.album(album_id):
            raise HTTPException(404, "Album not found")
        return await notes.get(album_id, refresh)

    @app.get("/api/decades")
    def decades():
        return library.decades()

    @app.get("/api/albums/{album_id}/link-candidates")
    def link_candidates(album_id: int):
        a = library.album(album_id)
        if not a:
            raise HTTPException(404, "Album not found")
        return db.query(
            """SELECT a.id, a.artist, a.title, a.year, a.catalog_no FROM albums a
               WHERE a.id != ? AND a.removed_at IS NULL AND a.media_kind = ?
                 AND (SELECT COUNT(*) FROM discs d WHERE d.album_id = a.id) = 1
               ORDER BY (a.artist = ?) DESC, a.artist COLLATE NOCASE, a.title COLLATE NOCASE""",
            (album_id, a["media_kind"], a["artist"]))

    @app.post("/api/albums/{album_id}/link")
    def link_disc(album_id: int, body: LinkBody):
        try:
            return library.link_disc(album_id, body.other_id, body.as_disc)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/discs/{disc_id}/unlink")
    def unlink_disc(disc_id: int):
        try:
            return library.unlink_disc(disc_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/to-pull")
    def to_pull():
        return library.to_pull()

    @app.post("/api/to-pull/pulled")
    def pulled(body: PulledBody):
        return {"pulled": library.mark_pulled(body.disc_ids)}

    @app.post("/api/albums/{album_id}/lock-manual")
    def lock_manual(album_id: int, body: ManualLockBody):
        """Save the catalog number / barcode on the disc and lock it, when MusicBrainz has no such release."""
        cat = (body.catalog_no or "").strip() or None
        bc = "".join(ch for ch in (body.barcode or "") if ch.isdigit()) or None
        if not cat and not bc:
            raise HTTPException(400, "Type the catalog number or barcode first.")
        if not library.album(album_id):
            raise HTTPException(404, "Album not found")
        db.execute(
            """UPDATE albums SET catalog_verified = ?, barcode = COALESCE(?, barcode), pressing_locked = 1,
               locked_at = datetime('now'), match_method = 'manual', enrich_status = 'manual',
               enrich_note = 'catalog number entered by hand; not on MusicBrainz' WHERE id = ?""",
            (cat, bc, album_id))
        return library.album(album_id)

    @app.get("/api/albums/{album_id}")
    def album(album_id: int):
        a = library.album(album_id)
        if not a:
            raise HTTPException(404, "Album not found")
        return a

    @app.get("/api/albums/{album_id}/remove-preview")
    def remove_preview(album_id: int):
        try:
            return library.remove_preview(album_id)
        except RemoveError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.delete("/api/albums/{album_id}")
    def remove_album(album_id: int):
        if player.active and player.active.album_id == album_id:
            raise HTTPException(409, "This album is playing. Stop it first.")
        try:
            return library.remove(album_id)
        except RemoveError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/albums/{album_id}/restore")
    def restore_album(album_id: int):
        try:
            return library.restore(album_id)
        except RemoveError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/removed")
    def removed_albums():
        return library.removed()

    @app.put("/api/albums/{album_id}/money")
    def set_money(album_id: int, body: dict):
        if not library.album(album_id):
            raise HTTPException(404, "Album not found")
        try:
            return library.set_money(album_id, body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/collection")
    def collection():
        return library.collection({c.id: c.name for c in settings.changers})

    @app.post("/api/albums/{album_id}/favorite")
    def favorite(album_id: int, body: FavoriteBody):
        library.set_favorite(album_id, body.favorite)
        return library.album(album_id)

    @app.post("/api/albums/{album_id}/tags")
    def tags(album_id: int, body: TagsBody):
        library.set_tags(album_id, body.genre, body.subgenre, body.mood_tags)
        return library.album(album_id)

    @app.get("/api/genres")
    def genres():
        return library.genres()

    @app.post("/api/discs/{disc_id}/location")
    def set_location(disc_id: int, body: LocationBody):
        try:
            return library.set_location(disc_id, body.location_type, body.changer_id, body.slot, body.note)
        except LocationError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/slots/free")
    def free_slot(changer_id: int | None = None):
        slot = library.free_slot(changer_id)
        if not slot:
            raise HTTPException(409, "Every slot is full.")
        return slot

    @app.get("/api/changers")
    async def changers():
        out = []
        usage = {u["changer_id"]: u for u in library.slot_map()}
        for d in drivers.values():
            try:
                st = (await d.status()).to_dict()
            except NotImplementedError:
                st = {}
            out.append({"id": d.id, "name": d.name, "driver": d.cfg.driver, "status": st, **usage.get(d.id, {})})
        return out

    @app.post("/api/import")
    async def import_xlsx(file: UploadFile = File(...)):
        if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
            raise HTTPException(400, "Upload the .xlsx catalog.")
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            shutil.copyfileobj(file.file, tmp)
            path = Path(tmp.name)
        try:
            result = import_workbook(db, path)
            if result["added"] and prefs.get("auto_sync_on_import"):
                enricher.start()  # fetch covers and details for the new albums in the background
            return result
        finally:
            path.unlink(missing_ok=True)

    # --- playing ---------------------------------------------------------------
    @app.post("/api/play")
    async def play(body: PlayBody, request: Request):
        try:
            return await player.play_album(body.album_id, body.from_track, _user(request), from_disc=body.disc)
        except PlayError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/albums/{album_id}/resume")
    async def resume(album_id: int, request: Request):
        try:
            return await player.resume_album(album_id, _user(request))
        except PlayError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/random")
    async def random_album(request: Request, genre: str | None = None, decade: int | None = None,
                           favorites: bool = False):
        import random as _random
        picks = [a for a in library.albums(genre=genre, decade=decade, favorites=favorites, limit=5000)
                 if a["location_type"] == "changer"]
        if not picks:
            what = " ".join(x for x in [f"{decade}s" if decade else "", genre or "", "favorite" if favorites else ""] if x)
            raise HTTPException(409, f"No {what + ' ' if what else ''}albums are in a changer yet.")
        return await player.play_album(_random.choice(picks)["id"], 1, _user(request))

    @app.post("/api/transport/{action}")
    async def transport(action: str):
        try:
            return await player.transport(action)
        except PlayError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/now-playing")
    async def now_playing():
        return await player.now_playing()

    # --- listening -------------------------------------------------------------
    @app.get("/api/history")
    def history(limit: int = 50):
        return library.history(limit)

    @app.get("/api/continue")
    def continue_listening():
        return library.continue_listening()

    @app.get("/api/stats")
    def stats(range: str = "all"):
        return library.stats(_since(range))

    @app.post("/api/vibe")
    def vibe(body: VibeBody):
        return library.vibe(body.mood, body.energy, body.time_budget, body.familiarity, seed=body.seed)

    @app.get("/api/settings")
    async def get_settings():
        changers = []
        for c in settings.changers:
            d = drivers.get(c.id)
            try:
                st = await d.status() if d else None
                state = st.state if st else "missing"
            except Exception as exc:  # noqa: BLE001 - shown to the user, never fatal
                state = f"unreachable ({type(exc).__name__})"
            changers.append({"id": c.id, "name": c.name, "driver": c.driver, "host": c.host,
                             "port": c.port, "state": state})
        return {
            "prefs": prefs.public(),
            "conditions": CONDITIONS,
            "version": __version__,
            "configured_listen_script": settings.listen_script or "",
            "changers": changers,
        }

    @app.get("/api/prefs")
    def get_prefs():
        return prefs.public()

    @app.put("/api/settings")
    def put_settings(body: dict):
        if any(k in body for k in ("discogs_token", "discogs_user")):
            raise HTTPException(400, "Use Connect Discogs to set the token.")
        try:
            prefs.update(body)
            return {"prefs": prefs.public()}
        except PrefsError as exc:
            raise HTTPException(400, str(exc)) from exc

    # --- Discogs values ------------------------------------------------------------
    @app.post("/api/discogs/connect")
    async def discogs_connect(body: dict):
        token = str(body.get("token") or "").strip()
        if not token:
            raise HTTPException(400, "Paste your Discogs token first.")
        try:
            async with values.client_factory() as client:
                user = await Discogs(client, token).identity()
        except DiscogsAuthError as exc:
            raise HTTPException(400, f"{exc} Copy it again from Discogs → Settings → Developers.") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Can't reach Discogs from Home Assistant ({type(exc).__name__}).") from exc
        prefs.update({"discogs_token": token, "discogs_user": user})
        values.start()
        return {"user": user, "job": values.state.__dict__}

    @app.post("/api/discogs/disconnect")
    async def discogs_disconnect():
        values.stop()
        prefs.update({"discogs_token": "", "discogs_user": ""})
        return {"ok": True}

    @app.get("/api/values")
    def values_status():
        return {"job": values.state.__dict__, "counts": values.counts()}

    @app.post("/api/values/start")
    async def values_start(force: bool = False):
        return {"job": values.start(force=force).__dict__}

    @app.post("/api/values/stop")
    async def values_stop():
        return {"job": values.stop().__dict__}

    @app.post("/api/albums/{album_id}/value/refresh")
    async def value_refresh(album_id: int):
        if not prefs.get("discogs_token"):
            raise HTTPException(400, "Connect Discogs in Settings first.")
        if values.state.running:
            raise HTTPException(409, "Prices are updating right now; try again when that finishes.")
        db.execute("UPDATE albums SET discogs_release = NULL WHERE id = ? AND COALESCE(value_source,'') != 'manual'",
                   (album_id,))
        values.start(album_ids=[album_id])
        await values.wait()
        return library.album(album_id)

    @app.get("/api/storage")
    def storage():
        data = Path(settings.data_dir)
        db_file = Path(settings.db_path)

        def size(p: Path) -> int:
            try:
                return p.stat().st_size
            except OSError:
                return 0

        db_bytes = sum(size(Path(str(db_file) + s)) for s in ("", "-wal", "-shm", "-journal"))
        covers = [p for p in enricher.art_dir.glob("*.jpg")] if enricher.art_dir.exists() else []
        art_bytes = sum(size(p) for p in covers)
        total = 0
        for p in data.rglob("*"):
            if p.is_file():
                total += size(p)
        other = max(0, total - db_bytes - art_bytes)
        try:
            du = shutil.disk_usage(data)
            disk = {"total": du.total, "used": du.used, "free": du.free}
        except OSError:
            disk = None
        n = lambda sql: db.one(sql)["n"]  # noqa: E731
        return {
            "total_bytes": total,
            "database_bytes": db_bytes,
            "covers_bytes": art_bytes,
            "other_bytes": other,
            "covers": len(covers),
            "covers_by_source": {r["src"]: r["n"] for r in db.query(
                "SELECT COALESCE(art_source,'unknown') AS src, COUNT(*) AS n FROM albums "
                "WHERE art_url IS NOT NULL GROUP BY src")},
            "records": {
                "albums": n("SELECT COUNT(*) AS n FROM albums"),
                "tracks": n("SELECT COUNT(*) AS n FROM tracks"),
                "listens": n("SELECT COUNT(*) AS n FROM sessions"),
                "locked_pressings": n("SELECT COUNT(*) AS n FROM albums WHERE pressing_locked = 1"),
            },
            "disk": disk,
        }

    @app.get("/api/health")
    def health():
        return {"ok": True, "albums": db.one("SELECT COUNT(*) AS n FROM albums")["n"]}

    # --- web app ---------------------------------------------------------------
    www = Path(settings.www_dir)
    if www.exists():
        @app.get("/")
        def index():
            return FileResponse(www / "index.html", headers={"Cache-Control": "no-cache"})

        app.mount("/", StaticFiles(directory=www), name="www")

    return app


def main() -> None:
    import uvicorn

    settings = load_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8099, log_level=settings.log_level)


if __name__ == "__main__":
    main()
