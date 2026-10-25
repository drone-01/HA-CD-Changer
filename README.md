# Disc Library for Home Assistant

An album-first app for browsing and playing a physical disc collection across
two Sony BDP-CX7000ES 400-disc changers. It runs as a Home Assistant add-on
with its own sidebar panel, designed for the iPhone first.

## What works today (v0.1.0)

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
| Cover art, track lists, genre tagging | Phase 2 |
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

## Notes

- The spreadsheet stays the catalog master for now. Re-importing refreshes catalog fields
  and never touches locations, favorites, genres or history.
- Multi-disc sets currently import as one album per row, matching the spreadsheet.
- A session left open by a restart is closed as "interrupted" on start-up.
