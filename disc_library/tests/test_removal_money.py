from openpyxl import load_workbook

from app.importer import import_workbook, paid_from_notes
from app.library import Library, value_tier


def album_id(db, title):
    return db.one("SELECT id FROM albums WHERE title = ?", (title,))["id"]


def disc_id(db, title):
    return db.one("SELECT d.id FROM discs d JOIN albums a ON a.id = d.album_id WHERE a.title = ?", (title,))["id"]


def test_paid_from_notes():
    assert paid_from_notes("Bought on eBay 2026-09-04 for $9.99 from someone") == 9.99
    assert paid_from_notes("bought for $12 | order total US $21.80. Listed brand new.") == 21.80
    assert paid_from_notes("Bought on eBay 2026-09-03 for $1,062.73") == 1062.73
    assert paid_from_notes("Bought in the same order as the Phil Collins Face Value.") is None
    assert paid_from_notes(None) is None


def test_value_tiers():
    assert value_tier(None) is None
    assert value_tier(4) == "$" and value_tier(8.99) == "$"
    assert value_tier(9) == "$$" and value_tier(24.99) == "$$"
    assert value_tier(25) == "$$$"


def test_paid_imports_and_manual_value_sticks(db, workbook):
    wb = load_workbook(workbook)
    wb["CD Collection"]["G3"] = "Bought on eBay for $14.50 from a seller"
    wb.save(workbook)
    import_workbook(db, workbook)
    lib = Library(db, [1, 2])
    dirt = album_id(db, "Dirt")
    assert db.one("SELECT paid, paid_source FROM albums WHERE id = ?", (dirt,)) == {"paid": 14.5, "paid_source": "notes"}

    lib.set_money(dirt, {"paid": "$16", "value": "30"})
    import_workbook(db, workbook)  # a typed-in price survives a re-import
    row = db.one("SELECT paid, paid_source, value, value_source FROM albums WHERE id = ?", (dirt,))
    assert row == {"paid": 16.0, "paid_source": "manual", "value": 30.0, "value_source": "manual"}

    lib.set_money(album_id(db, "Nevermind"), {"value": 5})
    titles = [a["title"] for a in lib.albums(sort="value")]
    assert titles[:2] == ["Dirt", "Nevermind"]

    c = lib.collection()
    assert c["money"]["value_total"] == 35 and c["money"]["tiers"] == {"$": 1, "$$": 0, "$$$": 1}
    assert c["money"]["paid_known"] == 1


def test_remove_restore_and_reimport(db, workbook):
    import_workbook(db, workbook)
    lib = Library(db, [1, 2])
    dirt = album_id(db, "Dirt")
    lib.set_location(disc_id(db, "Dirt"), "changer", 1, 3)

    out = lib.remove(dirt)
    assert out["removed_at"] and out["slots"] == []
    assert "Dirt" not in [a["title"] for a in lib.albums()]
    assert lib.free_slot(1) == {"changer_id": 1, "slot": 1}
    assert [r["title"] for r in lib.removed()] == ["Dirt"]

    r = import_workbook(db, workbook)  # stays removed
    assert r["skipped_removed"] == 1
    assert "Dirt" not in [a["title"] for a in lib.albums()]

    back = lib.restore(dirt)
    assert back["back_in_slot"] == [{"changer_id": 1, "slot": 3}]
    a = lib.album(dirt)
    assert (a["location_type"], a["changer_id"], a["slot"]) == ("changer", 1, 3)


def test_restore_when_old_slot_was_reused(db, workbook):
    import_workbook(db, workbook)
    lib = Library(db, [1, 2])
    lib.set_location(disc_id(db, "Dirt"), "changer", 1, 3)
    lib.remove(album_id(db, "Dirt"))
    lib.set_location(disc_id(db, "Nevermind"), "changer", 1, 3)
    assert lib.restore(album_id(db, "Dirt"))["back_in_slot"] == []
    assert lib.album(album_id(db, "Dirt"))["location_type"] == "unassigned"


def test_collection_counts(db, workbook):
    import_workbook(db, workbook)
    lib = Library(db, [1, 2])
    lib.set_location(disc_id(db, "Dirt"), "changer", 1, 3)
    db.execute("UPDATE albums SET runtime_sec = 3000 WHERE title = 'Dirt'")
    c = lib.collection({1: "Upstairs"})
    assert c["albums"] == 6 and c["on_order"] == 1 and c["discs"] == 6
    assert c["video"] == 2 and c["audio"] == 4
    assert c["formats"]["CD"] == 3 and c["formats"]["SACD"] == 1 and c["formats"]["Blu-ray"] == 2
    assert c["changers"][0] == {"id": 1, "name": "Upstairs", "used": 1, "capacity": 400}
    assert c["not_in_changer"] == 5 and c["runtime_sec"] == 3000
