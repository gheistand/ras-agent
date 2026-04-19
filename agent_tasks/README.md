# Agent Tasks

This directory is the local planning layer for `ras-agent`.

Use it to track:

- active roadmap items
- completion status for major workstreams
- benchmark and validation gates that are not obvious from tests alone
- links to upstream GitHub issues in sibling repos when this repo is blocked on shared-library feature gaps

Rules:

- Keep one clearly active roadmap plan.
- Move superseded or exploratory work out of the active plan instead of letting it linger as implied scope.
- Record benchmark requirements explicitly when changing hydro-processing methods.
- If work needed in `hms-commander` or `ras-commander` is broadly reusable, the request should live as a GitHub issue in the target repo. Record the issue link here or in the active plan; do not treat local markdown notes as the canonical request.

Active plan:

- [`plans/illinois-taudem-primary.md`](plans/illinois-taudem-primary.md)
