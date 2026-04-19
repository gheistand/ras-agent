# Plan: Integrate Geometry-First Mesh Strategy into ras-agent

## Context

ras-commander Issue #38 is complete: `GeomStorage.set_2d_flow_area_perimeter()` and `set_2d_flow_area_settings()` are validated through two rounds of Codex QAQC and a HEC-RAS 6.6 preprocessor smoke test. Four commits sit on `feat/ras-calibrate` (1758c2c5..78998b88) awaiting a clean PR.

The current `hdf5_direct` strategy in `pipeline/model_builder.py` manually writes `.g01` and seeds `.g01.hdf` — this was always marked as scaffolding. Now that ras-commander has a proper geometry writer, we replace it with a `geometry_first` strategy that:
1. Copies the RAS_6.6_Template scaffold
2. Creates `.g01`, `.p01`, `.u01` files with proper HEC-RAS formatting
3. Calls `GeomStorage.set_2d_flow_area_perimeter()` to write the watershed perimeter
4. Lets HEC-RAS regenerate all HDF artifacts via `compute_plan(force_geompre=True)`

---

## Phase A: ras-commander Branch Cleanup & PR

**Goal**: Get Issue #38 merged into ras-commander main.

1. In `G:\GH\ras-commander`, create branch `feat/issue-38-2d-writer` from `main`
2. Cherry-pick the 4 commits: `1758c2c5`, `153141ff`, `668e96c2`, `78998b88`
3. Resolve any conflicts (these commits only touch `GeomStorage.py` + 2 test files)
4. Push and open PR against `main` with title: `feat(GeomStorage): 2D flow area perimeter writer (#38)`
5. Run `pytest tests/test_geom_storage_2d_flow_area_writer.py` to confirm clean

**Files touched** (ras-commander):
- `ras_commander/geom/GeomStorage.py`
- `tests/test_geom_storage_2d_flow_area_writer.py`
- `tests/test_geom_storage_2d_preprocess_smoke.py`

**Blocker**: ~120 other unstaged files on `feat/ras-calibrate` — cherry-pick avoids that mess.

---

## Phase B: Implement `geometry_first` Strategy in model_builder.py

**Goal**: Add 4 new functions + wire into `build_model()` dispatch.

### B1: `_scaffold_project_from_template()`
- Copy `data/RAS_6.6_Template/` to `output_dir/{project_name}/`
- Rename `TEMPLATE.prj` -> `{project_name}.prj`, update `Proj Title=` line
- Rename `TEMPLATE.rasmap` -> `{project_name}.rasmap`, update project references
- Return `(project_dir, prj_file, rasmap_file)`

**Key reuse**: The existing `data/RAS_6.6_Template/TEMPLATE.prj` and `TEMPLATE.rasmap` are the scaffold. No new template files needed — the geometry writer creates `.g01` from scratch.

### B2: `_register_files_in_prj()`
- Append `Geom File=g01`, `Unsteady File=u01`, `Plan File=p01` lines to `.prj`
- Pattern: match existing `_write_project_file()` at line ~600 for formatting

### B3: `_write_geometry_first_geom_file()`
- Create `.g01` with header (`Geom Title=`, `Program Version=6.60`)
- Call `GeomStorage.set_2d_flow_area_perimeter(geom_file, area_name, coordinates=perimeter_coords, recompute_centroid=True)`
- Call `GeomStorage.set_2d_flow_area_settings(geom_file, area_name, mannings_n=mannings_n, point_generation_data=[None, None, cell_size, cell_size])`
- **Replaces**: `_write_perimeter_to_geometry_file()` (line 487) and `_write_geometry_seed_file()` — both use fragile regex/manual formatting

### B4: `_build_geometry_first()`
- Full builder function, same signature as `_build_hdf5_direct()`
- Steps:
  1. `_scaffold_project_from_template()` — copy template, rename files
  2. `_write_geometry_first_geom_file()` — create `.g01` via ras-commander
  3. Write `.u01` (unsteady flow) — reuse existing `_write_unsteady_flow_file()`
  4. Write `.p01` (plan) — reuse existing `_write_plan_file()`
  5. `_register_files_in_prj()` — update `.prj` references
  6. Return `HecRasProject` with `mesh_strategy="geometry_first"`
