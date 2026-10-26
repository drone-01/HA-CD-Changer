import asyncio

import httpx
import pytest

from app.enrich import Enricher
from app.importer import import_workbook
from app.library import Library, LocationError
from app.notes import LinerNotes, credits_from_release
from app.onboard import Onboarding, OnboardError


def run(coro):
    return asyncio.run(coro)


DIRT_REL = {
    "id": "2d1c3a6e-0000-4000-8000-000000000d75", "title": "Dirt", "status": "Official", "country": "US", "date": "1992-09-29",
    "barcode": "074645247526", "artist-credit": [{"name": "Alice in Chains"}],
    "release-group": {"id": "rg-dirt", "first-release-date": "1992-09-29"},
    "label-info": [{"catalog-number": "CK 52475", "label": {"name": "Columbia"}}],
    "media": [{"position": 1, "format": "CD", "track-count": 2, "tracks": [
        {"position": 1, "title": "Them Bones", "length": 150000,
         "recording": {"relations": [
             {"target-type": "artist", "type": "instrument", "attributes": ["guitar"], "artist": {"name": "Jerry Cantrell"}},
             {"target-type": "artist", "type": "vocal", "attributes": ["lead vocals"], "artist": {"name": "Layne Staley"}}]}},
        {"position": 2, "title": "Dam That River", "length": 190000,
         "recording": {"relations": [
             {"target-type": "artist", "type": "instrument", "attributes": ["guitar"], "artist": {"name": "Jerry Cantrell"}},
             {"target-type": "artist", "type": "mix", "artist": {"name": "Dave Jerden"}}]}}]}],
    "relations": [{"target-type": "artist", "type": "producer", "artist": {"name": "Dave Jerden"}},
                  {"target-type": "artist", "type": "mastering", "artist": {"name": "Eddy Schreyer"}}],
}


def fake(calls):
    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        p, q = req.url.path, req.url.params
        if p == "/ws/2/release/" and q.get("query", "").startswith("barcode:"):
            code = q["query"].split(":")[1]
            if code == "74645247526" or code == "074645247526":
                return httpx.Response(200, json={"releases": [DIRT_REL]})
            if code == "720642442524":
                return httpx.Response(200, json={"releases": [{**DIRT_REL, "id": "2d1c3a6e-0000-4000-8000-000000000e01", "title": "Nevermind",
                                                               "barcode": "720642442524",
                                                               "artist-credit": [{"name": "Nirvana"}]}]})
            return httpx.Response(200, json={"releases": []})
        if p == "/ws/2/release/2d1c3a6e-0000-4000-8000-000000000e01":
            return httpx.Response(200, json={**DIRT_REL, "id": "2d1c3a6e-0000-4000-8000-000000000e01", "title": "Nevermind", "barcode": "720642442524"})
        if p == "/ws/2/release/2d1c3a6e-0000-4000-8000-000000000d75":
            return httpx.Response(200, json=DIRT_REL)
        if p == "/ws/2/release-group/rg-dirt":
            return httpx.Response(200, json={"genres": [], "relations": [
                {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q1166045"}}]})
        if p.startswith("/ws/2/release-group/"):
            return httpx.Response(200, json={"genres": []})
        if req.url.host == "www.wikidata.org":
            return httpx.Response(200, json={"entities": {"Q1166045": {"sitelinks": {"enwiki": {"title": "Dirt (Alice in Chains album)"}}}}})
        if req.url.host == "en.wikipedia.org":
            return httpx.Response(200, json={"type": "standard", "title": "Dirt (Alice in Chains album)",
                                             "extract": "Dirt is the second studio album by Alice in Chains.",
                                             "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Dirt_(Alice_in_Chains_album)"}}})
        if req.url.host in ("coverartarchive.org", "itunes.apple.com"):
            return httpx.Response(404)
        return httpx.Response(404)
    return handle


@pytest.fixture
def ob(db, workbook):
    import_workbook(db, workbook)
    calls = []
    enr = Enricher(db, db.path + ".art", mb_interval=0,
                   client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake(calls))))
    lib = Library(db, [1, 2])
    return Onboarding(db, lib, enr), lib, calls


