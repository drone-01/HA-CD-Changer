from fastapi.testclient import TestClient

from app.config import ChangerConfig, Settings
import httpx

from app.enrich import Enricher
from app.ha_client import HAClient
from app.main import create_app
from app.db import Database


def make_client(tmp_path, drivers):
    www = tmp_path / "www"
    www.mkdir()
    (www / "index.html").write_text("<!doctype html><title>Disc Library</title>")
    settings = Settings(data_dir=tmp_path, www_dir=www,
                        changers=[ChangerConfig(1, "Changer 1"), ChangerConfig(2, "Changer 2")])
    offline = Enricher(Database(settings.db_path), tmp_path / "art", mb_interval=0,
                       client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(
                           lambda req: httpx.Response(200, json={"releases": []}))))
    return TestClient(create_app(settings, drivers=drivers, ha=HAClient(token=""), enricher=offline))


def test_end_to_end(tmp_path, drivers, workbook):
    with make_client(tmp_path, drivers) as c:
        assert c.get("/").status_code == 200
        r = c.post("/api/import", files={"file": ("CD_Collection.xlsx", workbook.read_bytes())})
        assert r.json()["added"] == 7

        albums = c.get("/api/albums").json()
        assert [a["artist"] for a in albums] == ["Alice In Chains", "Nine Inch Nails", "Nirvana", "Pink Floyd"]
        assert len(albums) == 4
        assert len(c.get("/api/albums?media=all").json()) == 6

        dirt = next(a for a in albums if a["title"] == "Dirt")
        slot = c.get("/api/slots/free").json()
        r = c.post(f"/api/discs/{dirt['disc_id']}/location",
                   json={"location_type": "changer", **slot})
        assert r.status_code == 200

        r = c.post("/api/play", json={"album_id": dirt["id"]}, headers={"X-Remote-User-Display-Name": "Adam"})
        assert r.status_code == 200 and r.json()["changer"]["slot"] == slot["slot"]
        assert c.get("/api/now-playing").json()["playing"] is True
        assert c.post("/api/transport/next").json()["track"] == 2
        assert c.post("/api/transport/stop").json() == {"playing": False}
        assert c.get("/api/history").json()[0]["user_name"] == "Adam"

        assert c.post(f"/api/albums/{dirt['id']}/favorite", json={"favorite": True}).json()["favorite"] == 1
        assert len(c.get("/api/albums?favorites=true").json()) == 1
        assert c.get("/api/albums?q=nirvana").json()[0]["title"] == "Nevermind"

        nevermind = next(a for a in albums if a["title"] == "Nevermind")
        r = c.post("/api/play", json={"album_id": nevermind["id"]})
        assert r.status_code == 409 and "isn't in a changer" in r.json()["detail"]

        changers = c.get("/api/changers").json()
        assert [ch["used"] for ch in changers] == [1, 0]
        assert c.post("/api/vibe", json={"familiarity": "favorite"}).json()[0]["reason"].startswith("A favorite")
        assert c.get("/api/stats?range=year").json()["partial_listens"] == 1


def test_storage(tmp_path, drivers, workbook):
    with make_client(tmp_path, drivers) as c:
        c.post("/api/import", files={"file": ("CD_Collection.xlsx", workbook.read_bytes())})
        (tmp_path / "art").mkdir(exist_ok=True)
        (tmp_path / "art" / "1.jpg").write_bytes(b"x" * 2048)
        r = c.get("/api/storage")
    assert r.status_code == 200
    s = r.json()
    assert s["covers"] == 1 and s["covers_bytes"] == 2048
    assert s["records"]["albums"] == 7
    assert s["database_bytes"] > 0
    assert s["total_bytes"] >= s["database_bytes"] + s["covers_bytes"]
    assert s["records"]["albums"] >= 0 and "tracks" in s["records"]
    assert s["disk"] is None or s["disk"]["free"] > 0


