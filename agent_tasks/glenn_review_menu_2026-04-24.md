# Glenn Review Menu - Cross-Repository QAQC Package

Prepared: 2026-04-24

Operator context: Bill/CLB is an external contributor. Treat this as a menu of
reviewable integration slices, not a request to merge the current working trees
as-is. The intent is to preserve Glenn's upstream direction while offering
portable work from `ras-agent`, `hms-commander`, and `ras-commander`.

## Current Git State

### ras-agent

- Working branch: `feat/bc-lines-mesh`
- Status: dirty working tree with source/doc/test edits plus many generated or
  scratch artifacts.
- Integration risk: local branch is materially behind Glenn's current upstream
  `main`; this should be rebased or selectively ported before any PR.
- QAQC fix added during review: headwater mode now preserves normal-depth-only
  fallback behavior when no stream/perimeter intersections are found.

### hms-commander

- Working branch: `main`
- Status: dirty working tree, no local commit created in this pass.
- Main contribution: reusable TauDEM, gauge-study, terrain, watershed
  verification, HMS basin scaffold, Atlas 14, and roundtrip validation helpers.
- Scope note: generated HMS projects should be framed as benchmark/import-valid
  scaffolds until hydrologic parameters receive domain review.

### ras-commander

- Working branch: `main`
- Status: one local commit ahead of `origin/main`:
  `81439b0b Release 0.95.0 mesh and land classification updates`
- Main contribution: reusable HEC-RAS mesh/geometry, RasMap, land-classification,
  terrain, HDF reader, GDAL/RasMapper runtime, and packaging updates.
- QAQC fix added during review: GDAL/RasMapper bootstrap now verifies the
  `python.exe` sibling `GDAL` bridge before loading `RasMapperLib.dll`.

## Changelog Draft

### ras-agent

- Added Illinois-first geometry-first model-building scaffolding that composes
  reusable HEC-RAS geometry and mesh primitives from `ras-commander`.
- Added boundary-condition mode normalization with explicit fail-fast behavior
  for downstream/chained-basin support.
- Added headwater BC-line generation tests and geometry-first model-builder
  fixtures.
- Fixed no-intersection BC fallback so `headwater=True` cannot silently create
  an upstream flow hydrograph.
- Added Spring Creek seed-workspace context-layer refresh/report helpers.
- Updated roadmap and knowledge docs to reflect direct TauDEM CLI processing,
  text-first HEC-RAS geometry, and shared-library ownership boundaries.

Review notes before merge:

- Resolve the headwater water-source contract before presenting generated
  geometry-first runs as operational.
- Integrate with Glenn's current plan-HDF/preprocessor direction before real
  batch execution.
- Use the final/smoothed 2D flow-area polygon consistently for both perimeter
  writing and BC placement.
- Keep Spring Creek-specific helpers clearly labeled or parameterized.

### hms-commander

- Added direct TauDEM CLI workflow support with deterministic manifests, status
  reports, expected-output checks, and test wrappers.
- Added gauge-study, gauge-data, terrain, watershed-verification,
  TauDEM-to-HMS basin-builder, and HMS roundtrip validation helpers.
- Added Atlas 14 PFDS/frequency-storm helpers and benchmark workflow docs.
- Added Spring Creek-oriented fixtures and tests for deterministic TauDEM/HMS
  assembly behavior.

Review notes before merge:

- Keep generated HMS models documented as benchmark scaffolds, not production
  hydrology.
- Document the `Atlas14Storm.generate_hyetograph_from_ari` return-shape/API
  change.
- Exclude transient package/install artifacts and oversized generated fixtures
  not required for tests.

### ras-commander

- Added text-first HEC-RAS geometry mesh/breakline support that treats `.g##`
  text geometry as authoritative.
- Added geometry storage helpers for breaklines, mesh spacing, and related 2D
  flow-area content.
- Added RasMap land-cover, soils, infiltration, and geometry association helpers
  with packaged template HDF resources.
