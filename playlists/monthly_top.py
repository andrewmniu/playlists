"""Monthly playlist: my top tracks of the month just ended.

Runs on the 1st: builds a new playlist named for the previous month (e.g.
"May 2026") from my own listening history — the top tracks by play count.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db import LOCAL_TZ, connect, top_tracks_in_range
from spotify_client import SpotifyAPI

TOP_N = 30


def main():
    today = datetime.now(ZoneInfo(LOCAL_TZ)).date()
    last_month_end = today.replace(day=1) - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    label = last_month_end.strftime("%B %Y")

    with connect() as conn:
        track_ids = top_tracks_in_range(conn, last_month_start, last_month_end, TOP_N)

    if not track_ids:
        print(f"no plays in {label}; nothing created")
        return

    api = SpotifyAPI()
    playlist_id = api.create_playlist(label, f"My top tracks of {label}")
    api.add_tracks_to_playlist(playlist_id, track_ids)
    print(f"created '{label}' with {len(track_ids)} tracks ({playlist_id})")


if __name__ == "__main__":
    main()
