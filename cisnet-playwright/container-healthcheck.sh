#!/usr/bin/env bash

set -Eeuo pipefail

curl --fail --silent --show-error --max-time 3 \
  "http://127.0.0.1:${CISNET_CDP_PORT:-9223}/json/version" >/dev/null
curl --fail --silent --show-error --max-time 3 \
  "http://127.0.0.1:6080/vnc.html" >/dev/null
