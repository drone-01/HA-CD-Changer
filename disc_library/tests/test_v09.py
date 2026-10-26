"""v0.9: theater scenes on Play, adding discs that aren't in the catalog, the slot check."""

import asyncio

import httpx
import pytest

from app.enrich import Enricher
from app.importer import import_workbook
from app.library import Library
from app.onboard import Onboarding, OnboardError, guess_format
from app.player import Player
from app.prefs import Prefs, PrefsError


def run(coro):
    return asyncio.run(coro)


class FakeHA:
    def __init__(self):
        self.calls = []

    async def run_script(self, entity_id, variables=None):
        self.calls.append((entity_id, variables))
        return True


def test_play_runs_listen_or_watch_with_the_changer_input(db, workbook, drivers, clock):
    import_workbook(db, workbook)
    lib = Library(db, [1, 2])
    cd = db.one("SELECT * FROM albums WHERE title = 'Dirt'")
    video = db.one("SELECT * FROM albums WHERE media_kind = 'video' LIMIT 1")
    lib.set_location(db.one("SELECT id FROM discs WHERE album_id = ?", (cd["id"],))["id"], "changer", 1, 5)
    lib.set_location(db.one("SELECT id FROM discs WHERE album_id = ?", (video["id"],))["id"], "changer", 2, 7)
    ha, prefs = FakeHA(), Prefs(db)
    p = Player(db, drivers, ha, None, clock=clock, prefs=prefs)
    run(p.play_album(cd["id"]))
    run(p.play_album(video["id"]))
    assert ha.calls == [("script.theater_listen", {"source": "SA-CD/CD"}), ("script.theater_watch", {"source": "VIDEO"})]

    prefs.update({"watch_script": "", "changer_inputs": {"1": "Changer 1", "2": ""}})
    ha.calls.clear()
    run(p.play_album(cd["id"]))
    run(p.play_album(video["id"]))
    assert ha.calls == [("script.theater_listen", {"source": "Changer 1"})]  # empty watch script: skipped


def test_changer_inputs_validation(db):
    prefs = Prefs(db)
    assert prefs.update({"changer_inputs": '{"1": " BD/DVD "}'})["changer_inputs"] == {"1": "BD/DVD"}
    with pytest.raises(PrefsError):
        prefs.update({"changer_inputs": {"one": "x"}})
    with pytest.raises(PrefsError):
        prefs.update({"watch_script": "light.kitchen"})


def test_guess_format():
    assert guess_format(["Blu-ray", "Album"]) == "Blu-ray"
    assert guess_format("DVD-Video") == "DVD"
    assert guess_format(["Blu-ray"], "Live (4K Ultra HD)") == "4K Blu-ray"
    assert guess_format(["SACD", "Hybrid"]) == "SACD"
    assert guess_format(["DVD", "DVD-Audio"]) == "DVD-Audio"
    assert guess_format(None) == "CD"


def discogs_and_mb(calls):
    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if req.url.host == "musicbrainz.org":
            return httpx.Response(200, json={"releases": []})
        if req.url.path == "/database/search" and req.url.params.get("barcode") == "602438123456":
            return httpx.Response(200, json={"results": [{"id": 555, "title": "Alice In Chains (2) - MTV Unplugged",
                                                          "year": "1996", "label": ["Columbia"], "catno": "CVD 50140",
                                                          "country": "US", "format": ["DVD", "DVD-Video", "NTSC"],
                                                          "cover_image": "https://i.discogs.com/x.jpg"}]})
        return httpx.Response(200, json={"results": []})
    return handle


@pytest.fixture
def ob(db, workbook):
    import_workbook(db, workbook)
    calls = []
    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(discogs_and_mb(calls)))  # noqa: E731
    enr = Enricher(db, db.path + ".art", mb_interval=0, client_factory=factory)
    prefs = Prefs(db)
    prefs.update({"discogs_token": "tok"})
    lib = Library(db, [1, 2])
    return Onboarding(db, lib, enr, prefs=prefs, discogs_factory=factory), lib


def test_unknown_barcode_falls_back_to_discogs_and_adds_a_concert(db, ob):
    o, lib = ob
    found = run(o.identify("602438123456"))
    rel = found["release"]
    assert found["matches"] == [] and rel["source"] == "discogs"
    assert (rel["artist"], rel["title"], rel["suggested_format"], rel["year"]) == ("Alice In Chains", "MTV Unplugged", "DVD", 1996)
    out = run(o.add_new({"artist": rel["artist"], "title": rel["title"], "format": rel["suggested_format"],
                         "year": rel["year"], "catno": rel["catno"], "barcode": "602438123456", "source": "discogs",
                         "release_id": "555", "cover": rel["cover"]}, 2))
    assert out["added"] and out["assignments"][0] == {**out["assignments"][0], "changer_id": 2, "slot": 1}
    a = db.one("SELECT * FROM albums WHERE id = ?", (out["album_id"],))
    assert (a["media_kind"], a["format"], a["discogs_release"], a["enrich_status"], a["art_url"]) == \
        ("video", "DVD", "555", "manual", "https://i.discogs.com/x.jpg")
    assert [x["title"] for x in lib.albums(media_kind="video")] .count("MTV Unplugged") == 1
    with pytest.raises(OnboardError, match="already in your library"):
        run(o.add_new({"artist": "x", "title": "y", "barcode": "602438123456"}, 2))


