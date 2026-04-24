# RAS Agent

Illinois-first automated 2D HEC-RAS modeling pipeline for terrain acquisition, watershed delineation, hydrology, model assembly, execution, and results export.

Built at the [Illinois State Water Survey (CHAMP Section)](https://isws.illinois.edu/champ) in collaboration with the [RAS Commander](https://github.com/gpt-cmdr/ras-commander) community.

## Current Direction

- The primary Illinois watershed-processing integration in this repo uses TauDEM-backed delineation through [`pipeline/taudem.py`](pipeline/taudem.py) and [`pipeline/watershed.py`](pipeline/watershed.py).
- The reusable hydrology-side baseline is now upstreamed substantially into `hms-commander`: Spring Creek study packaging, direct TauDEM execution, watershed verification, boundary handoff selection, TauDEM-to-HMS bootstrap, parser-of-record validation, and a live Atlas 14 compute example all exist there now.
- The intended HEC-RAS build path is `ras-commander` driven plain-text geometry assembly, with the watershed outline inserted as a 2D flow area in the `.g##` file and HEC-RAS recompiling derived HDF/preprocessor artifacts.
- `ras-agent` should consume those shared capabilities from the latest
  published `hms-commander` and `ras-commander` pip packages, not from local
  sibling-repo working trees.
- Mesh generation should stay geometry-first and RASMapper-aligned. Upstream Cartesian mesh work should be mined only for compatible QA ideas or implementation details, not preserved as an alternate runtime path.
- The current `hdf5_direct` and `template_clone` code paths are legacy compatibility surfaces pending retirement, not architecture for new work.
- The repo now includes a starter HEC-RAS 6.6 project scaffold in `data/RAS_6.6_Template/`, but it is not yet a complete 1D/2D seed-project inventory.
- That upstream TauDEM-to-HMS path should still be treated here as benchmark-grade, not production-grade, until it has a readiness gate, TauDEM parameter-tuning support, and human reviewer QAQC signoff.

## Repo Boundaries

Development should land in the repo where it is most reusable:

- Put generalizable TauDEM, watershed-processing examples, and hydrology-support workflows in `hms-commander`.
- Put generalizable HEC-RAS project editing, geometry-writing, compilation, execution, and results primitives in `ras-commander`.
- Keep `ras-agent` focused on Illinois adaptation, orchestration, product-level integration, and region-specific profiles that consume those shared tools.

If a function first appears here but is broadly reusable outside Illinois, it should be upstreamed into `hms-commander` or `ras-commander` rather than becoming a permanent `ras-agent` primitive.

Cross-repo feature-gap requests should be filed as GitHub issues in the target repository, not tracked only as local markdown handoff notes. `ras-agent` should keep local plan references to those issue links, but the actual request for reusable library work belongs in the issue tracker of `gpt-cmdr/hms-commander` or `gpt-cmdr/ras-commander`.

## Quick Start

### Python environment

```bash
cd pipeline
pip install -r requirements.txt
```

### External software for real runs

- TauDEM 5.x on `PATH` or available via an explicit executable directory
- HEC-RAS 6.6 for execution
- HEC-RAS GUI on Windows for regeneration/validation of seed projects before production use

### Mock run

```bash
python pipeline/orchestrator.py --lon -88.578 --lat 40.021 --output ./output/test --mock
```

Boundary-condition scaffold:

- `pipeline/orchestrator.py` and `pipeline/batch.py` now accept `--bc-mode headwater|downstream`.
- `headwater` remains the only implemented mode today.
- `downstream` is intentionally plumbed through the public API but currently fails fast until chained-basin hydrograph handoff, model provenance, and QA are designed.

### Real run notes

- Watershed delineation now expects TauDEM executables.
- The default CRS remains `EPSG:5070` for Illinois processing.
- HEC-RAS 6.6 treats the plain-text geometry file as the durable source of truth for geometry-backed content.
- Derived geometry HDF and preprocessor files should be regenerated through HEC-RAS and `ras-commander`, not treated as primary editable assets.
- Infiltration is the main exception: `ras-commander` documents it as HDF-backed and surviving geometry saves.
- `data/RAS_6.6_Template/` is the current starter project scaffold for geometry-first seed projects; it still needs explicit 1D/2D geometry and plan content before real-run workflows should rely on it.

## Pipeline

1. `terrain.py` acquires and mosaics DEM data, defaulting to Illinois-friendly sources and `EPSG:5070`.
2. `watershed.py` adapts TauDEM-backed delineation for Illinois runs and returns basin, streams, subbasins, centerlines, breaklines, and intermediate artifact paths.
3. `streamstats.py` gets Illinois peak-flow estimates.
4. `hydrograph.py` generates design hydrographs.
5. `model_builder.py` is being realigned so `ras-agent` hands geometry, land-cover, and soils instructions to `ras-commander` for project assembly and recompilation.
6. `runner.py` and `results.py` execute models and export outputs.
7. `api.py` and `web/` expose the orchestration and results layers.

## Base Engineering Workspace

The Spring Creek fixture is now the reference workspace pattern for base-data studies.

Expected workspace package outputs:

- `report.html`
- `report.json`
- `data_gap_analysis.json`
- `manifest.json`
- `analysis_extent.geojson`
- `analysis_extent_5070.geojson`
- `analysis_extent_summary.json`

Use [`pipeline/workspace.py`](pipeline/workspace.py) for the current command surface:

```bash
python pipeline/workspace.py validate-workspace --workspace-dir "workspace/Spring Creek Springfield IL"
python pipeline/workspace.py refresh-context-layers --workspace-dir "workspace/Spring Creek Springfield IL"
python pipeline/workspace.py build-report-package --workspace-dir "workspace/Spring Creek Springfield IL"
```

Design rules:

- The HTML report must remain self-contained with inline figures and inline MapLibre assets.
- Informational downloads should derive from one shared buffered analysis extent instead of dataset-specific buffering.
- Workspace gap analysis should point to upstream GitHub issues in `hms-commander` or `ras-commander` when the missing capability belongs there.
- `ras-agent` keeps the Illinois-specific integration layer; reusable watershed and HEC-RAS primitives belong upstream.

## Architecture

```text
terrain.py
  -> taudem.py / watershed.py
  -> streamstats.py
  -> hydrograph.py
  -> model_builder.py
  -> runner.py
  -> results.py
  -> api.py / web/
```

Key implementation decisions:

- TauDEM-backed watershed preprocessing is the current basis for Illinois runs, but reusable TauDEM workflow patterns should land in `hms-commander`.
- `ras-commander` is the main interface for HEC-RAS projects, geometry edits, execution, and compiled artifacts.
- Plain-text geometry is the authoritative source for 2D flow area outlines and Manning's-n table content.
- Infiltration and compiled per-cell products remain HDF-backed workflows.
- `rivnet` / `traudem` are comparison tools only, not runtime dependencies.
- WhiteboxTools belongs in a separate benchmark worktree, not the mainline API.
- Watershed outputs are first-class artifacts for downstream debugging and provenance.
- Boundary-condition mode is now an explicit run/build parameter, but downstream/chained-basin support is still a planned scaffold rather than a completed workflow.

## Status

- Mock-mode orchestration, API, results export, and web UI are in place.
- Illinois-first TauDEM wrapper and watershed integration are now the active hydro-processing path.
- `ras-commander` already appears to cover most of the HEC-RAS-side workflow needed by `ras-agent`.
- The remaining gap is mainly geometry-first project assembly for watershed-derived 2D flow areas, maturing the starter scaffold into usable geometry-first seed projects, tighter orchestration around land-cover and infiltration compilation, and a downstream consume/regenerate proof against the new upstream Spring Creek handoff package.
- Real-basin validation, benchmark comparisons, and Windows regeneration workflows still need completion.
- Future calibration work should add precipitation-source QAQC for rain-on-grid
  events and a reviewer-in-the-loop batched sensitivity workflow that reserves
  high-resolution parameter exploration for the most influential parameters.
- The first live upstream HMS scaffold also exposed warning classes that need downstream acceptance rules before promotion: missing ET/canopy methods, Muskingum stability warnings, lag-vs-time-step warnings, and negative inflow clipping.

## Validation

Targeted local checks:

```bash
python -m pytest tests/test_model_builder.py tests/test_orchestrator.py tests/test_taudem.py -v
python -m compileall pipeline tests
```

Broader benchmark work is tracked in [`benchmarks/README.md`](benchmarks/README.md) and the active roadmap plan in [`agent_tasks/plans/illinois-taudem-primary.md`](agent_tasks/plans/illinois-taudem-primary.md).

## Project Guidance

- Internal project context: [`docs/KNOWLEDGE.md`](docs/KNOWLEDGE.md)
- Local planning/task guidance: [`agent_tasks/README.md`](agent_tasks/README.md)
- Active roadmap: [`agent_tasks/plans/illinois-taudem-primary.md`](agent_tasks/plans/illinois-taudem-primary.md)

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
