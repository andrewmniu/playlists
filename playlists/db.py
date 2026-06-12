"""Shared Postgres helpers for the playlist jobs.

Plays are stored in UTC. A "local day" is defined using the timezone I
actually lived in at the time: Eastern before the ~Aug 20 2023 move west,
Pacific after. LOCAL_TZ (current home) is used for "today".
"""
import os

import psycopg

EASTERN = "America/New_York"
PACIFIC = "America/Los_Angeles"
LOCAL_TZ = PACIFIC                       # current home; used to compute "today"
MOVE_INSTANT = "2023-08-20 00:00:00-07"  # ~when I moved east -> west


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
    """Track ids played on the given local calendar `day`, in play order."""
    rows = conn.execute(
        f"""SELECT t.id
            FROM history h JOIN tracks t ON t.id = h.track_id
            WHERE {_local_date('h.played_at')} = %s
            ORDER BY h.played_at""",
        (day,),
    ).fetchall()
    return [r[0] for r in rows]


def random_day_with_plays(conn):
    """A uniformly random local calendar day that has at least one play."""
    row = conn.execute(
        f"""SELECT {_local_date('played_at')} AS d
            FROM history GROUP BY d ORDER BY random() LIMIT 1"""
    ).fetchone()
    return row[0]


def top_tracks_in_range(conn, start, end, limit):
    """Most-played canonical track ids whose local date is in [start, end].

    Ordered by play count desc, ties broken by most recent play. Tracks are
    canonicalized (same name/album/artist -> one id) like the unabridged job.
    """
    rows = conn.execute(
        f"""
        WITH canonical_tracks AS (
            SELECT name, album, artist, MIN(id) AS canonical_id
            FROM tracks GROUP BY name, album, artist
        ),
        month_plays AS (
            SELECT ct.canonical_id AS id, h.played_at
            FROM history h
            JOIN tracks t ON h.track_id = t.id
            JOIN canonical_tracks ct
              ON t.name = ct.name AND t.album = ct.album AND t.artist = ct.artist
            WHERE {_local_date('h.played_at')} BETWEEN %s AND %s
        )
        SELECT id FROM month_plays
        GROUP BY id
        ORDER BY COUNT(*) DESC, MAX(played_at) DESC
        LIMIT %s
        """,
        (start, end, limit),
    ).fetchall()
    return [r[0] for r in rows]
