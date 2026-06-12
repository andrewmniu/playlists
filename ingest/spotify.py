"""Minimal Spotify client for ingestion.

Only what the hourly job needs: refresh an access token, and read
recently-played. recently-played already returns full track objects,
so there's no need for a separate /tracks lookup.
"""
import base64
import os

import requests

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
TIMEOUT = 30


class Spotify:
    def __init__(self):
        client_id = os.environ["SPOTIFY_CLIENT_ID"]
        client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
        refresh_token = os.environ["SPOTIFY_REFRESH_TOKEN"]

        auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        resp = requests.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {auth}"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=TIMEOUT,
        )
        if not resp.ok:
            raise RuntimeError(f"Token refresh failed ({resp.status_code}): {resp.text}")
        self.token = resp.json()["access_token"]

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def recently_played(self, after_ms=None, limit=50):
        """Return recently-played items (newest first).

        Each item is a dict with 'played_at' (ISO-8601 UTC) and a full
        'track' object. `after_ms` is a Unix-millisecond cursor: only
        plays strictly after that instant are returned (max 50).
        """
        params = {"limit": limit}
        if after_ms is not None:
            params["after"] = after_ms
        resp = requests.get(
            f"{API}/me/player/recently-played",
            headers=self._headers(),
            params=params,
            timeout=TIMEOUT,
        )
        if not resp.ok:
            raise RuntimeError(
                f"recently-played failed ({resp.status_code}): {resp.text}"
            )
        return resp.json()["items"]
