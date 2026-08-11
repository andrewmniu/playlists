"""Backfill album-edition facts, and re-home tracks stranded on single releases.

Two passes over the albums that actually appear in the listening history:

  A. Fetch each album's type and full tracklist, and work out how long its
     standard edition is (album_editions.split_edition). Writes
     v2.albums.album_type / core_tracks and flags the padding tracks with
     v2.tracks.is_variant.

  B. Find tracks filed under a 1-2 track single release, look up which of the
     user's albums carry the same isrc, and move the track onto the release the
     edition rules pick. Every move is logged to v2.track_album_repairs.

Pass B exists because dedup by isrc keeps whichever row was ingested first
(0004's MIN(id)), and for a song released ahead of its album that is the single
-- the track then never joins the album it belongs to. ingest_history.py now
avoids creating new instances of this; this script cleans up the existing ones.

Idempotent: recomputes from Spotify each run and only writes what changed.
Derived data only -- both columns and the flag can be dropped and rebuilt.

A full sweep is ~1,300 requests over roughly ten minutes. album-listens.service
runs it with --new-only before each rebuild, which covers just the albums heard
since the last run; the full form is for when the heuristics themselves change.
--cache memoizes Spotify's answers so those reruns cost nothing.

    DATABASE_URL=... SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... \
        python ingest/enrich_album_editions.py [--dry-run] [--new-only]
                                               [--limit N] [--cache FILE]
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from album_editions import (ALBUM_MIN_TRACKS, base_album_name, edition_rank,
                            is_secondary_edition, is_variant_title, split_edition,
                            standard_edition_tracks)

# Same fallback the other backfill uses: creds and DSN live in the dashboard's
# .env locally, and are supplied by app.env on the VPS.
FALLBACK_ENV = "/Users/andrewniu/Documents/music-dashboard/backend/.env"
if not os.environ.get("DATABASE_URL") and os.path.exists(FALLBACK_ENV):
    from dotenv import load_dotenv
    load_dotenv(FALLBACK_ENV)

DATABASE_URL = os.environ["DATABASE_URL"]
CID, CS = os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]
USER_ID = 1
API = "https://api.spotify.com/v1/"

# A release has to have been played this much to be allowed to claim a track.
CANDIDATE_MIN_PLAYS = 5


# ── Spotify ───────────────────────────────────────────────────────────────────

class Api:
    """Client-credentials Spotify reader: retries 429s, refreshes on 401.

    Optionally memoizes tracklists and isrcs to a JSON file. A full sweep is
    ~1,300 requests and the answers don't change, so with a cache the heuristics
    can be re-run over the same data in seconds.
    """

    def __init__(self, cache_path=None):
        self.token = self._token()
        self.cache_path = cache_path
        self.cache = {"albums": {}, "isrcs": {}, "artist_albums": {}}
        if cache_path and os.path.exists(cache_path):
            with open(cache_path) as f:
                self.cache.update(json.load(f))
            print(f"cache: {len(self.cache['albums']):,} albums, "
                  f"{len(self.cache['isrcs']):,} isrcs")

    def save_cache(self):
        if self.cache_path:
            with open(self.cache_path, "w") as f:
                json.dump(self.cache, f)

    @staticmethod
    def _token():
        auth = base64.b64encode(f"{CID}:{CS}".encode()).decode()
        data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request("https://accounts.spotify.com/api/token",
                                     data=data, headers={"Authorization": f"Basic {auth}"})
        return json.load(urllib.request.urlopen(req))["access_token"]

    def get(self, url):
        if not url.startswith("http"):
            url = API + url
        for attempt in range(6):
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
            try:
                return json.load(urllib.request.urlopen(req))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After", "2")) + 1
                    print(f"    rate-limited, sleeping {wait}s", flush=True)
                    time.sleep(wait)
                elif e.code == 401:
                    self.token = self._token()
                elif e.code >= 500:
                    time.sleep(2 * (attempt + 1))
                else:
                    raise
        raise RuntimeError(f"gave up on {url}")

    def album_tracklists(self, spotify_ids):
        """{album_spotify_id: (album_type, total_tracks, [(track_sid, title)])}.

        /albums returns the first 50 tracks inline; longer tracklists are paged
        so the split stays exact (a truncated list would undercount N).
        """
        out = {sid: tuple(self.cache["albums"][sid])
               for sid in spotify_ids if sid in self.cache["albums"]}
        todo = [sid for sid in spotify_ids if sid not in out]
        for i in range(0, len(todo), 20):
            batch = todo[i:i + 20]
            for al in self.get("albums?ids=" + ",".join(batch))["albums"]:
                if al is None:
                    continue
                items, page = list(al["tracks"]["items"]), al["tracks"]
                while page.get("next"):
                    page = self.get(page["next"])
                    items += page["items"]
                out[al["id"]] = (al["album_type"], al["total_tracks"],
                                 [(t["id"], t["name"]) for t in items])
                self.cache["albums"][al["id"]] = out[al["id"]]
            _progress("  albums", i + len(batch), 20, len(todo))
        self.save_cache()
        return out

    def artist_albums(self, artist_sid):
        """Every album release Spotify lists for an artist."""
        if artist_sid in self.cache["artist_albums"]:
            return self.cache["artist_albums"][artist_sid]
        out, url = [], f"artists/{artist_sid}/albums?include_groups=album&limit=50&market=US"
        while url:
            page = self.get(url)
            out += [{"id": a["id"], "name": a["name"], "album_type": a["album_type"],
                     "total_tracks": a["total_tracks"], "release_date": a.get("release_date"),
                     "release_date_precision": a.get("release_date_precision"),
                     "images": a.get("images") or []}
                    for a in page["items"]]
            url = page.get("next")
        self.cache["artist_albums"][artist_sid] = out
        self.save_cache()
        return out

    def track_isrcs(self, spotify_ids):
        """{track_spotify_id: isrc}. Album tracklists are simplified objects
        with no external_ids, so identity needs this second lookup."""
        out = {sid: self.cache["isrcs"][sid]
               for sid in spotify_ids if sid in self.cache["isrcs"]}
        todo = [sid for sid in spotify_ids if sid not in out]
        for i in range(0, len(todo), 50):
            batch = todo[i:i + 50]
            for t in self.get("tracks?ids=" + ",".join(batch))["tracks"]:
                if t is None:
                    continue
                isrc = (t.get("external_ids") or {}).get("isrc")
                if isrc:
                    out[t["id"]] = isrc
                    self.cache["isrcs"][t["id"]] = isrc
            _progress("  tracks", i + len(batch), 50, len(todo))
        self.save_cache()
        return out


def _progress(label, done, step, total):
    if done >= total or done // 500 != (done - step) // 500:
        print(f"{label}: {min(done, total):,}/{total:,}", flush=True)


# ── Pass A: editions ──────────────────────────────────────────────────────────

def pass_a(conn, api, albums, dry_run):
    """Populate album_type / core_tracks, and flag padding tracks."""
    targets = [a for a in albums if a["spotify_id"] and (a["total_tracks"] or 0) >= ALBUM_MIN_TRACKS]
    print(f"\nPass A — {len(targets):,} played albums to inspect")
    fetched = api.album_tracklists([a["spotify_id"] for a in targets])

    # Every release we know of per artist, played or not -- an unplayed row for
    # the standard edition is exactly the evidence the sibling rule wants.
    siblings = defaultdict(list)
    for artist_id, name, total in conn.execute(
            "SELECT artist_id, name, total_tracks FROM v2.albums").fetchall():
        siblings[artist_id].append((name, total))

    meta_rows, padding_by_album, tracklists = [], {}, {}
    for a in targets:
        got = fetched.get(a["spotify_id"])
        if not got:
            continue
        album_type, total_tracks, tracks = got
        tracklists[a["id"]] = tracks
        core, padding = split_edition(total_tracks, tracks)
        # A shorter edition of the same record outranks the tracklist heuristic:
        # box sets pad with mono remixes and unmarked outtakes, which no title
        # pattern catches, and the standard edition's own length is not a guess.
        sibling_core = standard_edition_tracks(a["name"], total_tracks, siblings[a["artist_id"]])
        if sibling_core and (core is None or sibling_core < core):
            core = sibling_core
            # Position can't say which tracks are the padding here. The VU &
            # Nico box opens with the mono disc and carries the stereo album --
            # the one actually played -- further down, so slicing at `core`
            # flags the album itself and the record scores nothing. Titles can:
            # labels mark the extras "- Alternate Mix", "- Live", "- Demo".
            padding = [sid for sid, title in tracks if is_variant_title(title)]
        meta_rows.append((a["id"], album_type, core, total_tracks))
        # Pass B ranks on these, and a dry run never reloads them from the DB.
        a["album_type"], a["core_tracks"], a["total_tracks"] = album_type, core, total_tracks
        if padding:
            padding_by_album[a["id"]] = set(padding)

    shrunk = [(a, c, t) for a, _, c, t in meta_rows if c]
    print(f"  {len(meta_rows):,} albums resolved; {len(shrunk):,} carry padding")
    by_id = {a["id"]: a for a in albums}
    for aid, core, total in sorted(shrunk, key=lambda r: r[2] - r[1], reverse=True)[:15]:
        a = by_id[aid]
        print(f"    {total:>3} -> {core:<3} {a['artist']} — {a['name'][:50]}")

    if dry_run:
        return tracklists

    with conn.cursor() as cur:
        cur.executemany(
            """UPDATE v2.albums SET album_type = %s, core_tracks = %s,
                      total_tracks = coalesce(%s, total_tracks), updated_at = now()
               WHERE id = %s""",
            [(t, c, tt, aid) for aid, t, c, tt in meta_rows])

        # Padding is matched on spotify_id, falling back to the title for rows
        # that isrc-dedup filed under a different release's id. Both directions
        # are written: a track that is no longer padding has to lose the flag,
        # or a heuristic change would only ever add exclusions.
        flagged = unflagged = 0
        for album_id, sids in padding_by_album.items():
            names = [t for sid, t in tracklists[album_id] if sid in sids]
            # Restricted to rows the tracklist accounts for. Pass B parks the
            # odd stray here -- a commentary track re-homed from another
            # edition, say -- and it is padding on its own title's evidence;
            # rewriting it from a tracklist it was never on would clear that.
            known_sids = [sid for sid, _ in tracklists[album_id]]
            known_names = [t for _, t in tracklists[album_id]]
            cur.execute(
                """UPDATE v2.tracks SET is_variant = (spotify_id = ANY(%s) OR name = ANY(%s))
                   WHERE album_id = %s
                     AND (spotify_id = ANY(%s) OR name = ANY(%s))
                     AND is_variant <> (spotify_id = ANY(%s) OR name = ANY(%s))
                   RETURNING is_variant""",
                (list(sids), names, album_id, known_sids, known_names,
                 list(sids), names))
            changed = [r[0] for r in cur.fetchall()]
            flagged += sum(changed)
            unflagged += len(changed) - sum(changed)
        # And on an album that turned out not to be padded at all, nothing is.
        cur.execute(
            """UPDATE v2.tracks t SET is_variant = false
               WHERE t.is_variant AND NOT EXISTS (
                   SELECT 1 FROM v2.albums a
                   WHERE a.id = t.album_id AND a.core_tracks IS NOT NULL)""")
        print(f"  padding flags: +{flagged:,} / -{unflagged + cur.rowcount:,}")
    conn.commit()
    return tracklists


# ── Pass A2: pull in album-proper editions that were never played ─────────────

def _norm_date(d, prec):
    if not d or not d[:4].isdigit() or int(d[:4]) < 1:
        return None
    return f"{d[:4]}-01-01" if prec == "year" else (f"{d[:7]}-01" if prec == "month" else d)


def adopt_primary_editions(conn, api, albums, tracklists, dry_run):
    """Add the album proper for any secondary edition that is standing in for one.

    Pass B can only re-home a track onto a release the user has played, which
    leaves a hole: if the *only* edition ever played is the commentary version,
    there is nothing better to move to and it keeps the album's identity. Laura
    Stevenson's "Sit Resist (Commentary Version)" held all 1,227 plays of Sit
    Resist, which does exist on Spotify -- as a release we'd simply never seen.

    So for those, ask Spotify for the plain edition and insert it. It arrives
    with no plays, which is fine: edition_rank puts the album proper above a
    secondary edition before play counts are consulted at all.
    """
    targets = [a for a in albums
               if a["plays"] > 0 and a["artist_spotify_id"]
               and is_secondary_edition(a["name"])]
    print(f"\nPass A2 — {len(targets):,} secondary editions holding plays")

    known = {a["spotify_id"] for a in albums}
    adopted = []
    for a in targets:
        base = base_album_name(a["name"])
        want = a["core_tracks"] or 0
        options = [c for c in api.artist_albums(a["artist_spotify_id"])
                   if (c["total_tracks"] or 0) >= ALBUM_MIN_TRACKS
                   and not is_secondary_edition(c["name"])
                   and base_album_name(c["name"]) == base]
        # The release matching the length we worked out for the record, else the
        # leanest: every other edition of a record is longer than the record.
        # Taking the fullest instead drags in expanded reissues -- a 19-track
        # "Begin to Hope" for a 13-track album.
        options.sort(key=lambda c: (c["total_tracks"] != want, c["total_tracks"]))
        # Only ever the best option, and only if we don't already have it. Taking
        # the next-best when the best is already ours doesn't converge: each run
        # would adopt one more edition of the same record for ever.
        if options and options[0]["id"] not in known:
            adopted.append((a, options[0]))
            known.add(options[0]["id"])

    print(f"  {len(adopted):,} album-proper editions to bring in")
    for a, pick in adopted[:15]:
        print(f"    {a['artist']} — {pick['name']} ({pick['total_tracks']}tr) "
              f"for {a['name'][:40]}")
    if not adopted:
        return []

    # Insert, or adopt the row we already have -- an album proper with no plays
    # is invisible to load_albums, so a release can be sitting in v2.albums and
    # still be unavailable to Pass B. That is exactly the Sit Resist case.
    existing = dict(conn.execute(
        "SELECT spotify_id, id FROM v2.albums WHERE spotify_id IS NOT NULL").fetchall())
    new_albums = []
    with conn.cursor() as cur:
        for a, pick in adopted:
            imgs = pick["images"]
            img = (imgs[1]["url"] if len(imgs) > 1 else imgs[0]["url"]) if imgs else None
            if dry_run:
                # No write, but still resolve an id so Pass B can report the
                # moves this would cause. A release we don't have yet gets a
                # placeholder, which never reaches the database.
                row = (existing.get(pick["id"], -len(new_albums) - 1),)
            else:
                row = cur.execute(
                    """INSERT INTO v2.albums (spotify_id, name, artist_id, image_url,
                                              release_date, total_tracks, album_type)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (spotify_id) DO UPDATE
                         SET total_tracks = EXCLUDED.total_tracks,
                             album_type = coalesce(EXCLUDED.album_type, v2.albums.album_type),
                             image_url = coalesce(v2.albums.image_url, EXCLUDED.image_url)
                       RETURNING id""",
                    (pick["id"], pick["name"], a["artist_id"], img,
                     _norm_date(pick["release_date"], pick["release_date_precision"]),
                     pick["total_tracks"], pick["album_type"])).fetchone()
            new_albums.append({
                "id": row[0], "spotify_id": pick["id"], "name": pick["name"],
                "total_tracks": pick["total_tracks"], "core_tracks": None,
                "album_type": pick["album_type"], "artist_id": a["artist_id"],
                "artist_spotify_id": a["artist_spotify_id"], "artist": a["artist"],
                "plays": 0, "adopted": True,
            })
    if dry_run:
        conn.rollback()
    else:
        conn.commit()

    for al in new_albums:
        got = api.album_tracklists([al["spotify_id"]]).get(al["spotify_id"])
        if got:
            tracklists[al["id"]] = got[2]
    return new_albums


# ── Pass B: re-home orphans ───────────────────────────────────────────────────

def pass_b(conn, api, albums, tracklists, dry_run):
    """File every played track under the release the edition rules pick.

    Scoped to all played tracks, not just ones on 1-2 track singles: a song can
    just as easily be stranded on a 4-track maxi-single (Beach Bunny's "Entropy"
    holding a track off Emotional Creature), and the same comparison moves a
    track off a deluxe onto the standard edition.
    """
    by_id = {a["id"]: a for a in albums}
    tracks = conn.execute("""
        SELECT t.id, t.isrc, t.name, t.album_id
        FROM v2.tracks t
        WHERE t.album_id IS NOT NULL
          AND t.isrc IS NOT NULL AND t.isrc <> ''
          AND EXISTS (SELECT 1 FROM v2.history h WHERE h.track_id = t.id)
    """).fetchall()
    print(f"\nPass B — {len(tracks):,} played tracks to place")

    # Only releases the user actually plays can claim a track. An album nobody
    # listens to is not "the version I listened to most", and filing a track
    # under one would put it somewhere it never otherwise appears -- the play
    # floor keeps barely-touched reissues out and the sweep down to one request
    # per 50 tracks of plausible homes.
    candidates = [a for a in albums
                  if (a["plays"] >= CANDIDATE_MIN_PLAYS or a.get("adopted"))
                  and (a["core_tracks"] or a["total_tracks"] or 0) >= ALBUM_MIN_TRACKS
                  and a["id"] in tracklists]
    print(f"  {len(candidates):,} candidate albums (>= {CANDIDATE_MIN_PLAYS} plays "
          f"or newly adopted)")

    sids = sorted({sid for a in candidates for sid, _ in tracklists[a["id"]]})
    print(f"  resolving isrcs for {len(sids):,} of their tracks")
    isrcs = api.track_isrcs(sids)

    isrc_albums = defaultdict(list)
    for a in candidates:
        for sid, _ in tracklists[a["id"]]:
            if sid in isrcs:
                isrc_albums[isrcs[sid]].append(a)

    def rank(a):
        return edition_rank(a["name"], a["album_type"], a["core_tracks"],
                            a["total_tracks"], a["plays"])

    moves, unmatched = [], 0
    for track_id, isrc, name, cur_album_id in tracks:
        options = isrc_albums.get(isrc, [])
        current = by_id.get(cur_album_id)   # absent only under --limit
        if not options or current is None:
            unmatched += 1
            continue
        best = max(options, key=rank)
        if best["id"] == cur_album_id or rank(best) <= rank(current):
            continue
        moves.append((track_id, cur_album_id, best["id"], name))

    print(f"  {len(moves):,} tracks to re-home "
          f"({unmatched:,} appear on no played album, left alone)")
    # Ranked by what the track is being taken *off*, not what it lands on: an
    # album proper adopted this run has no plays yet, so sorting by destination
    # buries the very corrections worth eyeballing.
    for track_id, old, new, name in sorted(
            moves, key=lambda m: -by_id[m[1]]["plays"])[:15]:
        print(f"    {name[:34]:34} {by_id[old]['name'][:24]:24} -> {by_id[new]['name'][:30]}")

    if dry_run or not moves:
        return moves

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO v2.track_album_repairs
                   (track_id, old_album_id, new_album_id, reason)
               VALUES (%s, %s, %s, 'backfill: better edition')""",
            [(t, o, n) for t, o, n, _ in moves])
        cur.executemany(
            "UPDATE v2.tracks SET album_id = %s, updated_at = now() WHERE id = %s",
            [(n, t) for t, _, n, _ in moves])
        # A re-homed track lands on a real album; whether it is padding there is
        # a fresh question, and its title is the only evidence we have.
        cur.executemany(
            "UPDATE v2.tracks SET is_variant = %s WHERE id = %s",
            [(is_variant_title(name), t) for t, _, _, name in moves])
    conn.commit()
    return moves