- Added shared GDAL/RasMapper bootstrap that configures HEC-RAS GDAL paths and
  verifies the legacy `python.exe/GDAL` bridge before `RasMapperLib.dll` loads.
- Improved terrain creation timeout handling, USGS 3DEP resolution validation,
  HDF pipe/inlet readers, and cross-section result access.

Review notes before merge:

- Stage mesh, RasMap/land-classification, HDF reader, terrain, GDAL runtime, and
  packaging changes separately if Glenn wants smaller review slices.
- Clear executed notebook outputs and local paths before including notebook
  updates.
- Treat land-cover/soils/infiltration template defaults as starter mappings
  pending domain signoff.

## Severity-Ranked QAQC Findings

1. HIGH - `ras-agent` headwater water-source contract is unresolved.
   Geometry-first headwater runs can produce BC files without a flow hydrograph;
   this is only valid if rain-on-grid/met/infiltration/plan wiring is implemented
   and validated.

2. HIGH - `ras-agent` real execution still depends on `.p##.hdf` plan files that
   the builder names but does not generate. Port this onto Glenn's current
   preprocessor/plan-HDF direction before operational runtime claims.

3. FIXED - `ras-agent` no-intersection fallback ignored `headwater=True` and
   created `USInflow1`. The fallback now routes headwater mode through the
   normal-depth headwater BC generator and keeps inflow fallback only for
   `headwater=False`.

4. MEDIUM - `ras-agent` can write the 2D flow-area perimeter from a smoothed
   polygon while placing BCs against the original basin polygon. Use one final
   validated polygon for both.

5. MEDIUM - `ras-agent` is behind Glenn's upstream work around native
   preprocessing, cloud-native exports, Cartesian mesh, and SLURM support. Treat
   this as a selective porting effort.

6. MEDIUM - `hms-commander` TauDEM-to-HMS basin output uses heuristic hydrology
   and routing parameters. Keep the benchmark warning language.

7. MEDIUM - `ras-agent` context/report helpers still contain Spring Creek
   assumptions. Label as Spring Creek-only or parameterize gauge/site IDs.

8. LOW - `hms-commander` Atlas 14 return shape appears behavior-changing and
   needs a release-note/migration note.

9. LOW - `ras-commander` StoreAllMaps relocation now captures modified
   pre-existing outputs. Keep if intentional, but document as a behavior change.

10. INFO - `ras-commander` GDAL/RasMapper modal has been addressed and verified
   locally.

## Proposed Commit Grouping

### ras-agent

1. Docs and roadmap alignment
   - `README.md`
   - `docs/KNOWLEDGE.md`
   - `agent_tasks/plans/illinois-taudem-primary.md`

2. Boundary-condition mode scaffold and fallback fix
   - `pipeline/bc_lines.py`
   - `pipeline/model_builder.py`
   - `pipeline/orchestrator.py`
   - `pipeline/batch.py`
   - related tests

3. Workspace/context/report helpers
   - `pipeline/context_layers.py`
   - `pipeline/workspace.py`
   - `pipeline/report.py`
   - related tests

4. Geometry-first builder/template work
   - `pipeline/model_builder.py`
   - `pipeline/terrain.py`
   - `data/RAS_6.6_Template/TEMPLATE.rasmap`
   - related geometry-first tests

5. Benchmark artifacts
   - `benchmarks/compare_meshes.py`
   - `agent_tasks/mesh_comparison_report.md`
   - stage separately from runtime code

### hms-commander

1. Gauge-study workspace and terrain/input-pack primitives
2. Direct TauDEM CLI runner
3. Watershed verification and boundary handoff
4. TauDEM-to-HMS basin assembly/bootstrap
5. Atlas 14 PFDS/frequency helpers
6. HMS roundtrip validator
7. API docs, examples, and deterministic fixtures

### ras-commander

1. GDAL/RasMapper runtime bootstrap fix
2. Text-first mesh and breakline/refinement support
3. RasMap land-cover/soils/infiltration helpers
4. HDF reader improvements
5. Terrain and USGS 3DEP improvements
6. Packaging/version cleanup
7. Example notebooks only after output/local-path cleanup

