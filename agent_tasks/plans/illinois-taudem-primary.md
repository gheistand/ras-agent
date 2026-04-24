# Illinois TauDEM Primary

Status: active
Last updated: 2026-04-24

## Objective

Make `ras-agent` the active Illinois adaptation and integration repo for TauDEM-backed watershed delineation and a `ras-commander`-driven geometry-first HEC-RAS build workflow.

## Accepted Decisions

- TauDEM-backed watershed processing is the delineation basis for this repo's Illinois workflows.
- Default processing CRS remains `EPSG:5070`.
- `ras-commander` is the primary interface for HEC-RAS projects and compiled workflows.
- Reusable TauDEM and hydrology-support methods should land in `hms-commander`, not stay in `ras-agent`.
- Reusable HEC-RAS project/geometry/execution methods should land in `ras-commander`, not stay in `ras-agent`.
- Requests for those reusable sibling-repo features should be filed as GitHub issues in the target repo and linked from this plan.
- Plain-text geometry is authoritative for geometry-backed project content.
- `template_clone` is a legacy compatibility path, not a target architecture or
  recommended fallback.
- `data/RAS_6.6_Template/` is the current bundled starter project scaffold and should be matured into explicit 1D and 2D geometry-first seed projects.
- `rivnet` / `traudem` are reference-only validation tools.
- WhiteboxTools belongs in a separate benchmark worktree and must not drive the mainline API.
- Non-Illinois regional assumptions are not runtime defaults for this repo.

## Completed

- Added `pipeline/taudem.py` as the local TauDEM wrapper used by the Illinois adaptation layer.
- Replaced the old watershed implementation with a TauDEM-backed workflow in `pipeline/watershed.py`.
- Extended `WatershedResult` to include `subbasins`, `centerlines`, `breaklines`, and `artifacts`.
- Verified that `ras-commander` already covers most of the HEC-RAS-side workflow needed by `ras-agent`.
- Updated repo docs to keep the roadmap and architecture scoped to this repo.
- Upstream `hms-commander` now provides the reusable Spring Creek study/workspace package, direct TauDEM execution surface, watershed verification, boundary handoff outlet selection, TauDEM-to-HMS assembly/export, parser-of-record round-trip validation, and an end-to-end Atlas 14 benchmark notebook plus live compute example.
- The shared Spring Creek benchmark is now import-valid and compute-valid from the `hms-commander` side, with durable artifacts available for downstream comparison and handoff testing.
- Spring Creek is the first headwater pilot HUC with gauge history. Keep it
  runnable for BLE-style data generation and use it as the calibration/validation
  proving ground before expanding to downstream or chained-basin models.
- `ras-agent` now treats `hms-commander` and `ras-commander` as latest-pip
  dependencies; local sibling-repo branches are not part of this integration
  branch.

## Next

1. Separate active work by generalizability so reusable methods are upstreamed to `hms-commander` or `ras-commander` instead of accreting in `ras-agent`.
2. For each blocking sibling-repo feature gap, file or reference a GitHub issue in the target repo and record the issue link in this plan.
3. ~~Replace the `hdf5_direct` architectural framing with a geometry-first `ras-commander` workflow in docs and code.~~ DONE — `geometry_first` is the default strategy in orchestrator.py and batch.py (2026-04-19).
4. ~~Retire or rename the `mesh_strategy='hdf5_direct'` contract once the geometry-first builder exists so the public API no longer implies HDF-first authoring.~~ DONE — `geometry_first` is the default; `hdf5_direct` remains only as a legacy compatibility path pending retirement (2026-04-19).
5. Mature `data/RAS_6.6_Template/` into at least one usable 2D seed project and, if needed, a separate 1D seed project with real geometry, flow, and plan files.
6. ~~Implement a first-class `ras-commander` writer for watershed-derived 2D flow area perimeter geometry in `.g##`.~~ DONE — ras-commander Issue #38 complete, integrated via `_write_geometry_first_geom_file()` (2026-04-19).
7. Wire watershed outputs into `ras-commander` geometry, land-cover, and infiltration compilation workflows.
8. Push reusable TauDEM example and preprocessing patterns toward `hms-commander` while keeping Illinois-specific adaptation in `ras-agent`.
9. Validate direct TauDEM runs on at least two Illinois basins, including one low-relief case.
10. Capture Windows regeneration and QA steps for geometry recompilation and compiled HDF artifacts.
11. Add benchmark fixtures and written comparison outputs for direct TauDEM vs reference tracks.
12. Define acceptance tolerances for snapped outlet location, subbasin area, stream network structure, and derived channel metrics.
13. Use Spring Creek as the first headwater pilot to reproduce BLE-style data
    generation, then calibrate/validate against the gauge before promoting the
    workflow.
14. Implement the simpler rain-on-grid setup first via `ras-commander`
    AORC/MRMS support, while HMS modeling continues in parallel through
    `hms-commander`.
