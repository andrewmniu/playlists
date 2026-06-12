"""Daily 'yesteryear' playlist: what I listened to exactly one year ago today."""
from datetime import datetime
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from db import LOCAL_TZ, connect, tracks_on_local_date
from spotify_client import SpotifyAPI

PLAYLIST_ID = "7GsK6cpQmiiABLqe7O2xZY"


def main():
    today = datetime.now(ZoneInfo(LOCAL_TZ)).date()
    year_ago = today - relativedelta(years=1)

    with connect() as conn:
        track_ids = tracks_on_local_date(conn, year_ago)

    if not track_ids:
        print(f"no plays on {year_ago}; leaving playlist unchanged")
        return

    api = SpotifyAPI()
    api.delete_all_tracks(PLAYLIST_ID)
    api.add_tracks_to_playlist(PLAYLIST_ID, track_ids)
    api.update_playlist_details(
        PLAYLIST_ID, "yesteryear",
        f"what i listened to on {year_ago.strftime('%b %-d, %Y')}",
    )
    print(f"yesteryear updated: {len(track_ids)} tracks from {year_ago}")


if __name__ == "__main__":
    main()
