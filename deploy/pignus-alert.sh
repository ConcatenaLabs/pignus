#!/usr/bin/env bash
# Tell a person. Called by systemd through pignus-alert@.service when any
# Pignus unit fails, and by pignus-check.sh when its verdict changes; either
# way one line goes to a push topic a phone or a browser subscribes to, with
# no account and no credentials on this box beyond the topic's name.
#
# /root/sequentia/pignus-alert.env (mode 0600, box-only) holds:
#   NTFY_TOPIC=<a name nobody guesses>        # required; the channel
#   NTFY_URL=https://ntfy.sh                   # optional; a self-hosted ntfy
#   ALERT_PREFIX=sequentia-testnet             # optional; the message's title
# Subscribe at $NTFY_URL/$NTFY_TOPIC. Without a topic this prints to the
# journal and exits 0, so a box that has not set one up loses nothing but the
# push.
set -u
ENV="${PIGNUS_ALERT_ENV:-/root/sequentia/pignus-alert.env}"
[ -r "$ENV" ] && { set -a; . "$ENV"; set +a; }
title="${ALERT_PREFIX:-pignus} $(hostname -s)"
msg="${*:-something failed and nothing said what}"
echo "alert: $msg" >&2
if [ -z "${NTFY_TOPIC:-}" ]; then
    echo "alert: no NTFY_TOPIC in $ENV; the journal is the only record" >&2
    exit 0
fi
url="${NTFY_URL:-https://ntfy.sh}/$NTFY_TOPIC"
# A failure to deliver is said and not fatal: the alert path must never be
# the thing that takes a unit down a second time.
# --data-raw, not -d: a message beginning with "@" would otherwise name a
# file for curl to read and send instead.
curl -sS --max-time 15 -H "Title: $title" -H "Priority: high" \
     --data-raw "$msg" "$url" >/dev/null 2>&1 \
  || echo "alert: could not reach $url" >&2
exit 0
