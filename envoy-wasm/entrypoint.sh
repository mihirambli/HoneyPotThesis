#!/bin/sh
set -e

TEMPLATE="/etc/envoy/envoy-wasm.yaml"
RESOLVED="/tmp/envoy-wasm.yaml"
CONFIG_FILE="/etc/envoy/config.json"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "ERROR: $CONFIG_FILE not found" >&2
  exit 1
fi

CONFIG_JSON=$(tr -d '\n' < "$CONFIG_FILE" | sed 's/[&\\/]/\\&/g')

sed "s/{{WASM_CONFIG_JSON}}/${CONFIG_JSON}/" "$TEMPLATE" > "$RESOLVED"

exec envoy -c "$RESOLVED"
