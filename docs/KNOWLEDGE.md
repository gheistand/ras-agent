# RAS Agent Knowledge

Last revised: 2026-04-24

## Current Truth

- `ras-agent` is the active Illinois-first implementation repo.
- This repo focuses on Illinois adaptation and integration of TauDEM-backed watershed processing.
- `ras-commander` should be treated as the primary HEC-RAS project interface.
- `hms-commander` and `ras-commander` are the shared library/tool repos that should absorb reusable functions discovered while building `ras-agent`.
- `hms-commander` now provides a reusable Spring Creek benchmark for study packaging, direct TauDEM execution, watershed verification, TauDEM-to-HMS assembly, parser-of-record HMS validation, and a live Atlas 14 compute demonstration.
- `ras-agent` should consume shared commander functionality through the latest
  published pip packages, not through local sibling-repo working trees.
- Plain-text geometry should be treated as the source of truth for geometry-backed model content.
- The current `hdf5_direct` and `template_clone` paths in `ras-agent` are legacy compatibility surfaces and should not define the long-term architecture.
- Mesh generation should stay geometry-first and RASMapper-aligned. Cartesian mesh generation should not be carried forward as an alternate runtime path; only compatible QA concepts or implementation details should be ported.
- The repo now includes a starter HEC-RAS 6.6 project scaffold in `data/RAS_6.6_Template/`, but it is not yet a full 1D/2D seed-project inventory.

## Scope

The repo automates:

1. Terrain acquisition and clipping
2. Watershed delineation
3. Peak-flow estimation
4. Hydrograph generation
5. Seed HEC-RAS project generation
6. Execution orchestration
7. Results export and API delivery

The repo also now carries the Illinois integration layer for the shared base engineering
workspace contract:

- `report.html`
- `report.json`
- `data_gap_analysis.json`
- `manifest.json`

The repo does not currently guarantee:

- production-ready greenfield mesh generation without Windows regeneration
- a finished 1D/2D geometry-first seed-project inventory
- non-Illinois regional defaults
- WhiteboxTools parity in the mainline code path

## Regional Defaults

- Primary support region: Illinois
- Default processing CRS: `EPSG:5070`
- Hydrology assumptions and documentation should remain Illinois-first unless a new region-specific profile is explicitly added

These are not valid repo-wide defaults:

- non-Illinois regional parameter defaults
- alternate processing CRS values without an explicit regional profile
- basin-specific benchmark assumptions presented as general defaults
- borrowed infiltration starting values presented as repo-wide defaults

## Active Architecture

## Generalizability Rule

Land work in the repo where it is most reusable.

- If a TauDEM or watershed-processing method is generalizable beyond this Illinois workflow, it belongs in `hms-commander`.
- If a HEC-RAS editing, geometry, compilation, execution, or results primitive is generalizable, it belongs in `ras-commander`.
- If the code is primarily an Illinois profile, orchestration layer, application workflow, or product integration that composes shared capabilities, it belongs in `ras-agent`.

`ras-agent` may carry thin adapters while a shared capability is being proven, but reusable primitives should be upstreamed rather than kept here indefinitely.

When `ras-agent` identifies a missing reusable capability in `hms-commander` or `ras-commander`, the canonical request should be a GitHub issue in the target repository. Local plans in `ras-agent` should track the issue link and integration impact, but the issue tracker in the target repo should be treated as the system of record for the feature gap.

### Terrain

- `pipeline/terrain.py` acquires DEM data and clips terrain products for the delineated watershed.
- Illinois remains the default design center for terrain sourcing and processing.

### Watershed delineation

- `pipeline/taudem.py` provides the local TauDEM execution wrapper used by the Illinois adaptation layer.
- `pipeline/watershed.py` orchestrates `PitRemove`, `D8FlowDir`, `AreaD8`, `Threshold`, `MoveOutletsToStreams`, `Gridnet`, and `StreamNet` for Illinois-focused delineation workflows.
- The long-term shared baseline for those steps is now the upstream `hms-commander` TauDEM surface, not indefinite duplication inside `ras-agent`.
- `WatershedResult` now carries:
  - `basin`
  - `streams`
  - `subbasins`
  - `centerlines`
  - `breaklines`
  - `pour_point`
  - `characteristics`
  - `dem_clipped`
  - `artifacts`

Design rule:

- Preserve TauDEM intermediate files as first-class artifacts so model building and QA can inspect the exact preprocessing lineage.

### Hydrology

- `pipeline/streamstats.py` remains Illinois-focused.
- `pipeline/hydrograph.py` continues to generate design hydrographs from peak-flow inputs.

### Model build

- The intended architecture is:
  - `ras-agent` derives watershed geometry, hydrology, and parameter instructions
  - `ras-commander` edits the HEC-RAS project and plain-text geometry artifacts
  - HEC-RAS recompiles geometry HDF and preprocessor files from those instructions
