#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "rebalancer entrypoint must start as root to prepare /app/state" >&2
  exit 1
fi

mkdir -p /app/state
chown -R appuser:appuser /app/state
exec gosu appuser "$@"
