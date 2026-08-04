#!/usr/bin/env bash
# Push a notification to ntfy.
#
#   notify.sh unit-failed <unit>      # OnFailure hook: includes recent journal lines
#   notify.sh <title> <message>       # anything else
#
# Reads NTFY_TOPIC (required) and NTFY_SERVER (default https://ntfy.sh) from
# app.env, which systemd supplies via EnvironmentFile.
set -uo pipefail

: "${NTFY_TOPIC:?NTFY_TOPIC not set in app.env}"
NTFY_SERVER="${NTFY_SERVER:-https://ntfy.sh}"

mode=${1:-}

if [[ "$mode" == "unit-failed" ]]; then
    unit=${2:?usage: notify.sh unit-failed <unit>}

    # ingest runs hourly, so a persistent breakage (an expired token, say) would
    # otherwise fire ~24 alerts a day. One per unit per THROTTLE_HOURS is enough
    # to notice without training yourself to ignore them.
    throttle_hours="${NTFY_THROTTLE_HOURS:-6}"
    state_dir="${NTFY_STATE_DIR:-${HOME}/.cache/playlists-notify}"
    mkdir -p "$state_dir" 2>/dev/null
    stamp="${state_dir}/${unit}.last"
    if [[ "$throttle_hours" != "0" && -f "$stamp" ]]; then
        age=$(( $(date +%s) - $(stat -c %Y "$stamp" 2>/dev/null || echo 0) ))
        if (( age < throttle_hours * 3600 )); then
            exit 0
        fi
    fi
    touch "$stamp" 2>/dev/null

    title="${unit} failed"
    priority="high"
    tags="rotating_light"
    # Scope the excerpt to the invocation that actually failed. Plain `-n 15`
    # spills lines from earlier runs, so an alert can arrive carrying a mix of
    # stale tracebacks and a *successful* run -- which reads as gibberish.
    invocation=$(systemctl show -p InvocationID --value "$unit" 2>/dev/null)
    log=""
    if [[ -n "$invocation" ]]; then
        log=$(journalctl "_SYSTEMD_INVOCATION_ID=${invocation}" -n 15 --no-pager -o cat 2>/dev/null)
    fi
    # Fall back to the time-boxed tail if the invocation id isn't available
    # (older systemd, or the unit was reset), and to a pointer if the journal
    # isn't readable at all.
    [[ -z "$log" ]] && log=$(journalctl -u "$unit" --since "-10 min" -n 15 --no-pager -o cat 2>/dev/null)
    if [[ -n "$log" ]]; then
        body="$log"
    else
        body="(no journal access; run: journalctl -u ${unit} -n 50)"
    fi
else
    title=${1:?usage: notify.sh <title> <message>}
    body=${2:-}
    priority="${NTFY_PRIORITY:-default}"
    tags="${NTFY_TAGS:-information_source}"
fi

curl -fsS --max-time 20 \
    -H "Title: ${title}" \
    -H "Priority: ${priority}" \
    -H "Tags: ${tags}" \
    -d "$body" \
    "${NTFY_SERVER}/${NTFY_TOPIC}" >/dev/null
