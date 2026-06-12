"""Daily 'random' playlist: what I listened to on a random past day."""
from db import connect, random_day_with_plays, tracks_on_local_date
from spotify_client import SpotifyAPI

PLAYLIST_ID = "1FnYUsH4Hlp2BAjxLYj2WJ"


def main():
    with connect() as conn:
        day = random_day_with_plays(conn)        # guaranteed non-empty, one query
        track_ids = tracks_on_local_date(conn, day)

    api = SpotifyAPI()
    api.delete_all_tracks(PLAYLIST_ID)
    api.add_tracks_to_playlist(PLAYLIST_ID, track_ids)
    api.update_playlist_details(
        PLAYLIST_ID, "random",
        f"what i listened to on {day.strftime('%b %-d, %Y')}",
    )
    print(f"random updated: {len(track_ids)} tracks from {day}")


if __name__ == "__main__":
    main()
