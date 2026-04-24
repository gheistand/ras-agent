# Glenn Review Menu - Cross-Repository QAQC Package

Prepared: 2026-04-24

Operator context: Bill/CLB is an external contributor. Treat this as a menu of
reviewable `ras-agent` integration slices, not a request to inspect or merge
Bill's local sibling-repo working trees. The useful `hms-commander` and
`ras-commander` support has been pushed to those repos' `main` branches and is
available through their latest pip packages. The only held-back feature branch
for Glenn to reconcile is this `ras-agent` branch.

## Current Git State

### ras-agent

- Working branch: `commander-coordination-branch`
- Status: tracked coordination changes are committed and pushed; generated and
  scratch artifacts remain deliberately excluded.
- Integration risk: local branch is materially behind Glenn's current upstream
  `main`; this should be rebased or selectively ported before any PR.
- QAQC fix added during review: headwater mode now preserves normal-depth-only
  fallback behavior when no stream/perimeter intersections are found.

### Shared package dependencies

- `hms-commander`: consume from the latest pip package on `main`; do not ask
  Glenn to review Bill's local `hms-commander` working tree as part of this PR.
- `ras-commander`: consume from the latest pip package on `main`; do not ask
  Glenn to review Bill's local `ras-commander` working tree as part of this PR.
- Local sibling-repo QA notes below are retained only as dependency provenance
  and risk context for the `ras-agent` branch.

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

- Treat Spring Creek as the immediate headwater pilot for BLE-style data
  generation and gauge-based calibration/validation.
- Keep downstream/model-chaining support out of scope until Spring Creek
  headwater calibration/validation is working.
- Implement the simpler rain-on-grid setup first via `ras-commander` AORC/MRMS
  support, while HMS modeling continues in parallel through `hms-commander`.
- After the HMS modeling path is complete enough to trust, build out the
  parallel HMS-linked boundary-construction workflow.
- Use the commander libraries' spatial-linking and upstream-area accounting
  support so HMS basin areas are not double counted when assigning boundary
  conditions.
- Integrate with Glenn's current plan-HDF/preprocessor direction before real
  batch execution.
- Use the final/smoothed 2D flow-area polygon consistently for both perimeter
  writing and BC placement.
- Keep Spring Creek-specific helpers clearly labeled or parameterized.

### hms-commander package dependency

- Added direct TauDEM CLI workflow support with deterministic manifests, status
  reports, expected-output checks, and test wrappers.
- Added gauge-study, gauge-data, terrain, watershed-verification,
  TauDEM-to-HMS basin-builder, and HMS roundtrip validation helpers.
- Added Atlas 14 PFDS/frequency-storm helpers and benchmark workflow docs.
- Added Spring Creek-oriented fixtures and tests for deterministic TauDEM/HMS
  assembly behavior.

Review notes before merge:

- Treat these capabilities as packaged dependencies already available from
  `hms-commander` main/latest pip, not as sibling-repo branch changes to review
  in this PR.
- Keep generated HMS models documented as benchmark scaffolds, not production
  hydrology.

### ras-commander package dependency

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

- Treat these capabilities as packaged dependencies already available from
  `ras-commander` main/latest pip, not as sibling-repo branch changes to review
  in this PR.
- Treat land-cover/soils/infiltration template defaults as starter mappings
  pending domain signoff.

## ras-agent Severity-Ranked QAQC Findings

1. HIGH - Spring Creek headwater pilot needs explicit water-source selection and
   validation. This is not a reason to defer the pilot. The branch should support
   Glenn reproducing the Spring Creek BLE-style run, then validating/calibrating
   against the gauge. Implement the simpler rain-on-grid path first via
   `ras-commander` AORC/MRMS support, while HMS modeling continues in parallel
   through `hms-commander`. After the HMS modeling path is complete enough to
   trust, build out the parallel HMS-linked boundary-construction workflow. The
   validation task is proving generated plan/met/BC artifacts are wired and
   nonzero before calibration.

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
   preprocessing, cloud-native exports, Cartesian mesh experiments, and SLURM
   support. Treat this as a selective porting effort: keep the geometry-first,
   RASMapper-aligned mesh path, and mine upstream Cartesian work only for
   compatible QA ideas or implementation details.

