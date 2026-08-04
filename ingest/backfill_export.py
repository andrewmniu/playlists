"""Backfill v2.history from a Spotify "Extended streaming history" export.

For gaps the hourly ingest couldn't cover -- e.g. the 2026-07-20 -> 2026-08-03
outage after Spotify's refresh-token expiry, which /me/player/recently-played
can't recover because it only returns the last 50 plays.

The export gives `ts`, `spotify_track_uri` and `ms_played` but no track metadata
we can insert directly, so track ids are resolved through /v1/tracks (which
returns the same object shape recently-played does) and handed to ingest's own
upsert_v2. That reuse matters: upsert_v2 resolves tracks by ISRC first, so a
regional-duplicate spotify id maps onto the existing row instead of creating a
second one and re-fragmenting album/artist analytics.

Two filters worth knowing about:
  * ms_played >= 30000 -- the export logs every stream including sub-second
    skips, while recently-played only reports plays past ~30s. Without this the
    backfilled window would show inflated play counts against every other week.
  * spotify_track_uri must be non-null -- podcasts and local files have none.

Verified against this DB: export `ts` equals recently-played `played_at` exactly
(342/343 identical on a 452-play overlap sample), so no offset correction.

    python ingest/backfill_export.py --from 2026-07-20T04:52:38Z --to 2026-08-03T22:45:33Z
    python ingest/backfill_export.py --from ... --to ... --apply
"""
import argparse
import base64
import glob
import json
import os
import sys
from datetime import datetime, timezone

import psycopg
import requests

from ingest_history import parse_played_at, upsert_v2

MIN_MS_PLAYED = 30000
SOURCE = "spotify_export"
TRACKS_API = "https://api.spotify.com/v1/tracks"
TOKEN_URL = "https://accounts.spotify.com/api/token"
BATCH = 50
TIMEOUT = 30


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_streams(dumps_dir, frm, to):
    """Every qualifying music stream in (frm, to], oldest first."""
    files = sorted(glob.glob(os.path.join(dumps_dir, "Streaming_History_Audio_*.json")))
    if not files:
        raise SystemExit(f"no Streaming_History_Audio_*.json under {dumps_dir}")
    kept, skipped_short, skipped_nonmusic = [], 0, 0
    for path in files:
        with open(path) as fh:
            for r in json.load(fh):
                t = parse_ts(r["ts"])
                if not (frm < t <= to):
                    continue
                if not r.get("spotify_track_uri"):
                    skipped_nonmusic += 1
                    continue
                if (r.get("ms_played") or 0) < MIN_MS_PLAYED:
                    skipped_short += 1
                    continue
                kept.append((t, r["spotify_track_uri"].split(":")[-1], r))
    kept.sort(key=lambda x: x[0])
    print(f"scanned {len(files)} export files")
    print(f"  in window, playable : {len(kept)}")
    print(f"  dropped <30s        : {skipped_short}")
    print(f"  dropped non-music   : {skipped_nonmusic}")
    return kept


def client_token():
    """Client-credentials is enough here -- /v1/tracks needs no user scope, and
    it's exempt from the refresh-token expiry that caused the gap in the first place."""
    auth = base64.b64encode(
        f"{os.environ['SPOTIFY_CLIENT_ID']}:{os.environ['SPOTIFY_CLIENT_SECRET']}".encode()
    ).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {auth}"},
        data={"grant_type": "client_credentials"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_tracks(ids, token):
    """spotify_id -> full track object, in batches of 50."""
    out = {}
    for i in range(0, len(ids), BATCH):
        batch = ids[i : i + BATCH]
        resp = requests.get(
            TRACKS_API,
            headers={"Authorization": f"Bearer {token}"},
            params={"ids": ",".join(batch)},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        for sid, track in zip(batch, resp.json()["tracks"]):
            if track:
                out[sid] = track
        print(f"  resolved {len(out)}/{len(ids)} tracks", end="\r", file=sys.stderr)
    print(file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dumps-dir", default=os.path.expanduser("~/Documents/playlists/dumps"))
    ap.add_argument("--from", dest="frm", required=True, help="ISO-8601, exclusive")
    ap.add_argument("--to", dest="to", required=True, help="ISO-8601, inclusive")
    ap.add_argument("--apply", action="store_true", help="write to the DB (default: dry run)")
    args = ap.parse_args()

    frm, to = parse_ts(args.frm), parse_ts(args.to)
    print(f"window: {frm:%Y-%m-%d %H:%M} -> {to:%Y-%m-%d %H:%M} UTC\n")
    streams = load_streams(args.dumps_dir, frm, to)
    if not streams:
        print("nothing to import")
        return

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        stamps = [t for t, _, _ in streams]
        existing = conn.execute(
            "SELECT count(*) FROM v2.history WHERE user_id = 1 AND played_at = ANY(%s)",
            ([t.replace(microsecond=0) for t in stamps],),
        ).fetchone()[0]
        print(f"  already in history  : {existing}  (would be skipped)")
        print(f"  new rows expected   : {len(streams) - existing}\n")

        ids = sorted({sid for _, sid, _ in streams})
        print(f"resolving {len(ids)} distinct tracks via /v1/tracks...")
        tracks = fetch_tracks(ids, client_token())
        missing = [s for s in ids if s not in tracks]
        if missing:
            print(f"  WARNING: {len(missing)} ids returned nothing (relinked/removed): {missing[:5]}")

        playable = [(t, sid) for t, sid, _ in streams if sid in tracks]
        print(f"  importable plays: {len(playable)}/{len(streams)}")

        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply.")
            return

        before = conn.execute("SELECT count(*) FROM v2.history WHERE user_id = 1").fetchone()[0]
        with conn.cursor() as cur:
            for t, sid in playable:
                upsert_v2(
                    cur,
                    {"track": tracks[sid], "played_at": t.isoformat()},
                    source=SOURCE,
                )
        conn.commit()
        after = conn.execute("SELECT count(*) FROM v2.history WHERE user_id = 1").fetchone()[0]
        tagged = conn.execute(
            "SELECT count(*) FROM v2.history WHERE user_id = 1 AND source = %s", (SOURCE,)
        ).fetchone()[0]
        print(f"\ninserted {after - before} rows ({tagged} total tagged '{SOURCE}')")

        # The count queries above opened a fresh transaction, and autocommit
        # can't be toggled mid-transaction -- close it out first. REFRESH
        # CONCURRENTLY then has to run outside any transaction block.
        conn.commit()
        conn.autocommit = True
        conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY v2.artist_daily_plays")
        print("refreshed v2.artist_daily_plays")


if __name__ == "__main__":
    main()
