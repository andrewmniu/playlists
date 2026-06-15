"""Shared Postgres helpers for the playlist jobs.

Reads the normalized `v2` schema. Plays are stored in UTC; a "local day" uses
the timezone I actually lived in at the time: Eastern before the ~Aug 20 2023
move west, Pacific after. LOCAL_TZ (current home) is used for "today".

The surrogate `v2.tracks.id` is already canonical (regional duplicates were
collapsed by isrc in migration 0004), so the old runtime MIN(id) GROUP BY
name/album/artist canonicalization is gone. The Spotify API speaks Spotify
ids, so every helper resolves `tracks.id -> tracks.spotify_id` at the edge and
returns spotify ids.
"""
import os

import psycopg

EASTERN = "America/New_York"
PACIFIC = "America/Los_Angeles"
LOCAL_TZ = PACIFIC                       # current home; used to compute "today"
MOVE_INSTANT = "2023-08-20 00:00:00-07"  # ~when I moved east -> west
USER_ID = 1                              # single user for now


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"])


def _local_date(col):
    """SQL expr: a play's local calendar date, in the era's timezone."""
    return (
        f"({col} AT TIME ZONE "
        f"CASE WHEN {col} < TIMESTAMPTZ '{MOVE_INSTANT}' "
        f"THEN '{EASTERN}' ELSE '{PACIFIC}' END)::date"
    )


def tracks_on_local_date(conn, day):
    """Spotify ids of tracks played on the given local calendar `day`, in order."""
    rows = conn.execute(
        f"""SELECT t.spotify_id
            FROM v2.history h JOIN v2.tracks t ON t.id = h.track_id
            WHERE h.user_id = %s AND {_local_date('h.played_at')} = %s
            ORDER BY h.played_at""",
        (USER_ID, day),
    ).fetchall()
    return [r[0] for r in rows]


def random_day_with_plays(conn):
    """A uniformly random local calendar day that has at least one play."""
    row = conn.execute(
        f"""SELECT {_local_date('played_at')} AS d
            FROM v2.history WHERE user_id = %s
            GROUP BY d ORDER BY random() LIMIT 1""",
        (USER_ID,),
    ).fetchone()
    return row[0]


def top_tracks_in_range(conn, start, end, limit):
    """Spotify ids of the most-played tracks whose local date is in [start, end].

    Ordered by play count desc, ties broken by most recent play. No canonical
    collapsing needed -- tracks.id is already canonical.
    """
    rows = conn.execute(
        f"""
        SELECT t.spotify_id
        FROM v2.history h
        JOIN v2.tracks t ON t.id = h.track_id
        WHERE h.user_id = %s AND {_local_date('h.played_at')} BETWEEN %s AND %s
        GROUP BY t.id, t.spotify_id
        ORDER BY COUNT(*) DESC, MAX(h.played_at) DESC
        LIMIT %s
        """,
        (USER_ID, start, end, limit),
    ).fetchall()
    return [r[0] for r in rows]
