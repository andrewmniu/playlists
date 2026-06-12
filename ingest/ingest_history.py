"""Hourly Spotify listening-history ingestion.

Reads the most recent play already stored, asks Spotify for everything
played since, and upserts into Postgres.

Timestamps are stored as canonical UTC (timestamptz). Spotify returns
played_at in UTC, so we store the instant directly; local time-of-day is
a presentation concern handled at query/display time.
"""
import os
from datetime import datetime

import psycopg

from spotify import Spotify


def get_after_ms(conn):
    """Unix-ms cursor for Spotify's `after` param: 1s past the last stored play.

    played_at is timestamptz, so psycopg returns a timezone-aware datetime
    and .timestamp() yields the correct epoch directly. Returns None on an
    empty table (first run grabs the latest 50).
    """
    row = conn.execute("SELECT MAX(played_at) FROM history").fetchone()
    if not row or row[0] is None:
        return None
    return int((row[0].timestamp() + 1) * 1000)


def parse_played_at(iso_utc):
    """'2024-01-01T12:00:00.123Z' -> aware UTC datetime, second precision."""
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).replace(microsecond=0)


def main():
    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn) as conn:
        after = get_after_ms(conn)
        items = Spotify().recently_played(after_ms=after)
        if not items:
            print("no new plays")
            return

        # Each row: (played_at_utc, track_id, name, album, artist). recently-played
        # already carries full track objects, so no extra /tracks call is needed.
        rows = []
        for item in items:
            track = item["track"]
            rows.append((
                parse_played_at(item["played_at"]),
                track["id"],
                track["name"],
                track["album"]["name"],
                track["artists"][0]["name"],
            ))

        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tracks (id, name, album, artist) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                [(r[1], r[2], r[3], r[4]) for r in rows],
            )
            cur.executemany(
                "INSERT INTO history (played_at, track_id) "
                "VALUES (%s, %s) ON CONFLICT (played_at) DO NOTHING",
                [(r[0], r[1]) for r in rows],
            )
        conn.commit()
        # rows[0] is the newest play in this batch.
        print(f"fetched {len(rows)} play(s); newest {rows[0][0]:%Y-%m-%d %H:%M} UTC "
              f"— {rows[0][2]} by {rows[0][4]}")


if __name__ == "__main__":
    main()