## Explicit Exclude List

### ras-agent

- `=3.0.5`
- `TASK.md`, `OUTPUT.md`
- `.tmp_pytest*`, `pytest_cache/`, `pytest_tmp*/`, `pytest-cache-files-*`
- `working/`
- `py_write_probe_dir/`
- `example_projects/`
- `data/RAS_6.6_Template/*.backup`
- `data/RAS_7.0_Template/` unless deliberately size-reviewed as a fixture
- ad hoc `agent_tasks/session_state_*.md` unless intentionally part of the
  public handoff

### hms-commander

- `0.24.0`
- transient build, site, dist, egg-info, cache, and generated local outputs
- notebook outputs/local machine paths unless intentionally cleaned

### ras-commander

- `.claude/scheduled_tasks.lock`
- `.tmp/`, `.pytest-tmp/`
- `TASK.md`, `OUTPUT.md`
- `agent_tasks/session_state_*.md`
- `example_data/`
- `examples/out/`
- generated probe scripts and one-off output text files
- executed notebook copies and large generated outputs

## Validation Completed

### ras-agent

- `python -m compileall pipeline tests` passed.
- Focused pytest suite passed: `95 passed`.
- `git diff --check` is clean after fixing one trailing-whitespace line.

### hms-commander

- Focused pytest suite previously passed: `66 passed` using global Python 3.14
  because the repo venv lacked pytest.
- `git diff --check` is clean.

### ras-commander

- GDAL/RasMapper focused tests passed: `11 passed`.
- Broader targeted suite passed: `86 passed, 1 skipped`.
- Real smoke test passed: `RasTerrainMod._ensure_initialized()` loaded
  `RasMapperLib.dll` from HEC-RAS 7.0.
- Exact missing junction from the user dialog was created and verified:
  `C:\Users\bill\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\GDAL`
  points to `C:\Program Files (x86)\HEC\HEC-RAS\7.0\GDAL`.
- `git diff --check origin/main...HEAD` is clean.

## Draft Pull Request Text

Title:

Propose Illinois-first TauDEM and geometry-first integration slices for review

Summary:

This contribution set is intended as a review menu for upstream integration,
not a single all-or-nothing merge. It preserves the Illinois-first direction for
`ras-agent`, moves reusable TauDEM/HMS primitives toward `hms-commander`, and
moves reusable HEC-RAS geometry/mesh/RasMap primitives toward `ras-commander`.

The main proposal is to let `ras-agent` compose shared library capabilities
rather than owning long-term reusable implementations. The work includes direct
TauDEM watershed-processing support, Atlas 14 HMS benchmark scaffolding,
text-first HEC-RAS geometry/mesh helpers, boundary-condition mode scaffolding,
and Spring Creek seed-workspace integration tests.

Review options:

1. Accept low-risk documentation and roadmap alignment first.
2. Review `hms-commander` TauDEM/HMS benchmark primitives independently.
3. Review `ras-commander` text-first mesh, RasMap, and GDAL runtime helpers
   independently.
4. Rebase/port `ras-agent` geometry-first orchestration after upstream
   preprocessor/plan-HDF changes are reconciled.
5. Defer operational headwater hydraulic runs until the water-source contract is
   resolved and validated.

Known blockers before merge-ready `ras-agent` runtime support:

- Geometry-first headwater path needs an explicit water-source contract.
- Real batch execution needs plan-HDF/preprocessor generation reconciled with
  current upstream direction.
- Geometry and BC placement should use the same final basin/perimeter polygon.
- Local `ras-agent` branch is behind upstream work and should be ported rather
  than pushed as-is.

Suggested issue links to create or reference:

- `hms-commander`: production-readiness gate for TauDEM-to-HMS scaffold
  parameters.
- `ras-commander`: authoritative mesh/preprocessor workflow consumed by
  `ras-agent`.
- `ras-agent`: headwater water-source contract and plan-HDF regeneration gate.
