#!/usr/bin/env bash
# Warn before the Spotify refresh token hits its 6-month expiry.
#
# Spotify expires refresh tokens 180 days after the *authorization*, and
# refreshing an access token does not extend that clock -- only a new
# authorization-code flow resets it. So the only way to avoid another silent
# outage is to count down from the date we last authorized.
#
# SPOTIFY_AUTHORIZED_AT (ISO-8601 date, e.g. 2026-08-04) is written into app.env
# by deploy/get_refresh_token.py --deploy.
set -uo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LIFETIME_DAYS="${SPOTIFY_TOKEN_LIFETIME_DAYS:-180}"

if [[ -z "${SPOTIFY_AUTHORIZED_AT:-}" ]]; then
    echo "SPOTIFY_AUTHORIZED_AT not set in app.env; cannot compute expiry" >&2
    exit 0
fi

authorized_epoch=$(date -d "$SPOTIFY_AUTHORIZED_AT" +%s 2>/dev/null) || {
    echo "SPOTIFY_AUTHORIZED_AT='${SPOTIFY_AUTHORIZED_AT}' is not a date I can parse" >&2
    exit 1
}
expires_epoch=$(( authorized_epoch + LIFETIME_DAYS * 86400 ))
days_left=$(( (expires_epoch - $(date +%s)) / 86400 ))
expires_on=$(date -d "@${expires_epoch}" +%Y-%m-%d)

echo "authorized ${SPOTIFY_AUTHORIZED_AT}; expires ${expires_on} (${days_left} days left)"

# Quiet until it's worth interrupting for, then escalate as the date approaches.
# days_left == 0 still has up to 24h on the clock, so only < 0 is actually expired.
if (( days_left < 0 )); then
    "$here/notify.sh" "Spotify token EXPIRED" \
        "The refresh token expired on ${expires_on}. Ingest and playlists are down until you re-authorize:

  cd ~/Documents/playlists && python deploy/get_refresh_token.py --deploy"
elif (( days_left == 30 || days_left == 14 || days_left == 7 || days_left <= 3 )); then
    "$here/notify.sh" "Spotify token expires in ${days_left}d" \
        "Expires ${expires_on}. Re-authorize with:

  cd ~/Documents/playlists && python deploy/get_refresh_token.py --deploy"
fi
