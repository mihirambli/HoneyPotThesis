#!/bin/sh
set -e

# Paths inside envoy-wasm container — must match docker-compose volume mounts.
TEMPLATE="/etc/envoy/envoy-wasm.yaml"
RESOLVED="/tmp/envoy-wasm.yaml"
CONFIG_FILE="/etc/envoy/config.json"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "ERROR: $CONFIG_FILE not found" >&2
  exit 1
fi

# Minify to one line, then escape for a YAML SINGLE-quoted scalar (only ' needs doubling) and
# for sed's replacement text. Double-quoted YAML was previously used, but it processes escapes:
# a `"` in any config value collapsed to `\"` and terminated the scalar (Envoy refused to boot),
# and a `\n` was unescaped into a literal control character that serde_json then rejected.
# Single-quoted YAML processes no escapes, so `"` and `\` reach the filter untouched.
CONFIG_JSON=$(tr -d '\n' < "$CONFIG_FILE" | sed "s/'/''/g" | sed 's/[&\\/]/\\&/g')

# Produce a concrete Envoy config file for this run (Envoy does not expand env vars in YAML itself).
sed "s/{{WASM_CONFIG_JSON}}/${CONFIG_JSON}/" "$TEMPLATE" > "$RESOLVED"

exec envoy -c "$RESOLVED"
