# RAS Agent Instructions

Read these files in order before making substantial changes:

1. [`docs/KNOWLEDGE.md`](docs/KNOWLEDGE.md)
2. [`agent_tasks/README.md`](agent_tasks/README.md)
3. [`agent_tasks/plans/illinois-taudem-primary.md`](agent_tasks/plans/illinois-taudem-primary.md)

Working rules for this repo:

- Treat `ras-agent` as Illinois-first unless a new regional profile is explicitly added.
- Do not introduce non-Illinois regional defaults into runtime code, tests, or docs unless a new regional profile is explicitly added.
- Upstream reusable TauDEM, watershed-preprocessing, and hydrology-support functions to `hms-commander` when they are not Illinois-specific.
- Upstream reusable HEC-RAS project, geometry, compilation, execution, and results functions to `ras-commander`.
- When `ras-agent` discovers a reusable feature gap in a sibling repo, open or reference a GitHub issue in the target repo and track the issue link locally; do not rely on repo-local markdown handoff files as the system of record.
- Keep `ras-agent` focused on Illinois adaptation, orchestration, and product integration that composes the shared libraries.
- Direct TauDEM CLI is the authoritative watershed-processing path.
- Treat `ras-commander` as the main interface for HEC-RAS project editing and execution.
- Treat the plain-text `.g##` geometry file as authoritative for geometry-backed content.
- Treat the current `hdf5_direct` path as experimental scaffolding, not the target architecture.
- `template_clone` is a legacy fallback, not the default contract.
- The repo now includes a starter HEC-RAS 6.6 template scaffold at `data/RAS_6.6_Template/`, but it is not yet a full 1D/2D clone-ready template inventory.
- WhiteboxTools belongs in a separate benchmark worktree, not the mainline implementation.
- `rivnet` / `traudem` are reference tools only and should not become required runtime dependencies.
