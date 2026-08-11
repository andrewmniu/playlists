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

from album_editions import ALBUM_MIN_TRACKS
from spotify import Spotify


def get_after_ms(conn):
    """Unix-ms cursor for Spotify's `after` param: 1s past the last stored play.

    played_at is timestamptz, so psycopg returns a timezone-aware datetime
    and .timestamp() yields the correct epoch directly. Returns None on an
    empty table (first run grabs the latest 50).
    """
    row = conn.execute(
        "SELECT MAX(played_at) FROM v2.history WHERE user_id = 1"
    ).fetchone()
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


def _upgrade_album(cur, track_id, album_id, album, track_spotify_id):
    """Move an existing track onto this release if it's the better home.

    Identity is the isrc, so a song released as a single ahead of its album
    resolves to the row created for the single -- and used to keep that row's
    1-track album forever, stranding the song off the record it belongs to
    (Reality Awaits sat at 7 of 9 tracks this way). When the album turns up
    later, take it.

    Deliberately one-directional: only a single-length release gets upgraded, so
    two full editions of the same record can't fight over a track and rewrite
    album_id on every play. Choosing between *those* is the backfill's job
    (enrich_album_editions.py), where play counts are in view.
    """
    if (album.get("total_tracks") or 0) < ALBUM_MIN_TRACKS:
        return
    # spotify_id moves with album_id. It is what the playlist jobs push to
    # Spotify, so leaving it behind files the song under the album here while
    # playlists still add the single, under the single's cover. Skipped if the
    # album's id already belongs to another row -- the column is unique, and
    # merging two tracks as a side effect of a play is not this function's job.
    row = cur.execute(
        """UPDATE v2.tracks t SET album_id = %s, updated_at = now(),
                  spotify_id = CASE WHEN EXISTS (
                      SELECT 1 FROM v2.tracks o WHERE o.spotify_id = %s AND o.id <> t.id)
                      THEN t.spotify_id ELSE %s END
           FROM v2.albums a
           WHERE t.id = %s AND a.id = t.album_id AND t.album_id <> %s
             AND coalesce(a.total_tracks, 0) < %s
           RETURNING a.id, t.spotify_id""",
        (album_id, track_spotify_id, track_spotify_id, track_id, album_id, ALBUM_MIN_TRACKS),
    ).fetchone()
    if row:
        cur.execute(
            """INSERT INTO v2.track_album_repairs
                   (track_id, old_album_id, new_album_id, reason)
               VALUES (%s, %s, %s, 'ingest: single superseded by album')""",
            (track_id, row[0], album_id),
        )


def upsert_v2(cur, item, source="spotify"):
    """Insert one play into the normalized v2 schema (artists->albums->tracks->
    track_artists->history), resolving canonical identity by isrc/spotify_id.

    Artist identity is the name (matches the 0003 backfill); album by spotify_id;
    track canonically by isrc (so a regional-dup spotify id maps to the existing
    track) and only inserted under its own spotify id when the isrc is new/unknown.

    `source` tags the history row; backfill_export.py passes 'spotify_export' so
    imported plays stay distinguishable from hourly-ingested ones.
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
        """INSERT INTO v2.albums (spotify_id, name, artist_id, image_url, release_date,
                                  total_tracks, album_type)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (spotify_id) DO UPDATE
             SET total_tracks = EXCLUDED.total_tracks,
                 album_type = COALESCE(EXCLUDED.album_type, v2.albums.album_type),
                 image_url = COALESCE(v2.albums.image_url, EXCLUDED.image_url)
           RETURNING id""",
        (album["id"], album["name"], artist_id, _album_image(album),
         _release_date(album), album.get("total_tracks"), album.get("album_type")),
    )
    album_id = cur.fetchone()[0]

    isrc = (track.get("external_ids") or {}).get("isrc") or ""
    track_id = None
    if isrc:
        row = cur.execute("SELECT id FROM v2.tracks WHERE isrc = %s", (isrc,)).fetchone()
        if row:
            track_id = row[0]
            _upgrade_album(cur, track_id, album_id, album, track["id"])
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
           VALUES (1, %s, %s, %s) ON CONFLICT (user_id, played_at) DO NOTHING""",
        (parse_played_at(item["played_at"]), track_id, source),
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
            # Write the normalized v2 schema. The raw public.* tables were dropped
            # after the migration cutover; v2 is the single source of truth now.
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
