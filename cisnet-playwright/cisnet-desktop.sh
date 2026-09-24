#!/usr/bin/env bash

set -Eeuo pipefail

export HOME="${HOME:-/var/lib/cisnet-playwright}"
export DISPLAY="${DISPLAY:-:99}"
VNC_PASSWORD_FILE="${VNC_PASSWORD_FILE:-/var/lib/cisnet-playwright/.vnc/passwd}"
NOVNC_BIND_ADDRESS="${NOVNC_BIND_ADDRESS:-127.0.0.1}"
DISPLAY_NUMBER="${DISPLAY#:}"

if [[ ! "$DISPLAY_NUMBER" =~ ^[0-9]+$ ]]; then
  echo "Некорректный DISPLAY: $DISPLAY" >&2
  exit 2
fi

X_SOCKET="/tmp/.X11-unix/X${DISPLAY_NUMBER}"

PIDS=()

cleanup() {
  trap - EXIT INT TERM

  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done

  wait 2>/dev/null || true
}

terminate() {
  cleanup
  exit 0
}

trap cleanup EXIT
trap terminate INT TERM

/usr/bin/Xvfb "$DISPLAY" \
  -screen 0 1300x1080x24 \
  -nolisten tcp &

XVFB_PID=$!
PIDS+=("$XVFB_PID")

for attempt in {1..50}; do
  if [[ -S "$X_SOCKET" ]]; then
    break
  fi

  if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "Xvfb завершился при запуске"
    exit 1
  fi

  sleep 0.1
done

if [[ ! -S "$X_SOCKET" ]]; then
  echo "Не удалось запустить экран $DISPLAY"
  exit 1
fi

/usr/bin/fluxbox &
PIDS+=("$!")

/usr/bin/x11vnc \
  -display "$DISPLAY" \
  -localhost \
  -rfbport 5900 \
  -rfbauth "$VNC_PASSWORD_FILE" \
  -forever \
  -shared \
  -wait 16 \
  -defer 16 \
  -noxdamage &

PIDS+=("$!")

/usr/bin/websockify \
  --web=/usr/share/novnc \
  "$NOVNC_BIND_ADDRESS:6080" \
  127.0.0.1:5900 &

PIDS+=("$!")

echo "CIS-Net desktop запущен"
echo "noVNC: http://127.0.0.1:6080/vnc.html"

wait -n "${PIDS[@]}"

echo "Один из процессов неожиданно завершился"
exit 1
