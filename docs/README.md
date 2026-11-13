# Documentation

[← Back to the repo README](../README.md)

This directory holds the durable design, the stage material, and the demo deck.
Start with [design.md](design.md); use [stage-runbook.md](stage-runbook.md) to
run the demo; use [FINDINGS.md](../FINDINGS.md) for measurements.

| Document | Purpose |
|---|---|
| [design.md](design.md) | System map, routing and governance decisions, open items |
| [stage-runbook.md](stage-runbook.md) | One-page, three-beat stage script |
| [stage-review-walkthrough.html](stage-review-walkthrough.html) | Presenter guide for the dashboard |
| [demo-deck.html](demo-deck.html) | 19-slide demo and findings deck (arrow keys, `N` notes, `P` print) |
| [octo-demo-v0.3.md](octo-demo-v0.3.md) | OCTO demo plan and business case |
| [architecture-as-built.svg](architecture-as-built.svg) | As-built architecture diagram |
| [pipeline-direct.svg](pipeline-direct.svg) | Direct-run shape |
| [pipeline-through-stack.svg](pipeline-through-stack.svg) | Through-stack request path |

## Published slides

The deck is published at
[maryamtahhan.github.io/governed-contract-intelligence](https://maryamtahhan.github.io/governed-contract-intelligence/).
GitHub Actions copies `demo-deck.html` to the site root and publishes the SVG
diagrams alongside it. Edit the source file and push to `main`; the `pages`
workflow redeploys.

## Archive

Older handoffs and superseded documents are kept in git history, not in this
directory. Links in the design doc name the commit where each was archived.
