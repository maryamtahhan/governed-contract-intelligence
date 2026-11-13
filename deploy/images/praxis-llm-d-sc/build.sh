#!/usr/bin/env bash
# Build the praxis-ai image that contains the llm_d_sc filter and the GCI
# PPE policy host plugins.
#
# The upstream project publishes a correct praxis-ai image, but it has no
# llm_d_sc filter: the filter is an out-of-tree Praxis extension. This script
# exports pinned sources, applies the one dependency update the filter needs for
# Praxis 0.7.5 / ai-gateway 0.9.0, wires the crate into the server, and builds.
#
# Since v0.9.1 it also vendors the PPE policy plugins the engine deliberately
# does not bundle (praxis-proxy/policy enforces that by test): the reference
# audit-logger sink, exported from the PPE v0.5.0 tag with an adapted
# standalone manifest (audit-logger.Cargo.toml), plus our host registrations
# in policy-host/ — the observe/gate PII scanners and the step-key binding
# gate. They register through gci-praxis-policy-host, discovered by the same
# build-time mechanism as the filter.
#
# Since v0.9.3 it also vendors gci-screen-filter (checked in at
# gci-screen-filter/): the V19 HTTP filter that scans request bodies with
# the shared PII pattern catalog and sets X-Demo-Screen for the router.
# Unlike the policy plugins it is an HTTP filter, discovered through
# [package.metadata.praxis-filters] exactly like llm_d_sc.
#
# Versions are pinned by commit, not by branch. Bump them deliberately:
#
#   AI_GATEWAY_REF  praxis-proxy/ai origin/main at 2026-11-05
#                   (v0.9.0-20-ga716baae; pins praxis-proxy 0.7.5)
#   FILTER_REF      cnuland/llm-d-sc-praxis-filter main at 2026-11-05
#   PPE_REF         praxis-proxy/policy at tag v0.4.1 (a178f14, 2026-10-05).
#                   The version is not a choice: the PUBLISHED praxis-filter
#                   0.7.5 depends on praxis-policy 0.4.1 from crates.io, so
#                   the vendored plugins must implement the 0.4.1
#                   PluginFactory trait or the graph holds two
#                   praxis-policy-core copies and the build fails (E0277,
#                   the exact failure the first v0.9.1 build caught). praxis
#                   main has bumped to PPE 0.5.0 (#1464) but no release
#                   carries it yet; when one does, bump this pin, the two
#                   vendored manifests, and the check below together.
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
PPE_DEFAULT="$HOME/git-workspace/policy"
AI_GATEWAY_SRC="${AI_GATEWAY_SRC:-$AI_GATEWAY_DEFAULT}"
FILTER_SRC="${FILTER_SRC:-$FILTER_DEFAULT}"
PPE_SRC="${PPE_SRC:-$PPE_DEFAULT}"
AI_GATEWAY_REPO="${AI_GATEWAY_REPO:-https://github.com/praxis-proxy/ai}"
FILTER_REPO="${FILTER_REPO:-https://github.com/cnuland/llm-d-sc-praxis-filter}"
PPE_REPO="${PPE_REPO:-https://github.com/praxis-proxy/policy}"

AI_GATEWAY_REF="${AI_GATEWAY_REF:-a716baaef1a027ff02a00bf4fc692de54c366cee}"
FILTER_REF="${FILTER_REF:-b84acfb840c7232fcba2680e46fd023ac8c7e02c}"
PPE_REF="${PPE_REF:-a178f14cb156aecef59d3e16de3cf7fe65c691db}"
IMAGE="${IMAGE:-quay.io/mtahhan/gci-praxis:v0.9.3}"
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
if [[ "$PPE_SRC" == "$PPE_DEFAULT" && ! -d "$PPE_DEFAULT/.git" ]]; then
  PPE_SRC="$WORK/src/policy"
fi

log "build workspace: $WORK"
ensure_repo "$AI_GATEWAY_SRC" "$AI_GATEWAY_REPO"
ensure_repo "$FILTER_SRC" "$FILTER_REPO"
ensure_repo "$PPE_SRC" "$PPE_REPO"

export_ref "$AI_GATEWAY_SRC" "$AI_GATEWAY_REF" "$CTX/ai-gateway"
export_ref "$FILTER_SRC" "$FILTER_REF" "$CTX/ai-gateway/vendor/llm-d-sc-praxis-filter"
export_ref "$PPE_SRC" "$PPE_REF" "$WORK/ppe-export"

# Vendor the PPE reference audit-logger: src from the pinned tag, with our
# adapted standalone manifest (the upstream one inherits workspace fields).
# The PII scanners, step-key gate and the host registration are ours,
# checked in at policy-host/.
log "assembling PPE policy host vendor tree"
mkdir -p "$CTX/ai-gateway/vendor/praxis-policy-plugin-audit-logger"
cp -R "$WORK/ppe-export/reference/plugins/audit-logger/src" \
  "$CTX/ai-gateway/vendor/praxis-policy-plugin-audit-logger/src"
