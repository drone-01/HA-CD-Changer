from fastapi.testclient import TestClient

from app.config import ChangerConfig, Settings
from app.ha_client import HAClient
from app.main import create_app


def make_client(tmp_path, drivers):
    www = tmp_path / "www"
    www.mkdir()
    (www / "index.html").write_text("<!doctype html><title>Disc Library</title>")
    settings = Settings(data_dir=tmp_path, www_dir=www,
                        changers=[ChangerConfig(1, "Changer 1"), ChangerConfig(2, "Changer 2")])
    return TestClient(create_app(settings, drivers=drivers, ha=HAClient(token="")))


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
