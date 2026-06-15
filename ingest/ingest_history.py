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


def _album_image(album):
    """Medium (300px) album-art url if present, else largest, else None."""
    imgs = album.get("images") or []
    return imgs[1]["url"] if len(imgs) > 1 else (imgs[0]["url"] if imgs else None)


def _release_date(album):
    """Spotify release_date (year/month/day precision) -> full date or None."""
    d, prec = album.get("release_date"), album.get("release_date_precision")
    if not d or not d[:4].isdigit() or int(d[:4]) < 1:
        return None
    if prec == "year":
        return f"{d[:4]}-01-01"
    if prec == "month":
        return f"{d[:7]}-01"
    return d


def upsert_v2(cur, item):
    """Insert one play into the normalized v2 schema (artists->albums->tracks->
    track_artists->history), resolving canonical identity by isrc/spotify_id.

    Artist identity is the name (matches the 0003 backfill); album by spotify_id;
    track canonically by isrc (so a regional-dup spotify id maps to the existing
    track) and only inserted under its own spotify id when the isrc is new/unknown.
    """
    track = item["track"]
    artist = track["artists"][0]
    cur.execute(
        """INSERT INTO v2.artists (spotify_id, name) VALUES (%s, %s)
           ON CONFLICT (name) DO UPDATE
             SET spotify_id = COALESCE(v2.artists.spotify_id, EXCLUDED.spotify_id)
           RETURNING id""",
        (artist.get("id"), artist["name"]),
    )
    artist_id = cur.fetchone()[0]

    album = track["album"]
    cur.execute(
        """INSERT INTO v2.albums (spotify_id, name, artist_id, image_url, release_date, total_tracks)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (spotify_id) DO UPDATE
             SET total_tracks = EXCLUDED.total_tracks,
                 image_url = COALESCE(v2.albums.image_url, EXCLUDED.image_url)
           RETURNING id""",
        (album["id"], album["name"], artist_id, _album_image(album),
         _release_date(album), album.get("total_tracks")),
    )
    album_id = cur.fetchone()[0]

    isrc = (track.get("external_ids") or {}).get("isrc") or ""
    track_id = None
    if isrc:
        row = cur.execute("SELECT id FROM v2.tracks WHERE isrc = %s", (isrc,)).fetchone()
        if row:
            track_id = row[0]
    if track_id is None:
        cur.execute(
            """INSERT INTO v2.tracks (spotify_id, isrc, name, album_id, duration_ms, preview_url)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (spotify_id) DO UPDATE SET name = EXCLUDED.name
               RETURNING id""",
            (track["id"], isrc, track["name"], album_id,
             track.get("duration_ms"), track.get("preview_url")),
        )
        track_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO v2.track_artists (track_id, artist_id, position)
               VALUES (%s, %s, 0) ON CONFLICT DO NOTHING""",
            (track_id, artist_id),
        )

    cur.execute(
        """INSERT INTO v2.history (user_id, played_at, track_id, source)
           VALUES (1, %s, %s, 'spotify') ON CONFLICT (user_id, played_at) DO NOTHING""",
        (parse_played_at(item["played_at"]), track_id),
    )


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
            # Legacy raw tables (public.*) — still the source the dashboard reads.
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
            # Normalized v2.* (dual-write during migration). Same transaction, so
            # public and v2 stay consistent; one fails -> both roll back.
            for item in items:
                upsert_v2(cur, item)
        conn.commit()

        # Keep the artist-trends rollup current (concurrent => no read lock).
        conn.autocommit = True
        conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY v2.artist_daily_plays")

        # rows[0] is the newest play in this batch.
        print(f"fetched {len(rows)} play(s); newest {rows[0][0]:%Y-%m-%d %H:%M} UTC "
              f"— {rows[0][2]} by {rows[0][4]}")


if __name__ == "__main__":
    main()
