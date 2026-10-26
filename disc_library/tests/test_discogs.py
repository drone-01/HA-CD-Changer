import asyncio

import httpx

from app.discogs import ValueUpdater
from app.importer import import_workbook
from app.library import Library
from app.prefs import Prefs


def run(coro):
    return asyncio.run(coro)


def fake_discogs(calls, suggestions=True):
    def handle(req: httpx.Request) -> httpx.Response:
        calls.append((req.url.path, dict(req.url.params)))
        assert req.headers["Authorization"] == "Discogs token=tok"
        assert req.headers["User-Agent"].startswith("DiscLibrary/")
        p, q = req.url.path, req.url.params
        if p == "/oauth/identity":
            return httpx.Response(200, json={"username": "adam"})
        if p == "/database/search":
            if q.get("catno") == "CK 52475":
                return httpx.Response(200, json={"results": [
                    {"id": 9, "title": "Alice In Chains - Dirt", "format": ["Vinyl", "LP"], "country": "US"},
                    {"id": 10, "title": "Alice In Chains - Dirt", "format": ["CD", "Album"], "country": "Europe"},
                    {"id": 11, "title": "Alice In Chains - Dirt", "format": ["CD", "Album"], "country": "US"}]})
            if q.get("release_title") == "Nevermind":
                return httpx.Response(200, json={"results": [{"id": 20, "format": ["CD"], "country": "US"}]})
            return httpx.Response(200, json={"results": []})
        if p.startswith("/marketplace/price_suggestions/"):
            if not suggestions:
                return httpx.Response(403, json={"message": "seller settings"})
            rid = p.rsplit("/", 1)[1]
            return httpx.Response(200, json={} if rid == "20" else {
                "Very Good (VG)": {"currency": "USD", "value": 6.4321},
                "Near Mint (NM or M-)": {"currency": "USD", "value": 11.0}})
        if p.startswith("/marketplace/stats/"):
            return httpx.Response(200, json={"lowest_price": {"currency": "USD", "value": 3.5}, "num_for_sale": 4})
        return httpx.Response(404)
    return handle


def updater(db, calls, **kw):
    prefs = Prefs(db)
    prefs.update({"discogs_token": "tok"})
    return prefs, ValueUpdater(db, prefs, interval=0,
                               client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake_discogs(calls, **kw))))


def test_prices_by_catalog_then_title(db, workbook):
    import_workbook(db, workbook)
    calls = []
    prefs, vu = updater(db, calls)

    async def go():
        vu.start()
        await vu.wait()
    run(go())
    dirt = db.one("SELECT * FROM albums WHERE title = 'Dirt'")
    assert (dirt["discogs_release"], dirt["value"], dirt["value_source"], dirt["value_basis"]) == ("11", 6.43, "discogs", "VG")
    nev = db.one("SELECT * FROM albums WHERE title = 'Nevermind'")
    assert (nev["value"], nev["value_basis"]) == (3.5, "lowest of 4 listed")  # no suggestion -> lowest listing
    tds = db.one("SELECT * FROM albums WHERE title = 'The Downward Spiral'")
    assert tds["value"] is None and tds["value_basis"] == "not found"
    # on-order albums are not priced
    assert db.one("SELECT value_at FROM albums WHERE title = 'Kid A'")["value_at"] is None
    assert vu.state.priced >= 2 and vu.counts()["due"] == 0

    # typed-in values are never overwritten; condition can change
    Library(db, [1]).set_money(dirt["id"], {"value": 50})
    prefs.update({"discogs_condition": "Near Mint (NM or M-)"})

    async def again():
        vu.start(force=True)
        await vu.wait()
    run(again())
    assert db.one("SELECT value FROM albums WHERE id = ?", (dirt["id"],))["value"] == 50


def test_lowest_listing_when_suggestions_refused(db, workbook):
    import_workbook(db, workbook)
    calls = []
    _, vu = updater(db, calls, suggestions=False)

    async def go():
        vu.start()
        await vu.wait()
    run(go())
    assert db.one("SELECT value, value_basis FROM albums WHERE title = 'Dirt'") == {"value": 3.5, "value_basis": "lowest of 4 listed"}
    assert "seller settings" in vu.state.notice


def test_token_never_sent_to_browser(db):
    prefs = Prefs(db)
    prefs.update({"discogs_token": "secret"})
    pub = prefs.public()
    assert "discogs_token" not in pub and pub["has_discogs_token"] is True


def test_thin_listings_are_flagged_and_left_out(db, workbook):
    import_workbook(db, workbook)
    db.execute("UPDATE albums SET value = 3200, value_source = 'discogs', value_basis = 'only 1 listed' WHERE title = 'Dirt'")
    db.execute("UPDATE albums SET value = 8, value_source = 'discogs', value_basis = 'VG' WHERE title = 'Nevermind'")
    c = Library(db, [1]).collection()
    assert c["money"]["value_total"] == 8 and c["money"]["value_thin"] == 1
