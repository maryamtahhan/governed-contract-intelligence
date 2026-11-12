# praxis-ai with the llm_d_sc filter and the PPE policy host

**Image name:** `quay.io/mtahhan/gci-praxis:v0.9.2` (RC1, published
2026-11-12 in run `43203547413`, digest
`sha256:3140a8dad2626ef71504546fa7901f53d9ec68ac49ed20e39919ae0af71c77c6`).
`build.sh`, the publish workflow, base manifests and compose all name it.
It is the current measured image: the 2026-11-12 real-pool ROSA matrix and
the 28/28 `STACK=rosa` resilience smoke ran this digest. The previous measured
image is
`quay.io/mtahhan/gci-praxis:v0.9.1` (2026-11-06, digest
`sha256:d17410449aec2f30a8d07819503ad00badaab398a4f18a1d4692961026c91c56`);
the earlier build was `v0.9.0` (2026-11-05, digest
`sha256:2187ab818efab9a8f3d1f911190d47fad960888bada7d3db8a973d3ad70ac992`),
which the v0.9.0-era configs ran.

A custom Praxis build containing two out-of-tree additions the upstream
image does not have:

- the [`llm_d_sc` filter][filter] (semantic-classification routing), and
- since v0.9.1, the **PPE policy host plugins**: the reference
  `audit/logger` sink vendored from praxis-proxy/policy's v0.5.0 tag and
  `validator/gci-pii-observe` (observe-only comparison scanner);
- since v0.9.2, the RC1 plugins: `validator/gci-pii-gate` (enforcing scanner
  for the general entry) and `validator/gci-step-key` (binds an authenticated
  key to its `X-Demo-Step` stage).

The PII pattern catalog mirrors `demo/src/hybrid_demo/pii.py`. All register
through [`policy-host/`](policy-host/) via the same build-time discovery as
the filter. Updating the base image alone would silently remove them.

> **Live validation (2026-11-12):** the published image passed
> `--validate` with all four plugin kinds at build time. A fresh ROSA
> fake-pool stack ran `scripts/cluster/smoke.sh` with **27/27 checks**,
> including the step-key and PII-gate negative cases. The real `x8i.24xlarge`
> + `g5.12xlarge` stack then passed the two-document smoke
> (`runs/rosa-smoke-2026-11-12`), the live gate assertions
> (`runs/rosa-policy-2026-11-12-real`) and the 200-document matrix
> (`runs/rosa-200-2026-11-12-praxis-routes`): hybrid 818.87 s, 0 errors,
> 247/247 sensitive steps approved, F1 1.000, risk 0.995, report 0.998,
> 199/200 fully correct, 0.259 GPU-s/doc and 33.006 CPU core-s/doc. The same
> real stack then passed the parameterized `STACK=rosa` suite **28/28**
> (`runs/rosa-resilience-2026-11-12`), including CPU fallback/recovery and
> shared-GPU sensitive fail-closed/recovery. The policy declarations must
> grant `read_roles` to the step-key and audit
> plugins: `read_subject` alone projects only the subject id, so the step
> gate would see an empty role set and deny every keyed request. This is a
> policy configuration contract, not an image-tag issue.

The previous published build before this lineage was
`quay.io/rmalone/praxis-ai-llm-d-sc:v0.7.0` (Praxis 0.7.0).

This directory is the reproducible path to the current upstream stack:

| Component | Pin |
| --- | --- |
| praxis-proxy | `0.7.5` (dependency of ai-gateway 0.9.0) |
| ai-gateway | `a716baae` (`v0.9.0-20-ga716baae`, origin/main on 2026-11-05) |
| llm-d-sc filter | `b84acfb` (cnuland/llm-d-sc-praxis-filter) |
| filter patch | [`llm-d-sc-filter-0.7.5.patch`](llm-d-sc-filter-0.7.5.patch) |
| PPE (policy engine) | `a178f14` (praxis-proxy/policy **tag v0.4.1** — the version the published praxis-filter 0.7.5 pins; see below) |
| audit-logger | vendored from that tag, manifest adapted in [`audit-logger.Cargo.toml`](audit-logger.Cargo.toml) |
| policy host + PII observe/gate + step-key | [`policy-host/`](policy-host/) (ours) |

