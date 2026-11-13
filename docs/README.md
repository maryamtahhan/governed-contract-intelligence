# Documentation

[← Back to the repo README](../README.md)

This directory holds the durable design, the stage material, and the demo deck.
Start with [design.md](design.md); use [stage-runbook.md](stage-runbook.md) to
run the demo; use [FINDINGS.md](../FINDINGS.md) for measurements.

## Design and runbooks

| Document | Purpose |
|---|---|
| [design.md](design.md) | System map, routing and governance decisions, open items |
| [stage-runbook.md](stage-runbook.md) | One-page, three-beat stage script |

## Deck

| Document | Purpose |
|---|---|
| [deck/demo-deck.html](deck/demo-deck.html) | 19-slide demo and findings deck (arrow keys, `N` notes, `P` print) |
| [deck/stage-review-walkthrough.html](deck/stage-review-walkthrough.html) | Presenter guide for the dashboard |

## Diagrams

| Document | Purpose |
|---|---|
| [diagrams/architecture-as-built.svg](diagrams/architecture-as-built.svg) | As-built architecture diagram |
| [diagrams/pipeline-direct.svg](diagrams/pipeline-direct.svg) | Direct-run shape |
| [diagrams/pipeline-through-stack.svg](diagrams/pipeline-through-stack.svg) | Through-stack request path |

## Published slides

The deck is published at
[maryamtahhan.github.io/governed-contract-intelligence](https://maryamtahhan.github.io/governed-contract-intelligence/).
GitHub Actions publishes the deck at `deck/` and the SVG diagrams at
`diagrams/`, preserving their relative paths, and puts a redirect at the site
root. Edit the source file and push to `main`; the `pages` workflow redeploys.

## Archive

Older handoffs and superseded documents are kept in git history, not in this
directory. Links in the design doc name the commit where each was archived.