cp "$HERE/audit-logger.Cargo.toml" \
  "$CTX/ai-gateway/vendor/praxis-policy-plugin-audit-logger/Cargo.toml"
cp -R "$HERE/policy-host" "$CTX/ai-gateway/vendor/gci-praxis-policy-host"
# The V19 in-gateway screen filter: an HTTP filter (unlike the policy
# plugins above) that sets X-Demo-Screen from a body scan. Checked in at
# gci-screen-filter/, same discovery path as the llm_d_sc filter.
cp -R "$HERE/gci-screen-filter" "$CTX/ai-gateway/vendor/gci-screen-filter"

# The filter was written against Praxis 0.5.2. ai-gateway 0.9.0 pins Praxis
# 0.7.5, so the filter must resolve the same packages or Cargo creates two
# incompatible copies of praxis-filter in the graph.
log "applying Praxis 0.7.5 dependency patch"
patch -s -p1 -d "$CTX/ai-gateway/vendor/llm-d-sc-praxis-filter" \
  < "$HERE/llm-d-sc-filter-0.7.5.patch"

log "wiring the filter and policy host into server/Cargo.toml"
python3 - "$CTX/ai-gateway/server/Cargo.toml" "$CTX/ai-gateway/Cargo.toml" <<'PY'
from pathlib import Path
import sys

server = Path(sys.argv[1])
root = Path(sys.argv[2])

text = server.read_text()
marker = "[dependencies]\n"
if marker not in text:
    raise SystemExit(f"FATAL: no [dependencies] table in {server}")
# Two external crates, both discovered through [package.metadata.
# praxis-filters]: the llm_d_sc HTTP filter, the gci_screen HTTP filter,
# and the policy host whose register_filters() registers the PPE plugin
# factories.
for dep in ("llm-d-sc-praxis-filter", "gci-screen-filter", "gci-praxis-policy-host"):
    if dep not in text:
        text = text.replace(
            marker,
            f"{marker}{dep} = {{ path = \"../vendor/{dep}\" }}\n",
            1,
        )
server.write_text(text)

# Keep the vendored crates out of the ai-gateway workspace. The filter
# carries a self dev-dependency that turns on its `test-server` feature; as
# auto-included workspace members that is a cycle. As excluded path
# dependencies they remain visible to `cargo metadata` (which is how Praxis
# discovers filters) and to the server's dependency graph.
text = root.read_text()
excludes = 'exclude = ["vendor/llm-d-sc-praxis-filter", "vendor/gci-screen-filter", "vendor/gci-praxis-policy-host", "vendor/praxis-policy-plugin-audit-logger"]'
if excludes not in text:
    marker = "[workspace]\n"
    if marker not in text:
        raise SystemExit(f"FATAL: no [workspace] table in {root}")
    text = text.replace(marker, marker + excludes + "\n", 1)
    root.write_text(text)
PY

grep -q 'praxis-core = { version = "0.7.5"' \
  "$CTX/ai-gateway/vendor/llm-d-sc-praxis-filter/Cargo.toml" || {
    echo "FATAL: filter is not pinned to praxis 0.7.5" >&2
    exit 1
  }
grep -q 'praxis-filter = { version = "0.7.5"' \
  "$CTX/ai-gateway/vendor/gci-screen-filter/Cargo.toml" || {
      echo "FATAL: gci-screen-filter is not pinned to praxis-filter 0.7.5" >&2
      exit 1
    }
grep -q 'praxis-core = { version = "0.7.5"' "$CTX/ai-gateway/Cargo.toml" || {
  # ai-gateway may format the pin differently; make the operator check it.
  echo "WARNING: could not confirm praxis 0.7.5 in ai-gateway Cargo.toml" >&2
}
# The vendored plugins must resolve to the praxis-policy-core the published
# praxis-filter 0.7.5 pins (0.4.1 — its 0.5.0 bump is unreleased), or the
# PluginFactory trait splits in two and the server fails to compile. Both
# manifests say so explicitly.
for crate in praxis-policy-plugin-audit-logger gci-praxis-policy-host; do
  grep -q '^praxis-policy-core = "0.4.1"' \
    "$CTX/ai-gateway/vendor/$crate/Cargo.toml" || {
      echo "FATAL: $crate is not pinned to praxis-policy-core 0.4.1" >&2
      exit 1
    }
done

cp "$HERE/Containerfile" "$CTX/Containerfile"
cp "$HERE/validate.yaml" "$CTX/validate.yaml"
cp "$HERE/validate-policy.yaml" "$CTX/validate-policy.yaml"

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
