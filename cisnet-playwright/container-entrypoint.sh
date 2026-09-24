#!/usr/bin/env bash

set -Eeuo pipefail

if [[ -z "${VNC_PASSWORD:-}" ]]; then
  echo "VNC_PASSWORD is not configured" >&2
  exit 2
fi

export HOME="${HOME:-/var/lib/cisnet-playwright}"
export DISPLAY="${DISPLAY:-:99}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"
vnc_password_file="/tmp/cisnet-vnc/passwd"

umask 077
mkdir -p "${vnc_password_file%/*}" "$HOME/cdp-profile"
x11vnc -storepasswd "$VNC_PASSWORD" "$vnc_password_file" >/dev/null
unset VNC_PASSWORD
export VNC_PASSWORD_FILE="$vnc_password_file"

pids=()
stopping=0
display_number="${DISPLAY#:}"

if [[ ! "$display_number" =~ ^[0-9]+$ ]]; then
  echo "Invalid DISPLAY: $DISPLAY" >&2
  exit 2
fi
x_socket="/tmp/.X11-unix/X${display_number}"

stop_all() {
  if [[ "$stopping" -eq 1 ]]; then
    return
  fi
  stopping=1
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}

trap 'stop_all; exit 0' INT TERM
trap stop_all EXIT

/opt/cisnet-playwright/cisnet-desktop.sh &
pids+=("$!")

for _ in {1..100}; do
  [[ -S "$x_socket" ]] && break
  kill -0 "${pids[0]}" 2>/dev/null || exit 1
  sleep 0.1
done
[[ -S "$x_socket" ]] || { echo "Xvfb socket was not created" >&2; exit 1; }

node /opt/cisnet-playwright/launch-cdp.js &
pids+=("$!")

wait -n "${pids[@]}"
status=$?
stop_all
exit "$status"