- For geometry-backed content, `.g##` should remain authoritative.
- For land-cover roughness content, use `ras_commander.geom.GeomLandCover` and related geometry-side workflows.
- For infiltration and soils compilation, use `ras-commander` HDF-backed workflows such as `HdfInfiltration` plus `RasMap`/terrain-side context.
- The present `hdf5_direct` and `template_clone` implementations in `pipeline/model_builder.py` should be treated as legacy compatibility paths, not target architecture or recommended fallbacks.
- `data/RAS_6.6_Template/` is the current seed-project scaffold for geometry-first work. It currently contains a `.prj` and `.rasmap` starter, and still needs real 1D/2D geometry, flow, and plan content before real-run workflows should rely on it.
- Boundary-condition mode is now scaffolded through `build_model()`, `run_watershed()`, and `run_batch()` as `headwater` vs `downstream`.
- `headwater` remains the only implemented behavior. `downstream` currently fails fast by design so the API surface is explicit without pretending chained-basin support is complete.
- Before enabling `downstream`, finish at least:
  - the input contract for upstream hydrograph sources/provenance
  - the model-builder handoff for non-headwater inflow BC generation
  - regression fixtures and QA expectations for chained basins

### Execution and results

- `pipeline/orchestrator.py` and `pipeline/batch.py` now default to `geometry_first`; legacy mesh strategies should not drive new work.
- `pipeline/runner.py`, `pipeline/results.py`, `pipeline/api.py`, and `web/` remain valid, but real-run QA still depends on Windows-side regeneration and verification.
- `pipeline/report.py` is the current Spring Creek-derived reference implementation for the self-contained base engineering HTML report.
- `pipeline/workspace.py` is the current command surface for workspace validation and report-package generation.

## Repo Scope

Use `ras-agent` for:

- Illinois-first watershed and HEC-RAS automation
- Illinois-focused adaptation of TauDEM-backed watershed inputs
- orchestration of watershed-derived inputs into `ras-commander`
- benchmark-driven validation against alternative preprocessing paths
- tracking which upstream GitHub issues in sibling repos block or enable this integration work

Do not use `ras-agent` as the long-term home for:

- reusable TauDEM example workflows
- reusable watershed-preprocessing building blocks
- reusable HEC-RAS geometry or execution primitives

## Active Roadmap

The active plan lives at [`../agent_tasks/plans/illinois-taudem-primary.md`](../agent_tasks/plans/illinois-taudem-primary.md).

Priority order:

1. Keep repo ownership aligned with generalizability so reusable methods land in `hms-commander` or `ras-commander`
2. Consume the upstream `hms-commander` Spring Creek handoff package rather than rebuilding hydrology-side provenance locally
3. Use Spring Creek as the first runnable headwater pilot for BLE-style data generation and gauge calibration/validation
4. Implement the simpler rain-on-grid AORC/MRMS setup through `ras-commander` first
5. Add precipitation-source QAQC for rain-on-grid calibration events by comparing AORC/MRMS gridded accumulations against nearby station observations before parameter calibration decisions are made
6. Continue HMS modeling in parallel through `hms-commander`, then build the HMS-linked boundary-construction workflow once the HMS path is complete enough to trust
7. Require the upstream pre-HMS readiness gate and human-review QAQC signoff before treating generated HMS content as generalized production hydrology
8. Future calibration work should be reviewer-in-the-loop and batch-oriented: begin with base hydrologic/hydraulic parameters, run individual parameter sensitivity batches, update the base set after expert review, then use the sensitivity record to define a small multi-parameter validation matrix with high-resolution sweeps only for the most influential parameters
9. Keep mesh generation geometry-first and RASMapper-aligned; do not preserve Cartesian mesh generation as a fallback path
10. Implement the remaining `ras-commander` features needed for watershed-driven 2D flow area creation and compilation
11. Validate compiled geometry/regeneration workflows on Illinois basins
12. Add benchmark fixtures and comparison reporting
13. Keep `rivnet` / `traudem` as a reference track only
14. Keep WhiteboxTools in a separate benchmark worktree only

## Benchmark Rules

Benchmarking is required before any alternative backend becomes credible.

Authoritative baseline:

- direct TauDEM CLI

Reference-only comparison track:

- `rivnet` / `traudem`

Separate worktree comparison track:

- WhiteboxTools

Acceptance criteria should compare:

- snapped outlet location
- stream network structure
- subbasin geometry
- area totals
- derived main-channel metrics
- runtime and artifact quality

If direct TauDEM and a comparison backend disagree, direct TauDEM remains authoritative unless a documented regulatory-method reason proves otherwise.

## Testing Expectations

Minimum repo checks after hydro-processing changes:

```bash
python -m pytest tests/test_model_builder.py tests/test_orchestrator.py tests/test_taudem.py -v
python -m compileall pipeline tests
```

When real-basin fixtures are added, test coverage should expand to:

- executable discovery and failure handling
- command construction
- artifact persistence
- Illinois basin regression runs
- low-relief Illinois basin runs
- seed-project regeneration verification on Windows

## Open Constraints

- TauDEM is an external dependency and must be installed outside Python packaging.
- `ras-agent` still carries legacy `hdf5_direct` and `template_clone` compatibility paths; these should be retired or quarantined after the geometry-first path is fully reconciled upstream.
- The bundled project scaffold still needs to be matured into at least one usable 2D seed project and, if needed, a separate 1D seed project.
- The main missing `ras-commander` feature appears to be a first-class writer for watershed-derived 2D flow area perimeter geometry in `.g##`.
- Real benchmark fixtures are not yet committed.
- R-based comparison tooling is intentionally out of the runtime dependency chain.
- The current upstream TauDEM-to-HMS Spring Creek benchmark is import-valid and compute-valid. It should remain runnable for the Spring Creek headwater pilot, but it is not generalized production hydrology until the readiness gate, TauDEM parameter-tuning support, and human reviewer QAQC bundle are in place.
