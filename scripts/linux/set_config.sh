#!/usr/bin/env bash
# Swap the live praxis routing policy. Name-first alias for set_arm.sh.
#
#   scripts/linux/set_config.sh hybrid
#   scripts/linux/set_config.sh all-gpu
#
# Legacy A-D are still accepted for old scripts.
set -euo pipefail
exec "$(dirname "$0")/set_arm.sh" "$@"
