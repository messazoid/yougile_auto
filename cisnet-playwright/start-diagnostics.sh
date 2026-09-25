#!/usr/bin/env bash

set -Eeuo pipefail
source /opt/cisnet-playwright/screen-size.sh

geometry="$(cisnet_screen_geometry)"
display="${DISPLAY:-:99}"
display_number="${display#:}"
if [[ ! "$display_number" =~ ^[0-9]+$ ]]; then
  echo "Invalid DISPLAY: $display" >&2
  exit 2
fi
socket="/tmp/.X11-unix/X${display_number}"

/usr/bin/Xvfb "$display" -screen 0 "$geometry" -nolisten tcp &
xvfb_pid=$!
trap 'kill "$xvfb_pid" 2>/dev/null || true; wait "$xvfb_pid" 2>/dev/null || true' EXIT
for attempt in {1..50}; do
  [[ -S "$socket" ]] && break
  if ! kill -0 "$xvfb_pid" 2>/dev/null; then
    echo 'Xvfb exited during diagnostics startup' >&2
    exit 1
  fi
  sleep 0.1
done
[[ -S "$socket" ]] || { echo 'Xvfb did not create a display socket' >&2; exit 1; }
"$@"
