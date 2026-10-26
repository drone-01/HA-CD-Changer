# Disc Library for Home Assistant

An album-first app for browsing and playing a physical disc collection across
two Sony BDP-CX7000ES 400-disc changers. It runs as a Home Assistant add-on
with its own sidebar panel, designed for the iPhone first.

## What works today (v0.6.0)

| Area | Status |
| --- | --- |
| Add-on, sidebar panel, ingress | Done |
| Import of `CD_Collection.xlsx` (all three tabs, re-runnable) | Done |
| Disc locations: changer + slot, shelf, other player, loaned; next free slot; slot conflicts | Done |
| Play an album: finds the disc, stops the other changer, runs the Listen script, plays | Done |
| Pause, resume, next, previous, stop | Done |
| Listening history, resume at last track, "pick up where you left off" | Done |
| Full listen scoring (90% of runtime in one sitting) | Done |
| Stats: full and partial listens, hours, albums played, streaks, by genre, most played | Done |
| Vibe Sesh picks (time and familiarity now; mood and energy once albums are tagged) | Done |
| Favorites, search, genre filter, sort | Done |
| Phone-first web app (Library, Vibe Sesh, Playing, Stats, album sheet) | Done |
| Simulated changers for testing without hardware | Done |
| **Real CX7000ES control** | **Shell only, waiting on Sony's protocol sheet (Phase 1)** |
| Cover art, track lists, runtimes, genres (MusicBrainz + Cover Art Archive, Apple Music artwork fallback) | Done |
| Pressing verification: see, edit or enter catalog no. / barcode, search releases, lock; review screen; bulk lock; Excel export | Done (v0.3.0) |
| HA `media_player` entities (Theatrium, voice, dashboard card) | Phase 2 |
| Theater dashboard and Listen/Watch scripts | Phase 0, built in HA |

## Install

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/drone-01/HA-CD-Changer`. If the repository is private, HA can't read it;
   make it public or keep it private and install the add-on as a local add-on instead.
2. Install **Disc Library**, start it, and turn on **Show in sidebar**.
3. Open **Disc Library** from the sidebar and use **Import catalog** with `CD_Collection.xlsx`.

Requires Home Assistant OS or Supervised (add-ons need the Supervisor).

## Configuration

```yaml
changers:
  - name: Changer 1
    driver: simulated      # switch to cx7000es in Phase 1
  - name: Changer 2
    driver: simulated
listen_script: script.listen   # optional: run before each album plays
sim_track_seconds: 30          # simulated changers only
log_level: info
```

For a real changer: `driver: cx7000es`, plus `host` (the changer's IP, or the iTach's IP)
and `port` (from Sony's spec for network control, or 4999 for the iTach serial port).

## Layout

```
disc_library/
  config.yaml, Dockerfile, requirements.txt
  app/            Python backend (FastAPI + SQLite in /data)
    drivers/      changer drivers: simulated, cx7000es (shell)
    importer.py   spreadsheet import
    library.py    browse, locations, stats, Vibe Sesh
    player.py     play orchestration and listen tracking
    main.py       API + static web app on port 8099 (ingress)
  www/            phone-first web app (no build step)
  tests/          pytest suite (runs against simulated changers)
```

## Develop locally

```bash
cd disc_library
pip install -r requirements.txt pytest
python -m pytest -q
DL_DATA_DIR=./data python -m app.main     # http://localhost:8099
```

## Album metadata

After an import the app fetches cover art, track lists, runtimes and genres in the background
(about 4 seconds an album, so roughly 30 minutes for the whole collection; MusicBrainz allows one
request a second). Covers are saved in the add-on's data folder, so the grid stays fast offline.

- Matching uses artist, title and catalog number; a catalog match (including both numbers on
  BMG/Columbia House club editions) pins the exact pressing.
- Notes in titles ("— Disc 2", "— second copy", "(CD single)") are ignored for matching, and the
  disc number picks the right track list.
- Anything uncertain is left unmatched rather than guessed.
- If the Cover Art Archive can't be reached, covers come from Apple Music instead (matched on artist
  and album). The job stops itself with a message if MusicBrainz can't be reached at all.

## Value and collection stats

- Each album shows its market value and what you paid. Paid prices are read from the eBay notes in
  the spreadsheet ("bought for $X", or the order total); both can be typed in on the album.
- Discogs: paste a personal access token in Settings → Value (Discogs → Settings → Developers →
  Generate new token). Each album is matched by barcode, catalog number, then artist and title, and
  priced with Discogs' VG price suggestion (condition changeable), or the lowest listing when there's no
  suggestion. Typed-in values are never overwritten; prices refresh every 30 days. On-order discs aren't
  priced.
- Value tiers: $ under $9, $$ $9–24, $$$ $25 and up. Marks on library covers are off by default
  (Settings → Value). The library can be sorted by value.
- Stats → Collection: albums, discs, formats, changer slots used and free, discs not in a changer,
  total play time, collection value, paid, average value, cost per listen, most valuable.

## Multi-disc sets

A set is one album with one entry per disc, and each disc has its own location, so the discs of a set
can sit in different slots and on different changers. Spreadsheet rows like "— Disc 2", "— Book 2",
"(Disc One)" or notes like "2-CD set — DISC 2" are grouped automatically. For sets named
inconsistently, open the album → "Part of a multi-disc set?" to link another album in as a disc (or
split one back out). Playing a set goes straight through, switching changers when needed.

## Removing albums

Remove an album from the bottom of its page. The confirmation lists every slot its discs are in.
Those slots stay reserved until you pull the discs and mark them pulled (Library → "Pull N discs"),
so nothing new is sent into a slot that's still full. An Undo appears for a few seconds. Re-importing the spreadsheet won't bring it back; restore it from
Settings → Removed albums (it goes back into its old slot if that slot is still free).

## Settings

The **Settings** tab holds what you can change without restarting: the full-listen threshold,
the theater script, Apple Music cover fallback, whether new albums are looked up after an import,
album lookup controls, spreadsheet import, pressing export, changer status, storage used, and the
version. Changer names and addresses stay in the app configuration in Home Assistant.

## Pressings

Each album's **Pressing** card shows the catalog number on your disc, the barcode, and the exact
MusicBrainz release it's matched to (label, country, date, format, catalog number, track count).

- **Confirm & lock** pins the album to that release.
- **Change pressing / Find pressing**: type the catalog number or barcode, search, and lock the right
  release. Locking always pulls track list, runtime and details from that exact release.
- Locked albums are never re-matched, and re-importing the spreadsheet doesn't change them.
- **Library → Verify pressings** lists albums to review, ones matched on catalog number (lock them all
  at once), unmatched ones, and locked ones, and exports everything to Excel.
- Genres you set yourself are never overwritten.

## Notes

- The spreadsheet stays the catalog master for now. Re-importing refreshes catalog fields
  and never touches locations, favorites, genres or history.
- Multi-disc sets currently import as one album per row, matching the spreadsheet.
- A session left open by a restart is closed as "interrupted" on start-up.
