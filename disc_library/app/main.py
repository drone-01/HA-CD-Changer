"""HTTP API and web app, served through Home Assistant ingress on port 8099.

Ingress handles login, so there is no auth here; the add-on exposes no
other port. Home Assistant passes the signed-in user in X-Remote-User-*
headers, which tags each listening session with who played it.
"""

from __future__ import annotations

import asyncio
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

from .config import Settings, load_settings
from .db import Database
from .drivers import build_driver
from .enrich import Enricher
from .ha_client import HAClient
from .importer import import_workbook
from .library import Library, LocationError
from .player import PlayError, Player

log = logging.getLogger("disc_library")


class PlayBody(BaseModel):
    album_id: int
    from_track: int = 1


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
               enricher: Enricher | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    # A session still open from before a restart can't be followed any more.
    db.execute("""UPDATE sessions SET ended_at = datetime('now'), end_reason = 'interrupted'
                  WHERE ended_at IS NULL""")
    drivers = drivers or {c.id: build_driver(c, settings) for c in settings.changers}
    library = Library(db, list(drivers))
    player = Player(db, drivers, ha if ha is not None else HAClient(), settings.listen_script)
    art_dir = Path(settings.data_dir) / "art"
    enricher = enricher or Enricher(db, art_dir)

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

        task = asyncio.create_task(poll_loop())
        yield
        task.cancel()
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

    @app.get("/art/{name}")
    def art(name: str):
        path = (enricher.art_dir / name).resolve()
        if path.parent != enricher.art_dir.resolve() or not path.is_file():
            raise HTTPException(404, "No cover")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=31536000"})

    # --- library ---------------------------------------------------------------
    @app.get("/api/albums")
    def albums(q: str | None = None, genre: str | None = None, favorites: bool = False,
               media: str = "audio", sort: str = "artist", limit: int = 1000, offset: int = 0):
        return library.albums(q=q, genre=genre, favorites=favorites,
                              media_kind=None if media == "all" else media, sort=sort, limit=limit, offset=offset)

    @app.get("/api/albums/{album_id}")
    def album(album_id: int):
        a = library.album(album_id)
        if not a:
            raise HTTPException(404, "Album not found")
        return a

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
            if result["added"]:
                enricher.start()  # fetch covers and details for the new albums in the background
            return result
        finally:
            path.unlink(missing_ok=True)

    # --- playing ---------------------------------------------------------------
    @app.post("/api/play")
    async def play(body: PlayBody, request: Request):
        try:
            return await player.play_album(body.album_id, body.from_track, _user(request))
        except PlayError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/albums/{album_id}/resume")
    async def resume(album_id: int, request: Request):
        try:
            return await player.resume_album(album_id, _user(request))
        except PlayError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/random")
    async def random_album(request: Request, genre: str | None = None):
        picks = library.vibe(seed=None, count=50)
        if genre:
            picks = [p for p in picks if genre in (p["genre"], p["subgenre"])]
        if not picks:
            raise HTTPException(409, "No albums are in a changer yet.")
        return await player.play_album(picks[0]["id"], 1, _user(request))

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
