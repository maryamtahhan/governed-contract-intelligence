#!/usr/bin/env bash
# Shared names for the four praxis routing policies.
#
# This is a thin adapter over demo/src/hybrid_demo/configs.json, the canonical
# record also used by the Python CLI, UI and report. Do not add a second name
# map here: a rename must change one file.
#
# The letters remain internal keys: existing run directories use arm-A..arm-D,
# and summary.json records arm=A..D. Scripts and the CLI use the names so a
# path or command says what the policy does.

CONFIGS_JSON="${CONFIGS_JSON:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/demo/src/hybrid_demo/configs.json}"

_config_field() {
  python3 - "$CONFIGS_JSON" "${1:-}" "$2" <<'PY'
import json
import sys

path, value, field = sys.argv[1:4]
records = json.load(open(path))["configs"]
wanted = value.strip().lower()
for record in records:
    names = {record["key"].lower(), record["name"].lower()}
    names.update(alias.lower() for alias in record.get("aliases", []))
    if wanted not in names:
        continue
    if field == "key":
        print(record["key"])
    elif field == "manifest":
        print(record["manifest"].rsplit("/", 1)[-1])
    else:
        print(record[field])
    break
else:
    sys.exit(1)
PY
}

config_code() {
  _config_field "${1:-}" key || {
    echo "unknown config: ${1:-}" >&2
    exit 1
  }
}

config_name() {
  _config_field "${1:-}" name || {
    echo "unknown config: ${1:-}" >&2
    exit 1
  }
}

config_file() {
  _config_field "${1:-}" manifest || {
    echo "unknown config: ${1:-}" >&2
    exit 1
  }
}