- **Does NOT**: seed `.g01.hdf` (HEC-RAS regenerates it), call `compute_plan()` (that's the runner's job)

### B5: Wire into `build_model()` dispatch (line 1169)
- Add `elif mesh_strategy == "geometry_first":` block
- Add `"geometry_first"` to docstring's strategy list
- Update `HecRasProject.mesh_strategy` type hint/docstring

### B6: Tests (6 new, in `tests/test_model_builder.py`)
1. `test_scaffold_project_from_template` — copies template, renames correctly
2. `test_register_files_in_prj` — appends Geom/Plan/Flow lines
3. `test_write_geometry_first_geom_file` — `.g01` contains `Storage Area=` block with correct perimeter
4. `test_build_geometry_first_creates_complete_project` — all files exist, `.prj` references correct
5. `test_build_model_dispatches_geometry_first` — `build_model(mesh_strategy="geometry_first")` calls the right builder
6. `test_geometry_first_perimeter_matches_watershed` — round-trip: write perimeter, parse back, compare coordinates

**Files modified** (ras-agent):
- `pipeline/model_builder.py` — add B1-B5
- `tests/test_model_builder.py` — add B6
- `pipeline/requirements.txt` — ensure `ras-commander` is listed (already is as editable install)

---

## Phase C: Update Orchestrator & Batch Defaults

**Goal**: Make `geometry_first` the default, keep `hdf5_direct` as fallback.

1. `pipeline/orchestrator.py` line 265: change `mesh_strategy: str = "hdf5_direct"` -> `"geometry_first"`
2. `pipeline/batch.py`: update any hardcoded strategy references
3. Update docstrings to list `geometry_first` as the primary strategy
4. Keep `hdf5_direct` working (no deletion) — it's the fallback for environments without ras-commander

**Files modified**:
- `pipeline/orchestrator.py`
- `pipeline/batch.py`

---

## Phase D: Spring Creek End-to-End Validation — DONE (2026-04-19)

**Result**: Geometry-first `.g01` from cached Spring Creek basin (103 mi², 137 vertices) preprocessed successfully in HEC-RAS 6.6. Geometry HDF created (24KB). Full orchestrator pipeline blocked on TauDEM not being installed.

**What was validated**:
1. `build_model(mesh_strategy="geometry_first")` with real NLDI basin polygon
2. `.g01` created with valid `Storage Area=` block via GeomStorage
3. `RasPreprocess.preprocess_plan("01")` — geometry HDF generated in ~20 seconds
4. Full simulation blocked (no terrain attached to scaffold project) — separate scope

**What remains for full end-to-end**:
- Install TauDEM to run full orchestrator pipeline
- Attach terrain to geometry-first projects (terrain registration in `.rasmap`)

---

## Phase E: TauDEM Multi-Basin Validation

**Goal**: Validate on 2+ Illinois basins, including a low-relief case.

1. Select second basin (low-relief, different drainage area)
2. Run full pipeline with `geometry_first`
3. Compare watershed delineation quality, mesh generation, HEC-RAS preprocessing
4. Document any basin-specific issues

---

## Verification

After each phase:
- **Phase A**: `pytest tests/test_geom_storage_2d_flow_area_writer.py` passes in ras-commander
- **Phase B**: `pytest tests/test_model_builder.py` passes in ras-agent
- **Phase C**: `pytest tests/test_orchestrator.py` passes with new default
- **Phase D**: Full pipeline run produces valid HEC-RAS output, report generated
- **Phase E**: Multiple basins complete successfully

---

## Execution Order

Phase A is independent and can start immediately. Phase B depends on ras-commander being importable with Issue #38 code (already true via editable install from `feat/ras-calibrate`). Phases C-E are sequential.

**Start with Phase B** — the editable install already has the Issue #38 code. Phase A (PR) can happen in parallel or after.
