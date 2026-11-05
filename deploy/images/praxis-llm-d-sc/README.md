# praxis-ai with the llm_d_sc filter

**Image name:** `quay.io/mtahhan/gci-praxis:v0.9.0` — published 2026-11-05,
digest `sha256:2187ab818efab9a8f3d1f911190d47fad960888bada7d3db8a973d3ad70ac992`

A custom Praxis build containing the out-of-tree [`llm_d_sc` filter][filter].
Upstream `ghcr.io/praxis-proxy/ai` does not contain that filter, so updating
the base image alone would silently remove semantic classification from the
gateway. The previous published build was
`quay.io/rmalone/praxis-ai-llm-d-sc:v0.7.0` (Praxis 0.7.0).

This directory is the reproducible path to the current upstream stack:

| Component | Pin |
| --- | --- |
| praxis-proxy | `0.7.5` (dependency of ai-gateway 0.9.0) |
| ai-gateway | `a716baae` (`v0.9.0-20-ga716baae`, origin/main on 2026-11-05) |
| llm-d-sc filter | `b84acfb` (cnuland/llm-d-sc-praxis-filter) |
| filter patch | [`llm-d-sc-filter-0.7.5.patch`](llm-d-sc-filter-0.7.5.patch) |

The filter was written against Praxis 0.5.2. The patch updates only its
`praxis-core`, `praxis-filter`, and `yaml_serde` pins to the versions
ai-gateway 0.9.0 already resolves. Resolving a different Praxis in the filter
would put two incompatible `praxis_filter` crates in the graph; the filter and
the server traits must be the same type.

## Build

The host build is driven by [`build.sh`](build.sh). It exports the pinned
commits with `git archive` (it does not edit the local checkouts), vendors the
filter under `ai-gateway/vendor/`, adds the one dependency line to
`server/Cargo.toml`, excludes the vendored crate from the workspace, and builds
with the custom [`Containerfile`](Containerfile).

```bash
./deploy/images/praxis-llm-d-sc/build.sh
# or, on an amd64 builder:
PLATFORM=linux/amd64 ./deploy/images/praxis-llm-d-sc/build.sh
./deploy/images/praxis-llm-d-sc/build.sh --push
```

The default sources are `~/git-workspace/ai-gateway` and
`~/git-workspace/llm-d-sc-praxis-filter`; missing checkouts are cloned into the
temporary workspace. `AI_GATEWAY_REF` and `FILTER_REF` override the pins.

To hand the context to an OpenShift Docker-strategy BuildConfig:

```bash
./deploy/images/praxis-llm-d-sc/build.sh --stage-only
# prints the context path and the podman build command
```

The build context is the patched ai-gateway tree plus `Containerfile` and
`validate.yaml`. A binary BuildConfig that starts from a fresh pod should use
that directory as its context and `Containerfile` as `dockerfilePath`.

## What the build proves

`Containerfile` runs the gateway's real startup validation before the image is
accepted:

```text
praxis-ai --validate -c /tmp/validate.yaml
```

That constructs the `llm_d_sc` filter from its config schema, checks the
filter/load-balancer cluster contract, and exits without contacting llm-d-sc.
If the external filter were not discovered by the server's build script, or if
its config schema disagreed with the config, the build would fail instead of
producing a gateway that loses classification at runtime.

The wiring was also checked directly against the staged context with Rust 1.96
(the workspace toolchain):

```text
cargo check -p praxis-ai-proxy --no-default-features --features full
cargo build -p praxis-ai-proxy --no-default-features --features full
./target/debug/praxis-ai --validate -c validate.yaml
```

All three passed. The release image still needs to be built on an amd64 builder
and pushed; the local machine is arm64, so a local default build is not a
drop-in replacement for the cluster's amd64 nodes. The publish workflow below
is that builder.

## Publishing

The amd64 images are published from a GitHub Actions runner (native amd64,
unlike this repo's arm64 dev machines, where the default `--platform
linux/amd64` build runs emulated and slow).
[`publish-images.yml`](../../.github/workflows/publish-images.yml) runs on
manual dispatch only:

| Job | Image |
| --- | --- |
| gateway | `quay.io/mtahhan/gci-praxis:v0.9.0` — this directory's `build.sh --push` |

The workflow needs the `QUAY_USERNAME` / `QUAY_PASSWORD` repository secrets: a
quay robot account with push access to the `quay.io/mtahhan/gci-*`
repositories. Each job writes the pushed digest into the run summary; record
that digest, not the tag, for measured runs.

The classifier (`quay.io/mtahhan/gci-llm-d-sc:main-e660fff`) and its mirrored
modelcar were published once (2026-11-05) but are **not consumed by the
default stack** — llm-d-sc was removed from the request path the same day
(FINDINGS §15.5). The images stay in the registry for the contract-step
taxonomy experiment (`docs/design.md` §5); the workflow jobs are
gone, and a rebuild means restoring them from git history (commit `aa080e9`).

Published 2026-11-05 (run 42043729532):

| Image | Digest | Consumed by the default stack |
| --- | --- | --- |
| `quay.io/mtahhan/gci-praxis:v0.9.0` | `sha256:2187ab818efab9a8f3d1f911190d47fad960888bada7d3db8a973d3ad70ac992` | yes |
| `quay.io/mtahhan/gci-llm-d-sc:main-e660fff` | `sha256:d94c1f7226bfbaeb5a300f0efdbab0e8a8988fc16af7fd80cff0e008a11fa3e1` | no — experiment only |
| `quay.io/mtahhan/gci-llm-d-sc-modelcar:complexity` | `sha256:299874af83c565d8435c8e061b87da1754cdd918ca15e977a7fecbf3aa17f491` (mirror, byte-identical to the source) | no — experiment only |

## Switching the manifests

The base manifests point at this image (since 2026-11-05) and the opt-in
`deploy/openshift/praxis-0.9.0/` overlay is retired — base is the 0.9.0
stack now. A live cluster still running the v0.7.0 pair rolls with
`oc apply -k manifests` (or its own overlay); since the classifier removal
that apply also drops the llm-d-sc Deployment. To move only the gateway:

```bash
oc -n openshell-demo set image deploy/praxis \
  praxis=quay.io/mtahhan/gci-praxis:v0.9.0
oc -n openshell-demo rollout status deploy/praxis
```

Pin the pushed image by digest in a quoted run. The tag is mutable; the digest
is the reproducibility boundary.

The filter's `signal` field must match the llm-d-sc instance's classifier
signal. For the contract-step taxonomy that is `contract_step`; for the current
complexity instance it is unset (send no signal) or `complexity`. A mismatch is
an `INVALID_ARGUMENT`, not a fallback.

## Known limits

- The filter extracts the last `user` message. A request with only tool results,
  an assistant turn, or no user message is a `SKIPPED_NO_PROMPT` and takes
  `default_cluster`.
- A single llm-d-sc instance serves one signal. Multi-signal support is not
  implemented in the filter; run one instance per taxonomy.
- With `on_unavailable: default_cluster`, a classifier outage sends all stages
  to one pool. With `reject`, it fails every stage. Stage routing cannot
  reproduce B's judgment-only fail-closed property from inside one classifier;
  that property comes from separate listeners, which is why B was implemented
  as a judgment split rather than a classifier label.

[filter]: https://github.com/cnuland/llm-d-sc-praxis-filter
