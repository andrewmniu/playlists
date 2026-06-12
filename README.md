# playlists

Personal Spotify listening-history pipeline and playlist automation.

Originally ran on AWS (RDS MySQL + Lambda + GitHub Actions); migrated to a single
small VPS (Hetzner) running Postgres, with jobs scheduled by systemd timers. The
dashboard that visualizes this data lives in a separate repo (`music-dashboard`)
and reads the same Postgres database.

## Layout

| Path | What it is |
|------|------------|
| `ingest/` | Hourly job: pulls recently-played tracks from Spotify into Postgres (`ingest_history.py`). Timestamps stored as canonical UTC. |
| `playlists/` | Playlist jobs that read Postgres and update Spotify: `on_this_day.py` (yesteryear, daily), `random_day.py` (random past day, daily), `unabridged.py` (everything played ≥10×, daily, diff-synced), `monthly_top.py` (top 30 of last month, 1st of month). Shared helpers in `db.py`, Spotify client in `spotify_client.py`. |
| `migrations/` | Postgres schema migrations (RDS→PG lift-and-shift, then UTC conversion). |
| `deploy/` | `setup-server.sh` (one-time VPS bootstrap) and the systemd `.service`/`.timer` units. |

## Data model

Two tables: `tracks` (id, name, album, artist) and `history` (played_at `timestamptz`,
track_id). `played_at` is canonical UTC. "Local day" for the date-based playlists is
computed per era — Eastern before the ~2023-08-20 move west, Pacific after (see
`playlists/db.py`).

## Running

Jobs run on the VPS via systemd timers; secrets live in `~/music-dashboard/app.env`
on the box (`DATABASE_URL`, `SPOTIFY_*`). To run a job manually:

```bash
ssh playlists-vps
cd ~/music-dashboard/playlists && set -a && . ../app.env && set +a
../venv/bin/python unabridged.py
```

Schedules: ingestion hourly; daily playlists at 00:00 PT; monthly top tracks on the
1st at 00:00 PT. `.env.example` lists the environment variables; the real `.env`
(VPS + Spotify reference) is gitignored.
