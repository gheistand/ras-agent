# Illinois TauDEM Primary

Status: active
Last updated: 2026-04-16

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
- `template_clone` remains supported only as a legacy fallback.
- `data/RAS_6.6_Template/` is the current bundled starter project scaffold and should be matured into explicit 1D and 2D seed templates.
- `rivnet` / `traudem` are reference-only validation tools.
- WhiteboxTools belongs in a separate benchmark worktree and must not drive the mainline API.
- Non-Illinois regional assumptions are not runtime defaults for this repo.

## Completed

- Added `pipeline/taudem.py` as the local TauDEM wrapper used by the Illinois adaptation layer.
- Replaced the old watershed implementation with a TauDEM-backed workflow in `pipeline/watershed.py`.
- Extended `WatershedResult` to include `subbasins`, `centerlines`, `breaklines`, and `artifacts`.
- Verified that `ras-commander` already covers most of the HEC-RAS-side workflow needed by `ras-agent`.
- Updated repo docs to keep the roadmap and architecture scoped to this repo.

## Next

1. Separate active work by generalizability so reusable methods are upstreamed to `hms-commander` or `ras-commander` instead of accreting in `ras-agent`.
2. For each blocking sibling-repo feature gap, file or reference a GitHub issue in the target repo and record the issue link in this plan.
3. Replace the `hdf5_direct` architectural framing with a geometry-first `ras-commander` workflow in docs and code.
4. Retire or rename the `mesh_strategy='hdf5_direct'` contract once the geometry-first builder exists so the public API no longer implies HDF-first authoring.
5. Mature `data/RAS_6.6_Template/` into at least one usable 2D seed template and, if needed, a separate 1D seed template with real geometry, flow, and plan files.
6. Implement a first-class `ras-commander` writer for watershed-derived 2D flow area perimeter geometry in `.g##`.
7. Wire watershed outputs into `ras-commander` geometry, land-cover, and infiltration compilation workflows.
8. Push reusable TauDEM example and preprocessing patterns toward `hms-commander` while keeping Illinois-specific adaptation in `ras-agent`.
9. Validate direct TauDEM runs on at least two Illinois basins, including one low-relief case.
10. Capture Windows regeneration and QA steps for geometry recompilation and compiled HDF artifacts.
11. Add benchmark fixtures and written comparison outputs for direct TauDEM vs reference tracks.
12. Define acceptance tolerances for snapped outlet location, subbasin area, stream network structure, and derived channel metrics.

## Current Upstream Issue Links

- `hms-commander` gauge-first watershed study builder: https://github.com/gpt-cmdr/hms-commander/issues/2
- `hms-commander` workspace organizer and manifest builder: https://github.com/gpt-cmdr/hms-commander/issues/3
- `hms-commander` hydrology-side report and data-gap generator: https://github.com/gpt-cmdr/hms-commander/issues/4
- `hms-commander` TauDEM example-workflow/input-pack support: https://github.com/gpt-cmdr/hms-commander/issues/5
- `ras-commander` basin-first USGS study package: https://github.com/gpt-cmdr/ras-commander/issues/35
- `ras-commander` drainage-area comparison utility: https://github.com/gpt-cmdr/ras-commander/issues/36
- `ras-commander` model-side report and data-gap generator: https://github.com/gpt-cmdr/ras-commander/issues/37
- `ras-commander` geometry-first 2D flow area writer: https://github.com/gpt-cmdr/ras-commander/issues/38
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
