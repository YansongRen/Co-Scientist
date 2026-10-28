#!/bin/sh
set -eu

CONFIG_FILE="${CO_SCIENTIST_CONFIG:-/app/config/deployment.toml}"
PORT="${PORT:-8080}"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "Configuration file not found: $CONFIG_FILE" >&2
  exit 2
fi

# Migrations are idempotent. Running init before every container start keeps a
# persistent volume compatible with the current application schema.
co-scientist --config "$CONFIG_FILE" init
exec co-scientist --config "$CONFIG_FILE" serve --host 0.0.0.0 --port "$PORT"
