import asyncio
from datetime import date, timedelta

import pytest

from app.importer import import_workbook
from app.library import Library, LocationError, _streaks
from app.player import Player, PlayError, score_full_listen


def run(coro):
    return asyncio.run(coro)


def album_id(db, title):
    return db.one("SELECT id FROM albums WHERE title = ?", (title,))["id"]


def disc_id(db, title):
    return db.one("SELECT d.id FROM discs d JOIN albums a ON a.id = d.album_id WHERE a.title = ?", (title,))["id"]


# --- import -------------------------------------------------------------------
def test_import_reads_all_tabs(db, workbook):
    result = import_workbook(db, workbook)
    assert result["added"] == 6
    assert result["by_sheet"] == {"CD Collection": 4, "Non-CD Formats": 1, "Video (non-CD)": 1}
    kid_a = db.one("SELECT * FROM albums WHERE title = 'Kid A'")
    assert kid_a["status"] == "on_order"
    dirt = db.one("SELECT * FROM albums WHERE title = 'Dirt'")
    assert dirt["year"] == 1992 and dirt["catalog_no"] == "CK 52475"
    assert db.one("SELECT format FROM albums WHERE artist = 'Pink Floyd'")["format"] == "SACD"
    assert db.one("SELECT media_kind FROM albums WHERE artist = 'Various'")["media_kind"] == "video"


def test_reimport_keeps_locations_and_favorites(db, workbook):
    import_workbook(db, workbook)
    lib = Library(db, [1, 2])
    lib.set_location(disc_id(db, "Dirt"), "changer", 2, 17)
    lib.set_favorite(album_id(db, "Dirt"), True)
    result = import_workbook(db, workbook)
    assert result["added"] == 0 and result["updated"] == 6
    dirt = lib.album(album_id(db, "Dirt"))
    assert (dirt["changer_id"], dirt["slot"], dirt["favorite"]) == (2, 17, 1)


# --- locations ----------------------------------------------------------------
def test_slot_conflicts_and_bounds(db, workbook):
    import_workbook(db, workbook)
    lib = Library(db, [1, 2])
    lib.set_location(disc_id(db, "Dirt"), "changer", 1, 5)
    with pytest.raises(LocationError, match="already holds Alice In Chains"):
        lib.set_location(disc_id(db, "Nevermind"), "changer", 1, 5)
    with pytest.raises(LocationError):
        lib.set_location(disc_id(db, "Nevermind"), "changer", 1, 401)
    with pytest.raises(LocationError):
        lib.set_location(disc_id(db, "Nevermind"), "changer", 3, 1)


def test_free_slot_prefers_emptier_changer(db, workbook):
    import_workbook(db, workbook)
    lib = Library(db, [1, 2])
    lib.set_location(disc_id(db, "Dirt"), "changer", 1, 1)
    assert lib.free_slot() == {"changer_id": 2, "slot": 1}
    assert lib.free_slot(1) == {"changer_id": 1, "slot": 2}


# --- playing ------------------------------------------------------------------
@pytest.fixture
def setup(db, workbook, drivers, clock):
    import_workbook(db, workbook)
    lib = Library(db, [1, 2])
    lib.set_location(disc_id(db, "The Downward Spiral"), "changer", 2, 147)
    lib.set_location(disc_id(db, "Dirt"), "changer", 1, 3)
    lib.set_location(disc_id(db, "Nevermind"), "shelf", note="top shelf")
    tds = album_id(db, "The Downward Spiral")
    db.execute("UPDATE albums SET runtime_sec = NULL WHERE id = ?", (tds,))
    for n in range(1, 15):
        db.execute("INSERT INTO tracks (album_id, number, title, duration_sec) VALUES (?,?,?,10)", (tds, n, f"T{n}"))
    player = Player(db, drivers, ha=None, clock=clock)
    return lib, player


def test_disc_not_in_changer_says_where_it_is(db, setup):
    _, player = setup
    with pytest.raises(PlayError, match="on the shelf \\(top shelf\\)"):
        run(player.play_album(album_id(db, "Nevermind")))