def test_barcode_on_matched_release_loads_and_locks(db, ob):
    o, lib, _ = ob
    db.execute("UPDATE albums SET rel_barcode = '074645247526', mbid_release = '2d1c3a6e-0000-4000-8000-000000000d75' WHERE title = 'Dirt'")
    found = run(o.identify("0 74645 24752 6"))
    assert found["kind"] == "barcode" and [m["title"] for m in found["matches"]] == ["Dirt"]
    out = run(o.load(found["matches"][0]["id"], 1, "074645247526"))
    assert out["assignments"][0]["slot"] == 1 and out["pressing"] == "locked"
    assert db.one("SELECT pressing_locked, match_method FROM albums WHERE title = 'Dirt'") == {"pressing_locked": 1, "match_method": "barcode"}
    assert out["status"]["next_slot"] == 2 and out["status"]["free"] == 399
    with pytest.raises(OnboardError, match="already loaded"):
        run(o.load(found["matches"][0]["id"], 1))


def test_unknown_barcode_finds_album_by_name_and_rematches(db, ob):
    o, lib, _ = ob
    found = run(o.identify("720642442524"))
    assert [m["title"] for m in found["matches"]] == ["Nevermind"] and found["release"]["title"] == "Nevermind"
    out = run(o.load(found["matches"][0]["id"], 2, "720642442524"))
    assert out["pressing"] == "rematched" and out["assignments"][0]["changer_id"] == 2
    a = db.one("SELECT mbid_release, pressing_locked, barcode FROM albums WHERE title = 'Nevermind'")
    assert a == {"mbid_release": "2d1c3a6e-0000-4000-8000-000000000e01", "pressing_locked": 1, "barcode": "720642442524"}


def test_text_search_and_catalog(db, ob):
    o, *_ = ob
    assert run(o.identify("CK-52475"))["matches"][0]["title"] == "Dirt"
    assert [m["title"] for m in run(o.identify("downward"))["matches"]] == ["The Downward Spiral"]


def test_on_order_disc_arrives_and_stays_owned_on_reimport(db, ob, workbook):
    o, lib, _ = ob
    kid = db.one("SELECT id FROM albums WHERE title = 'Kid A'")["id"]
    out = run(o.load(kid, 1))
    assert out["arrived"] is True
    import_workbook(db, workbook)  # spreadsheet still says On order
    assert db.one("SELECT status FROM albums WHERE id = ?", (kid,))["status"] == "owned"


def test_skip_slot_and_undo(db, ob):
    o, lib, _ = ob
    o.skip(1, 1, "unknown disc already in it")
    dirt = db.one("SELECT id FROM albums WHERE title = 'Dirt'")["id"]
    out = run(o.load(dirt, 1))
    assert out["assignments"][0]["slot"] == 2 and out["status"]["skipped"] == 1
    with pytest.raises(LocationError, match="skipped"):
        lib.set_location(db.one("SELECT id FROM discs WHERE album_id != ? LIMIT 1", (dirt,))["id"], "changer", 1, 1)
    assert o.undo([a["disc_id"] for a in out["assignments"]]) == 1
    assert o.status(1)["next_slot"] == 2
    o.unskip(1, 1)
    assert o.status(1)["next_slot"] == 1


def test_credits_from_release():
    c = credits_from_release(DIRT_REL)
    assert c["production"] == [{"role": "Produced by", "names": ["Dave Jerden"]},
                               {"role": "Mixed by", "names": ["Dave Jerden"]},
                               {"role": "Mastered by", "names": ["Eddy Schreyer"]}]
    assert c["personnel"] == [{"name": "Jerry Cantrell", "roles": ["guitar"]},
                              {"name": "Layne Staley", "roles": ["lead vocals"]}]


def test_liner_notes_with_background_and_cache(db, ob):
    o, lib, calls = ob
    db.execute("UPDATE albums SET mbid_release = '2d1c3a6e-0000-4000-8000-000000000d75', mbid_release_group = 'rg-dirt' WHERE title = 'Dirt'")
    dirt = db.one("SELECT id FROM albums WHERE title = 'Dirt'")["id"]
    notes = LinerNotes(db, o.enricher)
    n = run(notes.get(dirt))
    assert n["available"] and n["background"]["text"].startswith("Dirt is the second")
    assert n["background"]["url"].endswith("Dirt_(Alice_in_Chains_album)")
    before = len(calls)
    assert run(notes.get(dirt))["cached"] is True and len(calls) == before
    assert run(notes.get(db.one("SELECT id FROM albums WHERE title = 'Nevermind'")["id"]))["available"] is False
