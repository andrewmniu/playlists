import requests
import base64
import os
from pathlib import Path
from dotenv import load_dotenv

env_path = Path('.') / '.env'
load_dotenv(dotenv_path=env_path)

class SpotifyAPI(object):
    # Gets access token from stored refresh token
    def __init__(self):
        CLIENT_ID = os.getenv('CLIENT_ID')
        CLIENT_SECRET = os.getenv('CLIENT_SECRET')
        REFRESH_TOKEN = os.getenv('REFRESH_TOKEN')
        encoded = base64.standard_b64encode(bytes(f"{CLIENT_ID}:{CLIENT_SECRET}", 'utf-8'))
        url = 'https://accounts.spotify.com/api/token'
        response = requests.post(
            url,
            headers={
                'Content-type':'application/x-www-form-urlencoded',
                "Authorization": f"Basic {encoded.decode()}"
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": REFRESH_TOKEN
            }
        )
        if response.ok:
            self.api_key = response.json()['access_token']
        else:
            message = "The refresh token is no longer valid."
            print(message)
            raise Exception

    # Creates a playlist with the month as the title
    def create_playlist(self, title, description):
        url = 'https://api.spotify.com/v1/users/andrewniu/playlists'
        response = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            },
            json={
                "name": title,
                "description": description
            }
        )
        if(response.ok):
            return response.json()['id']
        else:
            message = "There was an error creating a playlist."
            print(message)
            raise Exception

    def _get_track_count(self,playlist_id):
        url = f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?fields=total'

        response = requests.get(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
        )

        if response.ok:
            return response.json()['total']
        else:
            message = "There was an error getting recently played tracks."
            print(message)
            raise Exception

    def _get_tracks_from_playlist(self,playlist_id,offset):
        url = f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?fields=items%28track%28id%2Cname%29%29&limit=100&offset={offset}'
        response = requests.get(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
        )

        if response.ok:
            track_objects = response.json()['items']
            return [track["track"]['id'] for track in track_objects]
        else:
            message = "There was an error getting recently played tracks."
            print(message)
            raise Exception

    def get_tracks_from_playlist(self,playlist_id):
        total = self._get_track_count(playlist_id)
        track_uris = []
        offset = 0
        for i in range(0,total,100):
            track_uris += self._get_tracks_from_playlist(playlist_id, offset)
            offset += + min(100, total-i)
        return track_uris

    def _delete_tracks_from_playlist(self, playlist_id, track_uris):
        url = f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks'
        response = requests.delete(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            },
            json={
                "tracks":[{"uri": f"spotify:track:{track}"} for track in track_uris]
            }
        )
        if(response.ok):
            return "Tracks Removed"
        else:
            print(response.json())
            message = "There was an error deleting tracks from the playlist."
            print(message)
            raise Exception

    def delete_all_tracks(self, playlist_id):
        track_uris = self.get_tracks_from_playlist(playlist_id)
        total = len(track_uris)
        for i in range(0,total,100):
            j = i + min(100, total-i)
            self._delete_tracks_from_playlist(playlist_id, track_uris[i:j])
        return track_uris

    # Adds specified tracks to specified playlist
    def _add_tracks_to_playlist(self, playlist_id, track_uris):
        url = f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks'
        response = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            },
            json={
                "uris": track_uris,
            }
        )
        if(response.ok):
            return "Tracks Added!"
        else:
            print(response.json())
            message = "There was an error adding tracks to the playlist."
            print(message)
            raise Exception

    def add_tracks_to_playlist(self, playlist_id, track_uris):
        for i in range(0, len(track_uris), 100):
            j = i + min(100, len(track_uris)-i)
            tracks = [f"spotify:track:{track}" for track in track_uris[i:j]]
            self._add_tracks_to_playlist(playlist_id, tracks)

    def update_playlist_details(self, playlist_id, name, description):
        url = f'https://api.spotify.com/v1/playlists/{playlist_id}'
        response = requests.put(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            },
            json={
                "name":name,
                "description": description
            }
        )
        if(response.ok):
            return "Playlist Updated!"
        else:
            print(response.json())
            message = "There was an error updating playlist details."
            print(message)
            raise Exception
