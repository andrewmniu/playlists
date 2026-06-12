"""Spotify client for playlist management (create / clear / fill / rename).

Ported from the original playlists repo; reads SPOTIFY_* env vars and adds
request timeouts. Used by the daily/monthly playlist jobs.
"""
import base64
import os

import requests

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
TIMEOUT = 30


def _is_subsequence(sub, seq):
    """True if `sub` appears in `seq` in order (not necessarily contiguous)."""
    it = iter(seq)
    return all(item in it for item in sub)


class SpotifyAPI:
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
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}

    def create_playlist(self, title, description):
        resp = requests.post(
            f"{API}/users/andrewniu/playlists",
            headers=self._headers(),
            json={"name": title, "description": description},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def _get_track_count(self, playlist_id):
        resp = requests.get(
            f"{API}/playlists/{playlist_id}/tracks?fields=total",
            headers=self._headers(),
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["total"]

    def _get_tracks_page(self, playlist_id, offset):
        resp = requests.get(
            f"{API}/playlists/{playlist_id}/tracks"
            f"?fields=items%28track%28id%29%29&limit=100&offset={offset}",
            headers=self._headers(),
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        # Unavailable/removed/local tracks come back as {"track": null}; skip them.
        return [
            item["track"]["id"]
            for item in resp.json()["items"]
            if item.get("track") and item["track"].get("id")
        ]

    def get_tracks_from_playlist(self, playlist_id):
        total = self._get_track_count(playlist_id)
        ids = []
        for offset in range(0, total, 100):
            ids += self._get_tracks_page(playlist_id, offset)
        return ids

    def _delete_tracks(self, playlist_id, track_ids):
        resp = requests.delete(
            f"{API}/playlists/{playlist_id}/tracks",
            headers=self._headers(),
            json={"tracks": [{"uri": f"spotify:track:{tid}"} for tid in track_ids]},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()

    def delete_all_tracks(self, playlist_id):
        track_ids = self.get_tracks_from_playlist(playlist_id)
        for i in range(0, len(track_ids), 100):
            self._delete_tracks(playlist_id, track_ids[i:i + 100])
        return track_ids

    def _add_tracks(self, playlist_id, uris, position=None):
        body = {"uris": uris}
        if position is not None:
            body["position"] = position
        resp = requests.post(
            f"{API}/playlists/{playlist_id}/tracks",
            headers=self._headers(),
            json=body,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()

    def add_tracks_to_playlist(self, playlist_id, track_ids):
        for i in range(0, len(track_ids), 100):
            uris = [f"spotify:track:{tid}" for tid in track_ids[i:i + 100]]
            self._add_tracks(playlist_id, uris)

    def sync_playlist(self, playlist_id, desired_ids, current_ids=None):
        """Make the playlist match `desired_ids` (in order) with minimal edits.

        Removes only tracks that dropped out and inserts only the missing ones
        at their correct positions (Spotify's `position` param), preserving the
        "date added" on everything already in place. A new track whose slot is
        in the middle of the list is inserted there — no delete/re-add.

        `current_ids` lets the caller supply the known current state (e.g. a
        cached copy of what was last synced) to skip the expensive read. When
        None, the current order is read back from Spotify.

        Falls back to a full rebuild only if the existing order has diverged
        from desired (e.g. a manual reshuffle), so the result is always exactly
        `desired_ids`.
        """
        current = self.get_tracks_from_playlist(playlist_id) if current_ids is None else current_ids
        desired_set = set(desired_ids)
        kept = [t for t in current if t in desired_set]   # current order, still-wanted

        # Existing tracks' order is immutable (5th-play time never changes), so
        # `kept` should be an in-order subsequence of desired. If it isn't, the
        # playlist was reordered out-of-band — rebuild to guarantee correctness.
        if not _is_subsequence(kept, desired_ids):
            self.delete_all_tracks(playlist_id)
            self.add_tracks_to_playlist(playlist_id, desired_ids)
            return {"rebuilt": True, "added": len(desired_ids), "removed": len(current)}

        to_remove = [t for t in current if t not in desired_set]
        for i in range(0, len(to_remove), 100):
            self._delete_tracks(playlist_id, to_remove[i:i + 100])

        # Playlist now equals `kept`; insert each missing track at its desired index.
        ci = added = 0
        for di, tid in enumerate(desired_ids):
            if ci < len(kept) and kept[ci] == tid:
                ci += 1
            else:
                self._add_tracks(playlist_id, [f"spotify:track:{tid}"], position=di)
                added += 1
        return {"rebuilt": False, "added": added, "removed": len(to_remove)}

    def update_playlist_details(self, playlist_id, name, description):
        resp = requests.put(
            f"{API}/playlists/{playlist_id}",
            headers=self._headers(),
            json={"name": name, "description": description},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
