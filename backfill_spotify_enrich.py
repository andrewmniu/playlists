"""One-off enrichment backfill for the v2 schema.

Fills v2.tracks (isrc, duration_ms, preview_url) and v2.albums (spotify_id,
total_tracks, image_url, release_date) from Spotify's /tracks endpoint, which
returns album.total_tracks inline (the N for album-play counting).

Resumable: only processes tracks whose isrc is still NULL, so it can be re-run
after a rate-limit stop. Uses the music-dashboard backend .env for creds + DB.
Run with the music-dashboard venv:
  .../music-dashboard/backend/venv/bin/python backfill_spotify_enrich.py
"""
import base64, json, os, sys, time, urllib.parse, urllib.request
from dotenv import load_dotenv
import psycopg

ENV = "/Users/andrewniu/Documents/music-dashboard/backend/.env"
load_dotenv(ENV)
DB = os.environ["DATABASE_URL"]
CID, CS = os.environ["SPOTIFY_CLIENT_ID"], os.environ["SPOTIFY_CLIENT_SECRET"]


def get_token():
    auth = base64.b64encode(f"{CID}:{CS}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=data,
        headers={"Authorization": f"Basic {auth}"})
    return json.load(urllib.request.urlopen(req))["access_token"]


def fetch_tracks(ids, token):
    url = "https://api.spotify.com/v1/tracks?ids=" + ",".join(ids)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        return json.load(urllib.request.urlopen(req))["tracks"], token
    except urllib.error.HTTPError as e:
        if e.code == 429:
            wait = int(e.headers.get("Retry-After", "2")) + 1
            print(f"  rate-limited, sleeping {wait}s", flush=True)
            time.sleep(wait)
            return fetch_tracks(ids, token)
        if e.code == 401:
            print("  token expired, refreshing", flush=True)
            return fetch_tracks(ids, get_token())
        raise


def norm_date(d, prec):
    if not d or d[:4] in ("0000", "") or not d[:4].isdigit() or int(d[:4]) < 1:
        return None
    if prec == "year":
        return f"{d[:4]}-01-01"
    if prec == "month":
        return f"{d[:7]}-01"
    return d


def _bulk_update(cur, sql, rows, ncols):
    """One UPDATE ... FROM (VALUES ...) for the whole batch (1 round-trip)."""
    if not rows:
        return
    ph = ",".join(["(" + ",".join(["%s"] * ncols) + ")"] * len(rows))
    params = [v for row in rows for v in row]
    cur.execute(sql.replace("__VALUES__", ph), params)


def main():
    token = get_token()
    with psycopg.connect(DB) as conn:
        rows = conn.execute(
            "select id, spotify_id, album_id from v2.tracks where isrc is null"
        ).fetchall()
        total = len(rows)
        print(f"{total} tracks to enrich", flush=True)
        done = 0
        for i in range(0, total, 50):
            batch = rows[i:i + 50]
            by_sid = {r[1]: r for r in batch}
            tracks, token = fetch_tracks([r[1] for r in batch], token)

            track_rows = []                 # (spotify_id, isrc, duration_ms, preview_url)
            album_rows = {}                 # album_id -> (album_id, spotify_id, total_tracks, img, date)
            for sid, t in zip(by_sid, tracks):
                _id, _sid, album_id = by_sid[sid]
                if t is None:               # unknown to Spotify; mark so we don't retry
                    track_rows.append((sid, "", None, None))
                    continue
                track_rows.append((
                    sid, t.get("external_ids", {}).get("isrc") or "",
                    t.get("duration_ms"), t.get("preview_url")))
                al = t["album"]
                imgs = al.get("images") or []
                img = (imgs[1]["url"] if len(imgs) > 1 else imgs[0]["url"]) if imgs else None
                album_rows[album_id] = (
                    album_id, al.get("id"), al.get("total_tracks"), img,
                    norm_date(al.get("release_date"), al.get("release_date_precision")))

            with conn.cursor() as cur:
                _bulk_update(cur, """
                    update v2.tracks t set isrc=v.isrc, duration_ms=v.dur::int,
                        preview_url=v.prev, updated_at=now()
                    from (values __VALUES__) as v(sid, isrc, dur, prev)
                    where t.spotify_id = v.sid""", track_rows, 4)
                _bulk_update(cur, """
                    update v2.albums a set spotify_id=v.sid, total_tracks=v.tt::int,
                        image_url=v.img, release_date=v.rd::date, updated_at=now()
                    from (values __VALUES__) as v(aid, sid, tt, img, rd)
                    where a.id = v.aid::bigint""", list(album_rows.values()), 5)
            conn.commit()
            done += len(batch)
            if (i // 50) % 20 == 0 or done == total:
                print(f"  {done}/{total}", flush=True)
            time.sleep(0.1)
    print("enrichment complete", flush=True)


if __name__ == "__main__":
    main()