def test_settings(tmp_path, drivers, workbook):
    with make_client(tmp_path, drivers) as c:
        s = c.get("/api/settings").json()
        assert s["prefs"]["full_listen_percent"] == 90 and s["prefs"]["apple_covers"] is True
        assert [ch["name"] for ch in s["changers"]] == ["Changer 1", "Changer 2"]
        assert s["version"]
        r = c.put("/api/settings", json={"full_listen_percent": 75, "apple_covers": False,
                                          "listen_script": "script.theater_listen"})
        assert r.status_code == 200 and r.json()["prefs"]["full_listen_percent"] == 75
        assert c.get("/api/settings").json()["prefs"]["listen_script"] == "script.theater_listen"
        assert c.put("/api/settings", json={"full_listen_percent": 20}).status_code == 400
        assert c.put("/api/settings", json={"listen_script": "light.kitchen"}).status_code == 400
        assert c.put("/api/settings", json={"nope": 1}).status_code == 400
        # turning off auto-sync keeps an import from starting a lookup
        c.put("/api/settings", json={"auto_sync_on_import": False})
        c.post("/api/import", files={"file": ("CD_Collection.xlsx", workbook.read_bytes())})
        assert c.get("/api/enrich").json()["job"]["running"] is False


def test_version_matches_config():
    from pathlib import Path
    import re
    from app import __version__
    cfg = (Path(__file__).resolve().parent.parent / "config.yaml").read_text()
    assert re.search(r'^version: "([^"]+)"', cfg, re.M).group(1) == __version__


def test_remove_and_money_api(tmp_path, drivers, workbook):
    with make_client(tmp_path, drivers) as c:
        c.post("/api/import", files={"file": ("CD_Collection.xlsx", workbook.read_bytes())})
        dirt = next(a for a in c.get("/api/albums").json() if a["title"] == "Dirt")
        assert c.get(f"/api/albums/{dirt['id']}/remove-preview").json()["title"] == "Dirt"
        assert c.put(f"/api/albums/{dirt['id']}/money", json={"value": "abc"}).status_code == 400
        assert c.put(f"/api/albums/{dirt['id']}/money", json={"value": 12, "paid": 7}).json()["value"] == 12
        assert c.get("/api/collection").json()["money"]["tiers"]["$$"] == 1
        assert c.delete(f"/api/albums/{dirt['id']}").status_code == 200
        assert "Dirt" not in [a["title"] for a in c.get("/api/albums").json()]
        assert [r["title"] for r in c.get("/api/removed").json()] == ["Dirt"]
        assert c.post(f"/api/albums/{dirt['id']}/restore").status_code == 200
        assert "Dirt" in [a["title"] for a in c.get("/api/albums").json()]
        assert c.delete("/api/albums/99999").status_code == 404


def test_manual_lock_link_and_pull_api(tmp_path, drivers, workbook):
    with make_client(tmp_path, drivers) as c:
        c.post("/api/import", files={"file": ("CD_Collection.xlsx", workbook.read_bytes())})
        albums = {a["title"]: a for a in c.get("/api/albums").json()}
        tds, dirt = albums["The Downward Spiral"], albums["Dirt"]
        assert c.post(f"/api/albums/{tds['id']}/lock-manual", json={}).status_code == 400
        a = c.post(f"/api/albums/{tds['id']}/lock-manual", json={"catalog_no": "INTD-92346", "barcode": "6069 4923 46"}).json()
        assert (a["pressing_locked"], a["catalog_verified"], a["barcode"]) == (1, "INTD-92346", "606949234 6".replace(" ", ""))
        cands = c.get(f"/api/albums/{dirt['id']}/link-candidates").json()
        assert tds["id"] in [x["id"] for x in cands]
        linked = c.post(f"/api/albums/{dirt['id']}/link", json={"other_id": tds["id"], "as_disc": 2}).json()
        assert len(linked["discs"]) == 2
        d1, d2 = linked["discs"]
        c.post(f"/api/discs/{d1['id']}/location", json={"location_type": "changer", "changer_id": 1, "slot": 4})
        c.post(f"/api/discs/{d2['id']}/location", json={"location_type": "changer", "changer_id": 2, "slot": 8})
        prev = c.get(f"/api/albums/{dirt['id']}/remove-preview").json()
        assert len(prev["slots"]) == 2
        c.delete(f"/api/albums/{dirt['id']}")
        pull = c.get("/api/to-pull").json()
        assert [(p["changer_id"], p["slot"]) for p in pull] == [(1, 4), (2, 8)]
        assert c.post("/api/to-pull/pulled", json={"disc_ids": [p["disc_id"] for p in pull]}).json()["pulled"] == 2
        assert c.get("/api/to-pull").json() == []
        assert c.get("/api/decades").status_code == 200
        assert c.put("/api/settings", json={"discogs_token": "x"}).status_code == 400
        assert "discogs_token" not in c.get("/api/prefs").json()
