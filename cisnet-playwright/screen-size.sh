#!/usr/bin/env bash

cisnet_screen_geometry() {
  local width="${CISNET_SCREEN_WIDTH:-1300}"
  local height="${CISNET_SCREEN_HEIGHT:-1080}"
  if [[ ! "$width" =~ ^[1-9][0-9]{2,3}$ ]] || (( 10#$width < 800 || 10#$width > 3840 )); then
    echo 'CISNET_SCREEN_WIDTH must be an integer from 800 to 3840' >&2
    return 2
  fi
  if [[ ! "$height" =~ ^[1-9][0-9]{2,3}$ ]] || (( 10#$height < 600 || 10#$height > 2160 )); then
    echo 'CISNET_SCREEN_HEIGHT must be an integer from 600 to 2160' >&2
    return 2
  fi
  printf '%sx%sx24' "$width" "$height"
}
