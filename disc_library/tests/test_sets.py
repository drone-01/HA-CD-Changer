import asyncio

import pytest
from openpyxl import Workbook

from app.importer import import_workbook
from app.library import Library
from app.player import Player, PlayError
from app.sets import disc_part, merge_sets, set_member


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def set_workbook(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "CD Collection"
    ws.append(["#", "Artist", "Album", "CD ID / Catalog No.", "Year", "Label", "Edition / Notes", "Source", "Status"])
    ws.append([1, "2Pac", "All Eyez on Me — Book 1", "KOC-CD-5800", 2004, "Death Row", "U.S. 2-CD set — DISC 1. Bought for $20", "", "In collection"])
    ws.append([2, "2Pac", "All Eyez on Me — Book 2", "KOC-CD-5800", 2004, "Death Row", "U.S. 2-CD set — DISC 2", "", "In collection"])
    ws.append([3, "Michael Jackson", "HIStory: Past, Present and Future, Book I", "E2K 59000", 1995, "Epic", "U.S. 2-CD set — DISC 2 ('HIStory Continues')", "", "In collection"])
    ws.append([4, "Michael Jackson", "HIStory: Past, Present and Future, Book I — Disc 1: HIStory Begins", "EK 59003", 1995, "Epic", "", "", "In collection"])
    ws.append([5, "New Order", "Substance", "9 25621-2 (0001)", 1987, "Qwest", "Single disc entry", "", "In collection"])
    ws.append([6, "New Order", "Substance 1987 — Disc 2", "9 25621-2 (0002)", 1987, "Qwest", "", "", "In collection"])
    ws.append([7, "Nirvana", "Nevermind", "DGCD-24425", 1991, "DGC", "", "", "In collection"])
    path = tmp_path / "sets.xlsx"
    wb.save(path)
    return path


def test_disc_titles():
    assert disc_part("All Eyez on Me — Book 2") == ("All Eyez on Me", 2)
    assert disc_part("Workshop of the Telescopes (Disc One)") == ("Workshop of the Telescopes", 1)
    assert disc_part("101 — Disc B") == ("101", 2)
    assert disc_part("Diamond Jubilee — Disc II") == ("Diamond Jubilee", 2)
    assert disc_part("HIStory: Past, Present and Future, Book I") is None
    assert disc_part("Random Access Memories — second copy (clairecords)") is None
    assert set_member("HIStory: Past, Present and Future, Book I", "U.S. 2-CD set — DISC 2") == \
        ("HIStory: Past, Present and Future, Book I", 2)


def test_import_groups_sets(db, set_workbook):
    r = import_workbook(db, set_workbook)
    assert r["rows"] == 7 and r["added"] == 5
    lib = Library(db, [1, 2])
    by_title = {a["title"]: a for a in lib.albums()}
    pac = lib.album(by_title["All Eyez on Me"]["id"])
    assert [d["disc_number"] for d in pac["discs"]] == [1, 2] and pac["paid"] == 20
    mj = lib.album(by_title["HIStory: Past, Present and Future, Book I"]["id"])
    assert [d["disc_number"] for d in mj["discs"]] == [1, 2]
    assert by_title["All Eyez on Me"]["disc_count"] == 2
    # re-import is stable
    r = import_workbook(db, set_workbook)
    assert r["added"] == 0 and len(lib.albums()) == 5


def test_merge_legacy_rows(db):
    for n, title in ((1, "For the Record — Disc 1"), (2, "For the Record — Disc 2")):
        aid = db.execute("INSERT INTO albums (import_key, artist, title, format, paid) VALUES (?, 'Alabama', ?, 'CD', ?)",
                         (f"old|{n}", title, 15.0 if n == 2 else None))
        db.execute("INSERT INTO discs (album_id, disc_number, location_type, changer_id, slot) VALUES (?, 1, 'changer', ?, ?)",
                   (aid, n, 10 + n))
        db.execute("INSERT INTO sessions (album_id, completed) VALUES (?, 1)", (aid,))
    changed = merge_sets(db)
    assert len(changed) == 1
    a = db.one("SELECT * FROM albums")
    assert a["title"] == "For the Record" and a["paid"] == 15.0 and a["enrich_status"] == "pending"
    discs = db.query("SELECT disc_number, changer_id, slot FROM discs ORDER BY disc_number")
    assert [(d["disc_number"], d["changer_id"], d["slot"]) for d in discs] == [(1, 1, 11), (2, 2, 12)]
    assert db.one("SELECT COUNT(*) AS n FROM sessions WHERE album_id = ?", (a["id"],))["n"] == 2
    assert merge_sets(db) == []  # idempotent


def test_link_and_unlink_disc(db, set_workbook):
    import_workbook(db, set_workbook)
    lib = Library(db, [1, 2])
    by_title = {a["title"]: a for a in lib.albums()}
    sub, sub87 = by_title["Substance"]["id"], by_title["Substance 1987"]["id"]
    with pytest.raises(ValueError):
        lib.link_disc(sub, sub87, 1)  # already has a disc 1
    a = lib.link_disc(sub, sub87, 2)
    assert [d["disc_number"] for d in a["discs"]] == [1, 2]
    assert "Substance 1987" not in [x["title"] for x in lib.albums()]
    # the link survives a re-import
    import_workbook(db, set_workbook)
    assert "Substance 1987" not in [x["title"] for x in lib.albums()]
    assert len(lib.album(sub)["discs"]) == 2
    # and can be undone
    d2 = next(d for d in lib.album(sub)["discs"] if d["disc_number"] == 2)
    split = lib.unlink_disc(d2["id"])
    assert len(lib.album(sub)["discs"]) == 1 and len(split["discs"]) == 1


def test_set_plays_through_across_changers(db, set_workbook, drivers, clock):
    import_workbook(db, set_workbook)
    lib = Library(db, [1, 2])
    aid = next(a["id"] for a in lib.albums() if a["title"] == "All Eyez on Me")
    discs = lib.album(aid)["discs"]
    lib.set_location(discs[0]["id"], "changer", 1, 5)
    lib.set_location(discs[1]["id"], "changer", 2, 300)
    for disc in (1, 2):
        for n in range(1, 4):
            db.execute("INSERT INTO tracks (album_id, disc_number, number, title, duration_sec) VALUES (?,?,?,?,10)",
                       (aid, disc, n, f"D{disc}T{n}"))
    player = Player(db, drivers, ha=None, clock=clock)
    run(player.play_album(aid))
    assert player.active.changer_id == 1
    seen_changer_2 = False
    for _ in range(40):
        clock.advance(2)
        run(player.poll())
        if player.active and player.active.changer_id == 2:
            seen_changer_2 = True
    assert seen_changer_2 and player.active is None
    s = db.one("SELECT * FROM sessions")
    assert (s["completed"], s["end_reason"], s["last_disc"]) == (1, "finished", 2)


def test_set_stops_when_next_disc_is_not_loaded(db, set_workbook, drivers, clock):
    import_workbook(db, set_workbook)
    lib = Library(db, [1, 2])
    aid = next(a["id"] for a in lib.albums() if a["title"] == "All Eyez on Me")
    discs = lib.album(aid)["discs"]
    lib.set_location(discs[0]["id"], "changer", 1, 5)
    for disc in (1, 2):
        for n in range(1, 4):
            db.execute("INSERT INTO tracks (album_id, disc_number, number, title, duration_sec) VALUES (?,?,?,?,10)",
                       (aid, disc, n, f"D{disc}T{n}"))
    player = Player(db, drivers, ha=None, clock=clock)
    run(player.play_album(aid))
    for _ in range(40):
        clock.advance(2)
        run(player.poll())
    s = db.one("SELECT * FROM sessions")
    assert (s["completed"], s["end_reason"]) == (0, "next_disc_missing")
    with pytest.raises(PlayError, match="Disc 2 of All Eyez on Me"):
        run(player.play_album(aid, from_disc=2))


def test_remove_set_lists_every_slot(db, set_workbook):
    import_workbook(db, set_workbook)
    lib = Library(db, [1, 2])
    aid = next(a["id"] for a in lib.albums() if a["title"] == "All Eyez on Me")
    discs = lib.album(aid)["discs"]
    lib.set_location(discs[0]["id"], "changer", 1, 5)
    lib.set_location(discs[1]["id"], "changer", 2, 300)
    out = lib.remove(aid)
    assert [(d["disc_number"], d["changer_id"], d["slot"]) for d in out["slots"]] == [(1, 1, 5), (2, 2, 300)]
    assert len(lib.to_pull()) == 2


def test_decade_filter(db, set_workbook):
    import_workbook(db, set_workbook)
    lib = Library(db, [1, 2])
    assert {d["decade"]: d["albums"] for d in lib.decades()} == {1980: 2, 1990: 2, 2000: 1}
    assert [a["title"] for a in lib.albums(decade=1990)] == ["HIStory: Past, Present and Future, Book I", "Nevermind"]