def test_add_movie_without_artist_and_a_two_disc_cd(db, ob):
    o, lib = ob
    movie = run(o.add_new({"title": "Blade Runner", "format": "4K Blu-ray"}, 2))
    assert db.one("SELECT artist, media_kind FROM albums WHERE id = ?", (movie["album_id"],)) == {"artist": "Movie", "media_kind": "video"}
    cd = run(o.add_new({"artist": "Pearl Jam", "title": "Live", "format": "CD", "discs": 2}, 1))
    assert [x["slot"] for x in cd["assignments"]] == [1, 2]
    assert db.one("SELECT enrich_status FROM albums WHERE id = ?", (cd["album_id"],))["enrich_status"] == "pending"
    with pytest.raises(OnboardError, match="artist and the title"):
        run(o.add_new({"title": "", "format": "CD"}, 1))


def test_slot_audit(db, workbook):
    import_workbook(db, workbook)
    lib = Library(db, [1, 2])
    dirt = db.one("SELECT d.id FROM discs d JOIN albums a ON a.id = d.album_id WHERE a.title = 'Dirt'")["id"]
    nev = db.one("SELECT d.id FROM discs d JOIN albums a ON a.id = d.album_id WHERE a.title = 'Nevermind'")["id"]
    lib.set_location(dirt, "changer", 1, 10)
    lib.set_location(nev, "changer", 1, 11)
    db.execute("INSERT INTO slot_blocks (changer_id, slot, note) VALUES (1, 50, 'x'), (1, 51, 'y')")
    r = lib.audit(1, [10, 12, 13, 50])
    assert r["occupied"] == 4 and r["matched"] == 1
    assert r["unknown"] == [12, 13] and [m["title"] for m in r["missing"]] == ["Nevermind"]
    assert r["skipped_full"] == [50] and r["skipped_empty"] == [51]


def test_audit_endpoints(tmp_path, workbook):
    from fastapi.testclient import TestClient

    from app.config import ChangerConfig, Settings
    from app.db import Database
    from app.drivers.simulated import SimulatedChanger
    from app.ha_client import HAClient
    from app.main import create_app

    class Reporting(SimulatedChanger):
        async def occupied_slots(self):
            return [3, 4]

    www = tmp_path / "www"
    www.mkdir()
    (www / "index.html").write_text("<!doctype html>")
    settings = Settings(data_dir=tmp_path, www_dir=www, changers=[ChangerConfig(1, "Changer 1")])
    Database(settings.db_path)
    enr = Enricher(Database(settings.db_path), tmp_path / "art", mb_interval=0,
                   client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"releases": []}))))
    drivers = {1: Reporting(ChangerConfig(1, "Changer 1"))}
    with TestClient(create_app(settings, drivers=drivers, ha=HAClient(token=""), enricher=enr)) as c:
        r = c.get("/api/changers/1/audit").json()
        assert r["unknown"] == [3, 4]
        assert c.post("/api/changers/1/audit/skip", json={"slots": [3, 4]}).json()["skipped"] == 2
        assert c.get("/api/changers/1/audit").json()["skipped_full"] == [3, 4]
        assert c.post("/api/changers/1/audit/clear", json={"slots": [3]}).json()["skipped"] == 1
        assert c.get("/api/onboard/formats").json()[0] == {"format": "CD", "kind": "audio"}


def test_mqtt_discovery_states_and_buttons(db, workbook, drivers, clock):
    from app.mqtt_bridge import MqttBridge, changer_states, discovery_messages

    msgs = discovery_messages([{"id": 1, "name": "Changer 1"}, {"id": 2, "name": "Changer 2"}], "0.9.0")
    assert len(msgs) == 12  # 2 sensors + 4 buttons per changer
    topics = dict(msgs)
    np = topics["homeassistant/sensor/disc_library_changer_1/now_playing/config"]
    assert np["state_topic"] == "disc_library/changer/1/state" and np["device"]["name"] == "Changer 1"
    assert topics["homeassistant/button/disc_library_changer_2/next/config"]["command_topic"] == "disc_library/changer/2/command"

    import_workbook(db, workbook)
    lib = Library(db, [1, 2])
    cd = db.one("SELECT * FROM albums WHERE title = 'Dirt'")
    lib.set_location(db.one("SELECT id FROM discs WHERE album_id = ?", (cd["id"],))["id"], "changer", 1, 5)
    p = Player(db, drivers, FakeHA(), None, clock=clock, prefs=Prefs(db))
    run(p.play_album(cd["id"]))
    clock.advance(2)
    st = run(changer_states(p, drivers))
    assert st[1]["managed"] and st[1]["now_playing"] == "Alice In Chains – Dirt" and st[1]["slot"] == 5
    assert st[2]["managed"] is False and st[2]["now_playing"] == "Nothing playing"

    bridge = MqttBridge(p, drivers, "0.9.0")
    run(bridge.handle_command(1, "play_pause"))
    assert run(drivers[1].status()).state == "paused"
    run(bridge.handle_command(1, "play_pause"))
    assert run(drivers[1].status()).state == "playing"
    run(bridge.handle_command(1, "stop"))
    assert p.active is None
    run(bridge.handle_command(9, "stop"))       # unknown changer: ignored
    run(bridge.handle_command(1, "self_destruct"))  # unknown command: ignored