15. After the HMS modeling path is complete enough to trust, build out the
    parallel HMS-linked boundary-construction workflow.
16. Consume commander spatial-linking and upstream-area accounting support so
    HMS basin area is not double counted when boundary conditions are assigned.
17. Treat the current Spring Creek TauDEM-to-HMS output from `hms-commander` as
    benchmark-grade, not generalized production hydrology, until the upstream
    pre-HMS readiness gate and human-review QAQC signoff artifact exist. This
    does not defer the Spring Creek pilot; it defines the review status of the
    hydrology scaffold.
18. Validate downstream `ras-agent` regeneration against the live Spring Creek
    handoff package emitted by `hms-commander`, rather than rebuilding the
    hydrology-side context locally.
19. Track and consume upstream TauDEM parameter sensitivity / optimization
    support so delineation controls can be tuned deliberately before downstream
    model promotion.
20. Record downstream acceptance rules for the first live HMS warning classes
    now observed upstream: missing ET/canopy methods, Muskingum stability
    warnings, lag-vs-time-step warnings, and negative inflow clipping.
21. After Spring Creek headwater calibration/validation is working, convert the
    `boundary_condition_mode` scaffold from headwater-only plumbing into a
    validated downstream/chained-basin workflow.

## Downstream Scaffold Notes

The codebase now exposes `boundary_condition_mode` through `pipeline/model_builder.py`, `pipeline/orchestrator.py`, and `pipeline/batch.py`.

Current state:

- `headwater` remains the only implemented mode.
- `downstream` is intentionally accepted at the public API/CLI layer but fails fast in the builder with a planning note.
- Spring Creek is the immediate headwater pilot. It should remain a runnable
  target for BLE-style data generation, gauge calibration/validation, and
  Glenn's review before downstream chaining is expanded.

Before enabling non-headwater basins, first validate the Spring Creek headwater
pilot, then finish at least:

1. Define the durable input contract for upstream inflow hydrographs and their provenance.
2. Decide how downstream basins discover/reference upstream model outputs versus externally supplied hydrographs.
3. Revisit non-headwater BC generation in `pipeline/bc_lines.py` and confirm the AD8 weighting/fallback path against real chained-basin fixtures.
4. Add regression coverage for builder, orchestrator, and batch paths that exercise downstream mode end-to-end.
5. Decide how downstream-mode metadata should appear in reports, run metadata, and handoff artifacts.

## Current Upstream Issue Links

- `hms-commander` gauge-first watershed study builder: https://github.com/gpt-cmdr/hms-commander/issues/2
- `hms-commander` workspace organizer and manifest builder: https://github.com/gpt-cmdr/hms-commander/issues/3
- `hms-commander` hydrology-side report and data-gap generator: https://github.com/gpt-cmdr/hms-commander/issues/4
- `hms-commander` TauDEM example-workflow/input-pack support: https://github.com/gpt-cmdr/hms-commander/issues/5
- `ras-commander` basin-first USGS study package: https://github.com/gpt-cmdr/ras-commander/issues/35
- `ras-commander` drainage-area comparison utility: https://github.com/gpt-cmdr/ras-commander/issues/36
- `ras-commander` model-side report and data-gap generator: https://github.com/gpt-cmdr/ras-commander/issues/37
- `ras-commander` geometry-first 2D flow area writer: https://github.com/gpt-cmdr/ras-commander/issues/38
- `ras-commander` headless land-cover / soils layer creation: https://github.com/gpt-cmdr/ras-commander/issues/47
- `ras-agent` note: GitHub issues are disabled in `gpt-cmdr/ras-agent`, so repo-local items stay tracked here until issue tracking is enabled.

## Reference Track

Allowed reference work:

- compare direct TauDEM results to `rivnet` / `traudem`
- document where the R-backed reference agrees or diverges

Constraints:

- no R dependency in the shipped Python runtime
- no public API built around `rivnet` / `traudem`
- direct TauDEM remains the baseline when outputs differ

## Whitebox Worktree

WhiteboxTools work is explicitly out-of-band.

Requirements:

- use a separate git worktree and branch
- do not change the mainline API from that worktree
- use the same Illinois fixtures and metrics as the TauDEM baseline
- produce a written comparison report before any architecture decision is revisited

## `ras-commander` Gap To Close

`ras-commander` appears to cover roughly 80-90% of the HEC-RAS-side needs for `ras-agent`.

The main remaining gap is a clean geometry-first builder workflow for watershed-derived projects, especially:

- inserting/updating a watershed-derived 2D flow area perimeter in `.g##`
- orchestrating geometry recompilation after edits
- standardizing the handoff between watershed outputs and `ras-commander` land-cover/infiltration workflows

Do not carry forward:

- non-Illinois regional defaults
- project-specific benchmark assumptions presented as general defaults
- Whitebox-first architecture
- placeholder topology or longest-path shortcuts
