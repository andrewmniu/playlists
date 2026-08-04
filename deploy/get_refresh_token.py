"""Mint a new SPOTIFY_REFRESH_TOKEN via the authorization-code flow.

Spotify expires refresh tokens 180 days after authorization, and refreshing an
access token does not extend that clock -- so this has to be re-run roughly
every 6 months. There is no way to automate the consent click itself; the whole
point of the policy is that a human re-confirms. What is automated is
everything around it: --deploy writes the new token straight to the VPS and
records the authorization date so token-expiry.timer can warn you next time.

Run it on your laptop (it needs a browser):

    SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... python deploy/get_refresh_token.py --deploy

The redirect URI below must also be registered on the app at
https://developer.spotify.com/dashboard -> your app -> Settings -> Redirect URIs.
"""
import argparse
import base64
import http.server
import json
import os
import secrets
import shlex
import subprocess
import urllib.parse
import webbrowser
from datetime import date

import requests

REDIRECT_URI = "http://127.0.0.1:8888/callback"
PORT = 8888
VPS_HOST = "playlists-vps"
ENV_PATH = "/home/andrew/music-dashboard/app.env"

# Rewrites the given keys in app.env in place, preserving everything else and
# keeping the file 0600. Runs on the VPS; values arrive on stdin so the token
# never lands in a command line (visible in ps) or a shell history.
REMOTE_MERGE = r'''
import json, os, sys
payload = json.load(sys.stdin)
path = payload.pop("__path__")
lines = open(path).read().splitlines() if os.path.exists(path) else []
out, seen = [], set()
for ln in lines:
    key = ln.split("=", 1)[0].strip()
    if key in payload:
        out.append(key + "=" + payload[key])
        seen.add(key)
    else:
        out.append(ln)
for key, value in payload.items():
    if key not in seen:
        out.append(key + "=" + value)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    fh.write("\n".join(out) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print("updated " + path + " (" + ", ".join(sorted(payload)) + ")")
'''

# user-read-recently-played  -> ingest/ingest_history.py
# playlist-read-private      -> listing existing playlists to find them by name
# playlist-modify-public
# playlist-modify-private    -> creating/reordering the generated playlists
SCOPES = (
    "user-read-recently-played "
    "playlist-read-private "
    "playlist-modify-public "
    "playlist-modify-private"
)

state = secrets.token_urlsafe(16)
auth_code = None


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if query.get("state", [None])[0] != state:
            self.wfile.write(b"state mismatch - ignoring")
            return
        if "error" in query:
            self.wfile.write(f"denied: {query['error'][0]}".encode())
            return
        auth_code = query["code"][0]
        self.wfile.write(b"Got it. You can close this tab and return to the terminal.")

    def log_message(self, *args):
        pass


def deploy(token, host, env_path):
    """Write the new token + today's date into the VPS app.env, then restart ingest."""
    payload = json.dumps(
        {
            "__path__": env_path,
            "SPOTIFY_REFRESH_TOKEN": token,
            "SPOTIFY_AUTHORIZED_AT": date.today().isoformat(),
        }
    )
    # ssh concatenates its command args and hands the result to a *remote shell*,
    # which would re-split this multi-line script on whitespace. Quote it as one
    # shell word so python3 receives it intact.
    subprocess.run(
        ["ssh", host, "python3 -c " + shlex.quote(REMOTE_MERGE)],
        input=payload.encode(),
        check=True,
    )
    print(f"authorization date recorded; next expiry ~{date.today().isoformat()} + 180d")

    # Needs sudo on the VPS, so it may prompt (or fail on a passwordless-less
    # setup). Not fatal -- the token is already in place either way.
    print("\nstarting ingest.service to verify...")
    proc = subprocess.run(["ssh", "-t", host, "sudo", "systemctl", "start", "ingest.service"])
    if proc.returncode != 0:
        print("\ncouldn't start it from here. On the VPS, run:")
        print("  sudo systemctl start ingest.service")
        return
    subprocess.run(["ssh", host, "systemctl", "status", "ingest.service", "--no-pager", "-n", "15"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--deploy",
        action="store_true",
        help=f"write the token to {VPS_HOST}:{ENV_PATH} and restart ingest",
    )
    ap.add_argument("--host", default=VPS_HOST)
    ap.add_argument("--env-path", default=ENV_PATH)
    args = ap.parse_args()

    # read after parsing so --help works without the credentials in the environment
    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]

    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            # force the consent screen so a revoked grant is actually re-granted
            "show_dialog": "true",
        }
    )
    # Bind BEFORE opening the browser. The other order races: Spotify can redirect
    # back before the socket is listening, and the callback dies on a closed port.
    try:
        server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        raise SystemExit(
            f"can't listen on 127.0.0.1:{PORT} ({exc}).\n"
            f"A previous run is probably still waiting — kill it and try again:\n"
            f"  lsof -ti tcp:{PORT} | xargs kill"
        )

    print(f"Listening on {REDIRECT_URI}")
    print(f"Opening browser. If nothing happens, visit:\n\n{url}\n")
    webbrowser.open(url)

    while auth_code is None:
        server.handle_request()

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {auth}"},
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()["refresh_token"]

    if args.deploy:
        print("\ngot a new refresh token; deploying...")
        try:
            deploy(token, args.host, args.env_path)
        except Exception as exc:
            # Never let a deploy failure swallow the token -- it only exists in
            # this process, and getting another one means another consent round.
            print(f"\nDEPLOY FAILED: {exc}")
            print("\nThe token itself is fine. Save it by hand so it isn't lost —")
            print(f"in {args.env_path} on {args.host}, set:\n")
            print(f"  SPOTIFY_REFRESH_TOKEN={token}")
            print(f"  SPOTIFY_AUTHORIZED_AT={date.today().isoformat()}")
            raise SystemExit(1)
    else:
        print("\nNew refresh token:\n")
        print(token)
        print("\nPut it in ~/music-dashboard/app.env on the VPS as SPOTIFY_REFRESH_TOKEN=,")
        print("and set SPOTIFY_AUTHORIZED_AT=" + date.today().isoformat())
        print("(or just re-run with --deploy to do both)")


if __name__ == "__main__":
    main()
