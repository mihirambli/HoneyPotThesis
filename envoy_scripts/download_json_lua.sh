#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
curl -sL https://raw.githubusercontent.com/rxi/json.lua/master/json.lua \
  -o "$SCRIPT_DIR/json.lua"
echo "json.lua downloaded to $SCRIPT_DIR/"
