#!/usr/bin/env bash
# Build the praxis-ai image that contains the llm_d_sc filter.
#
# The upstream project publishes a correct praxis-ai image, but it has no
# llm_d_sc filter: the filter is an out-of-tree Praxis extension. This script
# exports pinned sources, applies the one dependency update the filter needs for
# Praxis 0.7.5 / ai-gateway 0.9.0, wires the crate into the server, and builds.
#
# Versions are pinned by commit, not by branch. Bump them deliberately:
#
#   AI_GATEWAY_REF  praxis-proxy/ai origin/main at 2026-11-05
#                   (v0.9.0-20-ga716baae; pins praxis-proxy 0.7.5)
#   FILTER_REF      cnuland/llm-d-sc-praxis-filter main at 2026-11-05
#
# Usage:
#   ./build.sh                         # build for linux/amd64
#   ./build.sh --push                  # build and push to quay.io
#   ./build.sh --stage-only            # prepare context for a BuildConfig
#   IMAGE=quay.io/me/praxis:test ./build.sh
#
# The default sources are the local checkouts under ~/git-workspace. If a
# checkout is absent, the script clones it into the temporary build workspace.
# It never edits the source checkout: it exports the commit with `git archive`
# first.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-$(mktemp -d)}"
CTX="${CTX:-$WORK/context}"
AI_GATEWAY_DEFAULT="$HOME/git-workspace/ai-gateway"
FILTER_DEFAULT="$HOME/git-workspace/llm-d-sc-praxis-filter"
AI_GATEWAY_SRC="${AI_GATEWAY_SRC:-$AI_GATEWAY_DEFAULT}"
FILTER_SRC="${FILTER_SRC:-$FILTER_DEFAULT}"
AI_GATEWAY_REPO="${AI_GATEWAY_REPO:-https://github.com/praxis-proxy/ai}"
FILTER_REPO="${FILTER_REPO:-https://github.com/cnuland/llm-d-sc-praxis-filter}"

AI_GATEWAY_REF="${AI_GATEWAY_REF:-a716baaef1a027ff02a00bf4fc692de54c366cee}"
FILTER_REF="${FILTER_REF:-b84acfb840c7232fcba2680e46fd023ac8c7e02c}"
IMAGE="${IMAGE:-quay.io/rmalone/praxis-ai-llm-d-sc:v0.9.0}"
PLATFORM="${PLATFORM:-linux/amd64}"
PUSH=0
KEEP=0
STAGE_ONLY=0

while (($#)); do
  case "$1" in
    --push) PUSH=1 ;;
    --keep) KEEP=1 ;;
    --stage-only) STAGE_ONLY=1; KEEP=1 ;;
    --platform) PLATFORM="$2"; shift ;;
    --image) IMAGE="$2"; shift ;;
    --no-platform) PLATFORM="" ;;
    -h|--help)
      sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

log() { printf '==> %s\n' "$*" >&2; }