The filter was written against Praxis 0.5.2. The patch updates only its
`praxis-core`, `praxis-filter`, and `yaml_serde` pins to the versions
ai-gateway 0.9.0 already resolves. Resolving a different Praxis in the filter
would put two incompatible `praxis_filter` crates in the graph; the filter and
the server traits must be the same type. The same one-instance rule is why the
PPE pin is **v0.4.1**: the published praxis-filter 0.7.5 depends on
praxis-policy 0.4.1 from crates.io, and the vendored plugins must implement
that core's `PluginFactory` trait. (The first v0.9.1 build failed exactly here
by pinning v0.5.0: two `praxis-policy-core` copies, E0277. praxis main has
bumped to PPE 0.5.0 but no release carries it; when one does, bump the pin,
the two vendored manifests, and build.sh's check together.)

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
It also loads a policy document naming every host-registered plugin kind
(`audit/logger`, `validator/gci-pii-observe`, `validator/gci-pii-gate`,
`validator/gci-step-key`), so a missing registration fails the image build,
not a demo run. If the external filter were not discovered by the server's
build script, or if its config schema disagreed with the config, the build
would fail instead of producing a gateway that loses classification at
runtime.

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
[`publish-images.yml`](../../../.github/workflows/publish-images.yml) runs on
manual dispatch only:

| Job | Image |
| --- | --- |
| gateway | `quay.io/mtahhan/gci-praxis:v0.9.2` — this directory's `build.sh --push` |

The workflow needs the `QUAY_USERNAME` / `QUAY_PASSWORD` repository secrets: a
quay robot account with push access to the `quay.io/mtahhan/gci-*`
repositories. Each job writes the pushed digest into the run summary; record
that digest, not the tag, for measured runs.

The classifier (`quay.io/mtahhan/gci-llm-d-sc:main-e660fff`) and its mirrored
modelcar were published once (2026-11-05) but are **not consumed by the
default stack** — llm-d-sc was removed from the request path the same day
(FINDINGS §15.5). The images stay in the registry for the contract-step
taxonomy experiment (`docs/design.md` §5); the workflow jobs are
gone, and a rebuild means restoring them from git history (commit `4ac0551`).

Published 2026-11-05 (run 42043729532):

| Image | Digest | Consumed by the default stack |
| --- | --- | --- |
| `quay.io/mtahhan/gci-praxis:v0.9.0` | `sha256:2187ab818efab9a8f3d1f911190d47fad960888bada7d3db8a973d3ad70ac992` | superseded by v0.9.1 |
| `quay.io/mtahhan/gci-llm-d-sc:main-e660fff` | `sha256:d94c1f7226bfbaeb5a300f0efdbab0e8a8988fc16af7fd80cff0e008a11fa3e1` | no — experiment only |
| `quay.io/mtahhan/gci-llm-d-sc-modelcar:complexity` | `sha256:299874af83c565d8435c8e061b87da1754cdd918ca15e977a7fecbf3aa17f491` (mirror, byte-identical to the source) | no — experiment only |

Published 2026-11-06 (run 42202721911, three attempts: the first two caught the
praxis-policy-core version seam — published praxis-filter 0.7.5 pins 0.4.1,
not main's 0.5.0 — and the RPITIT trait shape; see the build-commit messages):

| Image | Digest | Consumed by the default stack |
| --- | --- | --- |
| `quay.io/mtahhan/gci-praxis:v0.9.2` | `sha256:3140a8dad2626ef71504546fa7901f53d9ec68ac49ed20e39919ae0af71c77c6` | current base manifests and Linux compose — registers the RC1 gate and step-key plugins; measured in the 2026-11-12 real-pool matrix, passed the 28/28 real-pool resilience smoke and the 27/27 fake-pool policy smoke |
| `quay.io/mtahhan/gci-praxis:v0.9.1` | `sha256:d17410449aec2f30a8d07819503ad00badaab398a4f18a1d4692961026c91c56` | previous 2026-11-11 measured record — audit observer + observe-only PII scanner; predates the RC1 gate and step-key plugins |

## Switching the manifests

The base manifests and the Linux compose stack point at `v0.9.2`, the
published RC1 image; the 2026-11-12 real-pool stack ran `v0.9.2`, and the
2026-11-11 record ran `v0.9.1`. Applying the current policies against an
older image fails gateway validation because the policy names plugin kinds
that image does not register — that is the intended fail-closed behavior,
not a config bug. The opt-in
`deploy/openshift/praxis-0.9.0/` overlay is retired — base is the current
stack. A live cluster still running the v0.7.0 pair rolls with
`oc apply -k manifests` (or its own overlay); since the classifier removal
that apply also drops the llm-d-sc Deployment. To move only the gateway:

```bash
oc -n openshell-demo set image deploy/praxis \
  praxis=quay.io/mtahhan/gci-praxis:v0.9.2
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