6. MEDIUM - The `hms-commander` TauDEM-to-HMS package output that `ras-agent`
   may consume uses heuristic hydrology and routing parameters. Keep the
   benchmark warning language inside `ras-agent` until the HMS path is complete
   enough to trust.

7. MEDIUM - `ras-agent` context/report helpers still contain Spring Creek
   assumptions. Label as Spring Creek-only or parameterize gauge/site IDs.

8. INFO - `ras-agent` should depend on latest published `hms-commander` and
   `ras-commander` packages for commander functionality, not local sibling-repo
   working trees.

## Proposed ras-agent Commit Grouping

1. Docs and roadmap alignment
   - `README.md`
   - `docs/KNOWLEDGE.md`
   - `agent_tasks/plans/illinois-taudem-primary.md`
   - `pipeline/requirements.txt`

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

4. Geometry-first builder/seed scaffold work
   - `pipeline/model_builder.py`
   - `pipeline/terrain.py`
   - `data/RAS_6.6_Template/TEMPLATE.rasmap`
   - related geometry-first tests

5. Benchmark artifacts
   - `benchmarks/compare_meshes.py`
   - `agent_tasks/mesh_comparison_report.md`
   - stage separately from runtime code

## Explicit Exclude List

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

## Validation Completed

### ras-agent

- `python -m compileall pipeline tests` passed.
- Focused pytest suite passed: `95 passed`.
- `git diff --check` is clean after fixing one trailing-whitespace line.

### Package availability

- Latest PyPI package checks on 2026-04-24 report `ras-commander==0.95.0` and
  `hms-commander==0.3.0`.
- `pipeline/requirements.txt` now requires those latest package floors so
  `ras-agent` consumes published commander functionality instead of relying on
  local sibling-repo branches.

## Draft Pull Request Text

Title:

Propose ras-agent Illinois-first TauDEM and geometry-first integration slices

Summary:

This contribution set is intended as a review menu for upstream integration,
not a single all-or-nothing merge. It preserves the Illinois-first direction for
`ras-agent` while consuming reusable TauDEM/HMS primitives from the latest
`hms-commander` package and reusable HEC-RAS geometry/mesh/RasMap primitives
from the latest `ras-commander` package.

The main proposal is to let `ras-agent` compose shared library capabilities
rather than owning long-term reusable implementations. The work includes direct
TauDEM watershed-processing support, Atlas 14 HMS benchmark scaffolding,
text-first HEC-RAS geometry/mesh helpers, boundary-condition mode scaffolding,
and Spring Creek seed-workspace integration tests. Spring Creek is intentionally
the first identified headwater HUC with gauge history; downstream model chaining
should follow after that headwater pilot is generated, calibrated, and
validated.

Review options:

1. Accept low-risk documentation and roadmap alignment first.
2. Confirm `ras-agent` should depend on latest `hms-commander` and
   `ras-commander` pip packages for shared functionality.
3. Rebase/port `ras-agent` geometry-first orchestration after upstream
   preprocessor/plan-HDF changes are reconciled.
4. Use Spring Creek as the first headwater pilot to reproduce BLE-style data
   generation, then let Glenn decide how to improve and extend the
   implementation.

Known blockers before merge-ready `ras-agent` runtime support:

- Spring Creek pilot needs validated headwater source wiring: rain-on-grid via
  AORC/MRMS first, followed by HMS-linked boundary construction once the
  parallel HMS modeling path is complete enough to trust. Gauge
  calibration/validation is the acceptance loop.
- Real batch execution needs plan-HDF/preprocessor generation reconciled with
  current upstream direction.
- Geometry and BC placement should use the same final basin/perimeter polygon.
- Local `ras-agent` branch is behind upstream work and should be ported rather
  than pushed as-is.

Suggested issue links to create or reference:

- `hms-commander`: production-readiness gate for TauDEM-to-HMS scaffold
  parameters.
- `ras-commander`: authoritative geometry-first, RASMapper-aligned
  mesh/preprocessor workflow consumed by `ras-agent`.
- `ras-agent`: Spring Creek headwater pilot source wiring, gauge validation, and
  plan-HDF regeneration gate.
