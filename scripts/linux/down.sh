#!/usr/bin/env bash
# Stop the local Linux stack. Add -v to also delete the trace database.
set -euo pipefail
cd "$(dirname "$0")/../.."
COMPOSE="${COMPOSE:-docker compose}"
if [ "${1:-}" = "-v" ]; then
  $COMPOSE -f deploy/linux/docker-compose.yml down -v
else
  $COMPOSE -f deploy/linux/docker-compose.yml down
fi
