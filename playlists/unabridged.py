"""Daily 'unabridged' playlist: every track played >= 10 times.

Ordered by each track's 5th-play time, most recent first. Tracks are
canonicalized (same name/album/artist collapsed to one id) so regional
duplicate ids don't double-count. Refreshes via a minimal diff sync.

The last-synced id list is cached to a file. Each run diffs the freshly
computed desired list against that cache in Python, so neither no-op days
nor change days need to read the ~4,400-track playlist back from Spotify:
  - unchanged  -> zero Spotify calls
  - changed    -> only the insert/remove write calls for what differs
Delete the state file to force a fresh read/reconcile from Spotify (also
what happens automatically on the first run, before a cache exists).
"""
from pathlib import Path

from db import connect
from spotify_client import SpotifyAPI

PLAYLIST_ID = "2eImE8S0JZBnHZwCafDtII"
STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "unabridged.ids"

QUERY = """
WITH canonical_tracks AS (
    SELECT name, album, artist, MIN(id) AS canonical_id
    FROM tracks
    GROUP BY name, album, artist
),
ranked AS (
    SELECT ct.canonical_id AS id,
           h.played_at,
           ROW_NUMBER() OVER (PARTITION BY ct.canonical_id ORDER BY h.played_at) AS listen_number
    FROM history h
    JOIN tracks t ON h.track_id = t.id
    JOIN canonical_tracks ct
      ON t.name = ct.name AND t.album = ct.album AND t.artist = ct.artist
)
-- One pass: keep tracks with >= 10 plays (HAVING) and order by 5th-play time.
-- The original's ranked-JOIN-qualifying did this filter as a join, which the
-- planner turned into a 549M-row nested loop; HAVING avoids it entirely.
-- id is a deterministic tiebreaker so the order is stable across runs.
SELECT id
FROM ranked
GROUP BY id
HAVING COUNT(*) >= 10
ORDER BY MAX(CASE WHEN listen_number = 5 THEN played_at END) DESC, id
"""


def load_cache():
    """Last-synced id list, or None if there's no cache yet."""
    if STATE_FILE.exists():
        return STATE_FILE.read_text().splitlines()
    return None


def main():
    with connect() as conn:
        desired = [row[0] for row in conn.execute(QUERY).fetchall()]

    if not desired:
        print("no qualifying tracks")
        return

    cached = load_cache()
    if cached == desired:
        print(f"unchanged ({len(desired)} tracks); skipping Spotify sync")
        return

    # cached is None on the first run -> sync reads the playlist to establish a
    # baseline; otherwise we diff against the cache without reading Spotify.
    result = SpotifyAPI().sync_playlist(PLAYLIST_ID, desired, current_ids=cached)

    # Record the new state only after a successful sync, so a failure retries.
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text("\n".join(desired))
    print(f"unabridged synced: {len(desired)} tracks total — {result}")


if __name__ == "__main__":
    main()