def test_full_album_play_scores_full_listen(db, setup, clock):
    _, player = setup
    run(player.play_album(album_id(db, "The Downward Spiral"), user="Adam"))
    for _ in range(80):  # 14 tracks x 10 s, polled every 2 s
        clock.advance(2)
        run(player.poll())
    assert player.active is None
    s = db.one("SELECT * FROM sessions")
    assert (s["completed"], s["end_reason"], s["last_track"], s["user_name"]) == (1, "finished", 14, "Adam")
    assert s["played_sec"] >= 126


def test_switching_changers_stops_the_other_and_closes_session(db, setup, drivers, clock):
    _, player = setup
    run(player.play_album(album_id(db, "The Downward Spiral")))
    clock.advance(4)
    run(player.poll())
    run(player.play_album(album_id(db, "Dirt")))
    assert run(drivers[2].status()).state == "stopped"
    assert run(drivers[1].status()).state == "playing"
    first = db.one("SELECT * FROM sessions ORDER BY id LIMIT 1")
    assert (first["end_reason"], first["completed"]) == ("replaced", 0)


def test_stop_then_resume_picks_up_at_last_track(db, setup, clock):
    _, player = setup
    tds = album_id(db, "The Downward Spiral")
    run(player.play_album(tds))
    for _ in range(23):  # 46 s -> track 5
        clock.advance(2)
        run(player.poll())
    run(player.transport("stop"))
    np = run(player.resume_album(tds))
    assert np["track"] == 5


def test_continue_listening_lists_partial_albums(db, setup, clock):
    lib, player = setup
    run(player.play_album(album_id(db, "Dirt")))
    clock.advance(25)
    run(player.poll())
    run(player.transport("stop"))
    rows = lib.continue_listening()
    assert [r["title"] for r in rows] == ["Dirt"]
    assert rows[0]["last_track"] == 3


def test_pause_does_not_count_time(db, setup, clock):
    _, player = setup
    run(player.play_album(album_id(db, "The Downward Spiral")))
    clock.advance(5)
    run(player.poll())
    run(player.transport("pause"))
    clock.advance(300)
    run(player.poll())
    assert player.active.played_sec == pytest.approx(5)


def test_score_full_listen_rules():
    assert score_full_listen(played_sec=2700, runtime_sec=3000, start_track=1, last_track=12,
                             track_count=12, finished=False)
    assert not score_full_listen(played_sec=2600, runtime_sec=3000, start_track=1, last_track=12,
                                 track_count=12, finished=False)
    assert score_full_listen(played_sec=0, runtime_sec=None, start_track=1, last_track=10,
                             track_count=10, finished=True)
    assert not score_full_listen(played_sec=0, runtime_sec=None, start_track=5, last_track=10,
                                 track_count=10, finished=True)
    assert score_full_listen(played_sec=0, runtime_sec=None, start_track=1, last_track=1,
                             track_count=None, finished=True)


# --- stats and vibe -----------------------------------------------------------
def test_streaks():
    t = date(2026, 10, 25)
    days = [t - timedelta(days=d) for d in (10, 9, 8, 7, 2, 1, 0)]
    assert _streaks(sorted(days), t) == (3, 4)
    assert _streaks([t - timedelta(days=5)], t) == (0, 1)


def test_stats_counts(db, setup, clock):
    lib, player = setup
    run(player.play_album(album_id(db, "The Downward Spiral")))
    for _ in range(80):
        clock.advance(2)
        run(player.poll())
    run(player.play_album(album_id(db, "Dirt")))
    clock.advance(10)
    run(player.poll())
    run(player.transport("stop"))
    s = lib.stats()
    assert (s["full_listens"], s["partial_listens"], s["albums_played"]) == (1, 1, 2)
    run(player.play_album(album_id(db, "Dirt")))  # in progress: not a partial yet
    assert lib.stats()["partial_listens"] == 1
    assert s["albums_owned"] == 4  # audio, owned: TDS, Dirt, Nevermind, Dark Side
    assert s["streak_days"] == 1


def test_vibe_only_picks_playable_albums_with_honest_reasons(db, setup):
    lib, _ = setup
    picks = lib.vibe(familiarity="rediscover", seed=1)
    assert {p["title"] for p in picks} == {"The Downward Spiral", "Dirt"}
    assert all("Not played here yet" in p["reason"] for p in picks)
