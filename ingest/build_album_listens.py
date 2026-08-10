"""
Populate v2.album_listens and v2.album_pass_state from v2.history.

Algorithm (sequential coverage, time-agnostic):
  For each album, process its plays in chronological order.
  N = the album's real length: core_tracks (the standard edition, padding
      excluded) falling back to total_tracks. Albums shorter than
      ALBUM_MIN_TRACKS are singles and are skipped, as are albums Spotify never
      gave us a length for.
  Maintain a "pass" (set of unique tracks seen so far).
  When a repeat track triggers len(seen)/N >= COVER:
    - Close the pass → insert a row in album_listens
    - Start a new pass with the triggering track
  At end-of-history: if remaining pass has coverage >= COVER, close it too.
  Whatever is left in-progress is saved in album_pass_state.

N is Spotify's number, never "distinct tracks I happened to hear". Deriving N
from the history makes coverage self-fulfilling -- one song off a record played
35 times is 35 passes at 1/1 -- which is the whole bias this counter exists to
remove. The cost of honesty is that half-listened albums score 0, which is
correct: they were never listened to.

Plays of padding (v2.tracks.is_variant -- the live and demo takes a deluxe
appends past the standard edition) are dropped rather than counted toward a
pass, so a bonus disc can't stand in for the album.

Idempotent: truncates both tables before re-inserting, in one transaction.

Nothing maintains these tables incrementally -- ingest doesn't touch them -- so
album-listens.timer runs this daily. Left to manual runs they go stale silently:
they once sat ~7 weeks behind, which surfaced as the dashboard's Favorites tab
reporting zero album plays for any recent range.

    DATABASE_URL=... python ingest/build_album_listens.py
"""

import os, sys
from collections import defaultdict
import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from album_editions import ALBUM_MIN_TRACKS

DATABASE_URL = os.environ["DATABASE_URL"]
COVER   = 0.6
USER_ID = 1


def run():
    with psycopg.connect(DATABASE_URL) as conn:
        print("Loading plays from v2.history …")
        rows = conn.execute("""
            SELECT h.track_id, t.album_id, h.played_at
            FROM v2.history h
            JOIN v2.tracks  t ON t.id = h.track_id
            WHERE h.user_id = %s
              AND t.album_id IS NOT NULL
              AND NOT t.is_variant
            ORDER BY h.played_at
        """, [USER_ID]).fetchall()
        print(f"  {len(rows):,} play-rows loaded (padding tracks excluded)")

        # N per album: the standard edition's length, falling back to the whole
        # tracklist when there is no padding to strip.
        album_n = dict(conn.execute("""
            SELECT id, coalesce(core_tracks, total_tracks)
            FROM v2.albums
            WHERE coalesce(core_tracks, total_tracks) IS NOT NULL
        """).fetchall())

        # Group plays by album_id, preserving chronological order
        album_plays: dict[int, list] = defaultdict(list)
        for track_id, album_id, played_at in rows:
            album_plays[album_id].append((track_id, played_at))

        print(f"  {len(album_plays):,} distinct albums in history")

        unknown = sum(1 for a in album_plays if a not in album_n)
        eligible = sum(1 for a in album_plays if album_n.get(a, 0) >= ALBUM_MIN_TRACKS)
        print(f"  {eligible:,} albums eligible (N >= {ALBUM_MIN_TRACKS}); "
              f"{unknown:,} skipped for unknown length")

        # Run the algorithm
        listen_rows   = []  # (user_id, album_id, started_at, ended_at, distinct, total, n, coverage)
        pass_state_rows = []  # (user_id, album_id, seen_track_ids[], pass_started_at, last_played_at)

        for album_id, plays in album_plays.items():
            n = album_n.get(album_id, 0)
            if n < ALBUM_MIN_TRACKS:
                continue

            seen        = set()
            pass_start  = None
            pass_total  = 0
            last_at     = None

            for track_id, played_at in plays:
                last_at = played_at

                if track_id not in seen:
                    seen.add(track_id)
                    if pass_start is None:
                        pass_start = played_at
                    pass_total += 1
                else:
                    # Repeat track: check coverage
                    if seen and len(seen) / n >= COVER:
                        # Close this pass
                        listen_rows.append((
                            USER_ID, album_id,
                            pass_start, played_at,
                            len(seen), pass_total, n,
                            round(len(seen) / n, 4),
                        ))
                        # New pass starts with this repeat track
                        seen        = {track_id}
                        pass_start  = played_at
                        pass_total  = 1

            # End-of-history: close if coverage met
            if seen and len(seen) / n >= COVER:
                listen_rows.append((
                    USER_ID, album_id,
                    pass_start, last_at,
                    len(seen), pass_total, n,
                    round(len(seen) / n, 4),
                ))
                seen       = set()
                pass_start = None
                pass_total = 0

            # Save in-progress pass state (may be empty set if just completed)
            if seen:
                pass_state_rows.append((
                    USER_ID, album_id,
                    list(seen), pass_start, last_at,
                ))

        print(f"\nResults:")
        print(f"  {len(listen_rows):,} album listens")
        print(f"  {len(pass_state_rows):,} in-progress passes saved")

        # ── Write to DB (idempotent) ──────────────────────────────────────────
        print("\nWriting to DB …")
        with conn.cursor() as cur:
            cur.execute("TRUNCATE v2.album_listens RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE v2.album_pass_state")

            if listen_rows:
                cur.executemany("""
                    INSERT INTO v2.album_listens
                        (user_id, album_id, started_at, ended_at,
                         distinct_count, total_count, n_tracks, coverage)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """, listen_rows)

            if pass_state_rows:
                cur.executemany("""
                    INSERT INTO v2.album_pass_state
                        (user_id, album_id, seen_track_ids, pass_started_at, last_played_at)
                    VALUES (%s,%s,%s,%s,%s)
                """, pass_state_rows)

        conn.commit()
        print("  Done.")

        # ── Sanity-check summary ──────────────────────────────────────────────
        print("\nTop 15 albums by listen count:")
        top = conn.execute("""
            SELECT ar.name, al.name, count(*) AS listens,
                   coalesce(al.core_tracks, al.total_tracks) AS n,
                   round(avg(al2.coverage)::numeric, 2) AS avg_cov
            FROM v2.album_listens al2
            JOIN v2.albums al ON al.id = al2.album_id
            JOIN v2.artists ar ON ar.id = al.artist_id
            WHERE al2.user_id = %s
            GROUP BY ar.name, al.name, n
            ORDER BY listens DESC
            LIMIT 15
        """, [USER_ID]).fetchall()
        print(f"  {'listens':>7}  {'N':>3}  {'avg_cov':>7}  album — artist")
        for artist, album, listens, n, cov in top:
            print(f"  {listens:>7}  {n:>3}  {cov:>7}  {album[:35]:35} — {artist[:20]}")

        total_listens = conn.execute(
            "SELECT count(*) FROM v2.album_listens WHERE user_id=%s", [USER_ID]
        ).fetchone()[0]
        distinct_albums = conn.execute(
            "SELECT count(DISTINCT album_id) FROM v2.album_listens WHERE user_id=%s", [USER_ID]
        ).fetchone()[0]
        print(f"\nHeadline: {total_listens:,} album listens across {distinct_albums:,} albums")


if __name__ == "__main__":
    run()
