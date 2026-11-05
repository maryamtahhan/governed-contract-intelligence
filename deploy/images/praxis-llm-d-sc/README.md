# praxis-ai with the llm_d_sc filter

**Image name:** `quay.io/rmalone/praxis-ai-llm-d-sc:v0.9.0` (not pushed yet)

The deployed gateway is currently
`quay.io/rmalone/praxis-ai-llm-d-sc:v0.7.0`. It is a custom Praxis build that
contains the out-of-tree [`llm_d_sc` filter][filter]. Upstream
`ghcr.io/praxis-proxy/ai` does not contain that filter, so updating the base
image alone would silently remove semantic classification from the gateway.

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
drop-in replacement for the cluster's amd64 nodes.

## Switching the manifests

Do not point the base manifests at the new tag until the image exists in the
registry. Once it does:

```bash
oc -n openshell-demo set image deploy/praxis \
  praxis=quay.io/rmalone/praxis-ai-llm-d-sc:v0.9.0
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
