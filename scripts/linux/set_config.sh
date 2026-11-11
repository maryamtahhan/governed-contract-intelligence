#!/usr/bin/env bash
# Swap the live praxis routing policy. Name-first alias for set_arm.sh.
#
#   scripts/linux/set_config.sh hybrid
#   scripts/linux/set_config.sh one-pool
#
# Legacy names (all-gpu, gpu-small, gpu-split) and A-D are still accepted
# for old scripts.
set -euo pipefail
exec "$(dirname "$0")/set_arm.sh" "$@"