cleanup() {
  if ((KEEP)); then
    log "kept build workspace: $WORK"
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

ensure_repo() {
  local src="$1" repo="$2"
  if [[ ! -d "$src/.git" ]]; then
    log "cloning $repo"
    mkdir -p "$(dirname "$src")"
    git clone --quiet --filter=blob:none "$repo" "$src"
  fi
}

export_ref() {
  local src="$1" ref="$2" dest="$3"
  if ! git -C "$src" rev-parse --verify --quiet "$ref^{commit}" >/dev/null; then
    log "fetching $ref in $src"
    git -C "$src" fetch --quiet origin "$ref" || {
      echo "FATAL: cannot resolve $ref in $src" >&2
      exit 1
    }
  fi
  mkdir -p "$dest"
  git -C "$src" archive --format=tar "$ref" | tar -x -C "$dest"
}

# If the caller did not name a source and the usual checkout is absent, clone
# into the temporary workspace rather than creating a new checkout in $HOME.
if [[ "$AI_GATEWAY_SRC" == "$AI_GATEWAY_DEFAULT" && ! -d "$AI_GATEWAY_DEFAULT/.git" ]]; then
  AI_GATEWAY_SRC="$WORK/src/ai-gateway"
fi
if [[ "$FILTER_SRC" == "$FILTER_DEFAULT" && ! -d "$FILTER_DEFAULT/.git" ]]; then
  FILTER_SRC="$WORK/src/llm-d-sc-praxis-filter"
fi

log "build workspace: $WORK"
ensure_repo "$AI_GATEWAY_SRC" "$AI_GATEWAY_REPO"
ensure_repo "$FILTER_SRC" "$FILTER_REPO"

export_ref "$AI_GATEWAY_SRC" "$AI_GATEWAY_REF" "$CTX/ai-gateway"
export_ref "$FILTER_SRC" "$FILTER_REF" "$CTX/ai-gateway/vendor/llm-d-sc-praxis-filter"

# The filter was written against Praxis 0.5.2. ai-gateway 0.9.0 pins Praxis
# 0.7.5, so the filter must resolve the same packages or Cargo creates two
# incompatible copies of praxis-filter in the graph.
log "applying Praxis 0.7.5 dependency patch"
patch -s -p1 -d "$CTX/ai-gateway/vendor/llm-d-sc-praxis-filter" \
  < "$HERE/llm-d-sc-filter-0.7.5.patch"

log "wiring the filter into server/Cargo.toml"
python3 - "$CTX/ai-gateway/server/Cargo.toml" "$CTX/ai-gateway/Cargo.toml" <<'PY'
from pathlib import Path
import sys

server = Path(sys.argv[1])
root = Path(sys.argv[2])

text = server.read_text()
needle = "llm-d-sc-praxis-filter"
if needle not in text:
    marker = "[dependencies]\n"
    if marker not in text:
        raise SystemExit(f"FATAL: no [dependencies] table in {server}")
    text = text.replace(
        marker,
        marker + 'llm-d-sc-praxis-filter = { path = "../vendor/llm-d-sc-praxis-filter" }\n',
        1,
    )
    server.write_text(text)

# Keep the vendored crate out of the ai-gateway workspace. It carries a self
# dev-dependency that turns on its `test-server` feature; as an auto-included
# workspace member that is a cycle. As an excluded path dependency it remains
# visible to `cargo metadata` (which is how Praxis discovers filters) and to
# the server's dependency graph.
text = root.read_text()
if 'exclude = ["vendor/llm-d-sc-praxis-filter"]' not in text:
    marker = "[workspace]\n"
    if marker not in text:
        raise SystemExit(f"FATAL: no [workspace] table in {root}")
    text = text.replace(
        marker,
        marker + 'exclude = ["vendor/llm-d-sc-praxis-filter"]\n',
        1,
    )
    root.write_text(text)
PY

grep -q 'praxis-core = { version = "0.7.5"' \
  "$CTX/ai-gateway/vendor/llm-d-sc-praxis-filter/Cargo.toml" || {
    echo "FATAL: filter is not pinned to praxis 0.7.5" >&2
    exit 1
  }
grep -q 'praxis-core = { version = "0.7.5"' "$CTX/ai-gateway/Cargo.toml" || {
  # ai-gateway may format the pin differently; make the operator check it.
  echo "WARNING: could not confirm praxis 0.7.5 in ai-gateway Cargo.toml" >&2
}

cp "$HERE/Containerfile" "$CTX/Containerfile"
cp "$HERE/validate.yaml" "$CTX/validate.yaml"

if ((STAGE_ONLY)); then
  log "staged build context: $CTX"
  log "build with: podman build -f $CTX/Containerfile -t $IMAGE $CTX"
  exit 0
fi

command -v podman >/dev/null 2>&1 || {
  echo "FATAL: podman is required (or use --stage-only)" >&2
  exit 1
}

log "building $IMAGE${PLATFORM:+ for $PLATFORM}"
build_args=()
if [[ -n "$PLATFORM" ]]; then
  build_args+=(--platform "$PLATFORM")
fi
podman build \
  "${build_args[@]}" \
  --build-arg PRAXIS_AI_FEATURES=full \
  -f "$CTX/Containerfile" \
  -t "$IMAGE" \
  "$CTX"

if ((PUSH)); then
  log "pushing $IMAGE"
  podman push "$IMAGE"
fi

log "done: $IMAGE"