# ── main ──────────────────────────────────────────────────────────────────────

def load_albums(conn, limit, new_only=False):
    rows = conn.execute(f"""
        SELECT al.id, al.spotify_id, al.name, al.total_tracks, al.core_tracks,
               al.album_type, al.artist_id, ar.name AS artist,
               ar.spotify_id AS artist_spotify_id, count(h.*) AS plays
        FROM v2.albums al
        JOIN v2.artists ar ON ar.id = al.artist_id
        JOIN v2.tracks t   ON t.album_id = al.id
        JOIN v2.history h  ON h.track_id = t.id AND h.user_id = %s
        {"WHERE al.album_type IS NULL" if new_only else ""}
        GROUP BY al.id, ar.name, ar.spotify_id
        ORDER BY plays DESC
    """, [USER_ID]).fetchall()
    cols = ["id", "spotify_id", "name", "total_tracks", "core_tracks",
            "album_type", "artist_id", "artist", "artist_spotify_id", "plays"]
    albums = [dict(zip(cols, r)) for r in rows]
    return albums[:limit] if limit else albums


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--limit", type=int, help="only the N most-played albums")
    ap.add_argument("--new-only", action="store_true",
                    help="only albums not yet inspected (album_type is null)")
    ap.add_argument("--cache", help="JSON file memoizing Spotify responses")
    args = ap.parse_args()

    api = Api(args.cache)
    with psycopg.connect(DATABASE_URL) as conn:
        albums = load_albums(conn, args.limit, args.new_only)
        print(f"{len(albums):,} albums with plays")
        tracklists = pass_a(conn, api, albums, args.dry_run)
        albums += adopt_primary_editions(conn, api, albums, tracklists, args.dry_run)
        pass_b(conn, api, albums, tracklists, args.dry_run)
    print("\ndone" + (" (dry run — nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
