# Plan: Match RAS Mapper Mesh Generation Exactly

## Context

Our headless mesh generation via `GeomMesh.generate()` produces visually different meshes than RAS Mapper's GUI. The user generates the reference mesh by clicking "Regenerate All Meshes with Breaklines" then "Fix All Meshes" twice. Our mesh has 87,479 cells / 191,890 faces vs the reference's 85,187 cells / 176,473 faces (+2.7% cells, +8.7% faces). The breakline refinement zones show chaotic irregular polygons instead of the clean quasi-rectangular cells that RAS Mapper produces.

ILSpy decompilation of RasMapperLib.dll reveals our code is missing critical steps from the `TryAutoFix` algorithm and doesn't replicate the `TryToFixMeshes` priority logic.

**Reference files:**
- `g02.hdf` = "Manually Regenerated" (goal): `workspace/.../ras_agent_103mi2/ras_agent_103mi2.g02.hdf`
- `g01.hdf` = our output (current): `workspace/.../ras_agent_103mi2/ras_agent_103mi2.g01.hdf`

---

## Phase 1: Fix Known Algorithm Gaps in `GeomMesh.py`

File: `G:\GH\ras-commander\ras_commander\geom\GeomMesh.py`

### 1A. Breakline seed generation (DONE)

`_generate_breakline_seeds()` was rewritten to match `EnforceBreaklines` (ILSpy `ilspy_pointgenerator.txt:1600`):
- DELETE base grid seeds within `last_offset + far_spacing * 0.75` of each breakline
- Place perpendicular offset seeds at discretized segment midpoints
- Offsets: `bl_spacing/2 + k*bl_spacing` for k=0..near_repeats

### 1B. Add duplicate seed removal function

**Why:** TryAutoFix (ilspy_meshfv2d.txt:4773-4790) ALWAYS removes duplicate seeds via `RemoveDuplicatePoints(false, _filterToleranceTIN)`. Our code never removes seeds, only adds them.

Add two new functions after line ~430:

```python
def _compute_filter_tolerance(perim) -> float:
    """Match C# GetFilterTolerance: min(0.03, extent_diag * 0.0001)."""

def _find_duplicate_seeds(seeds_pm, filter_tol: float) -> list:
    """Return indices of seeds within filter_tol of another seed (higher index removed)."""
    # Use scipy.spatial.cKDTree.query_pairs(r=filter_tol)
    # Fallback: spatial hash grid if scipy unavailable

def _remove_seeds_by_index(seeds_pm, remove_indices: list, ns: dict):
    """Return new PointMs with specified indices removed."""
```

### 1C. Remove perimeter face exclusion from `_autofix_max_faces`

**Why:** C# TryAutoFix (line 4803-4804) filters by `InternalPoints.IsNullOrEmpty()` but does NOT exclude perimeter faces. Our code erroneously adds `not mesh.FaceIsPerimeter(f)`.

Change at line ~519:
```python
# FROM:
eligible = [f for f in faces if has_no_internal_pts(f) and not mesh.FaceIsPerimeter(f)]
# TO:
eligible = [f for f in faces if has_no_internal_pts(f)]
```

Also remove the `prep_basin` containment check (lines 486-498) — C# TryAutoFix does not perform this check.

### 1D. Restructure iteration loop to match TryAutoFix + TryToFixMeshes

**Why:** C# `TryToFixMeshes` (ilspy_meshperimetereditor.txt:379-429) uses strict priority:
1. If cell changes exist (remove dups OR add midpoints) → apply ONLY cell changes
2. Else if perimeter changes exist → apply ONLY perimeter changes
3. Then rebuild mesh (`ComputeOutOfDateMesh`)

This explains why two "Fix All" clicks are needed: 1st removes dups + adds midpoints; 2nd applies perimeter dedup.

New loop structure (replaces lines 1181-1301):

```python
filter_tol = _compute_filter_tolerance(current_perim)

for iteration in range(max_iterations):
    ratio = ratios[min(ratio_idx, len(ratios) - 1)]
    mesh = _compute_mesh(current_perim, current_seeds_pm, breaklines, ratio, ns)
    state_val = int(mesh.MeshCompletionState)
    state_name = str(mesh.MeshCompletionState)
    result.iterations = iteration + 1

    # ── TryAutoFix: gather ALL three suggestion lists ────────
    dup_remove = _find_duplicate_seeds(current_seeds_pm, filter_tol)
    perim_remove = _autofix_perimeter(current_perim, ns)
    midpoints = []
    if state_val == max_faces_val or "MaxFaces" in state_name:
        seeds_list = [current_seeds_pm[i] for i in range(current_seeds_pm.Count)]
        _, _, midpoints = _autofix_max_faces(mesh, seeds_list, ns)

    has_cell_changes = len(dup_remove) > 0 or len(midpoints) > 0

    # ── Complete: return only if no remaining fixes ──────────
    if state_val == complete_val and not has_cell_changes and not perim_remove:
        # [extract cell centers, patch text, return success]
        ...

    # ── TryToFixMeshes priority logic ────────────────────────
    if has_cell_changes:
        # PRIORITY 1: cell changes (remove dups + add midpoints)
        if dup_remove:
            current_seeds_pm = _remove_seeds_by_index(current_seeds_pm, dup_remove, ns)
        for mp in midpoints:
            current_seeds_pm.Add(mp)
        continue

    elif perim_remove:
        # PRIORITY 2: perimeter changes (only if no cell changes)
        current_perim = _remove_perimeter_points(current_perim, perim_remove, ns)
        current_seeds_pm = _reseed(current_perim)
        continue

    else:
        # No TryAutoFix suggestions → fall through to existing escalation
        # (DuplicatePoints handler, PointsOutside, ratio escalation, Tier 4 DP)
        ...
```

### 1E. Modify `_autofix_max_faces` return signature

Return midpoints separately for the priority logic:

```python
def _autofix_max_faces(mesh, seeds_as_list, ns) -> Tuple[list, int, list]:
    """Returns (combined_list, n_added, new_midpoints_only)"""
```

---

## Phase 2: Build HDF Comparison Harness

File: `G:\GH\ras-agent\benchmarks\compare_meshes.py` (new)

Reads both HDF files and reports metrics. Used to validate each iteration.

### Metrics to compare:
1. **Cell count** — absolute and % difference
2. **Face count** — absolute and % difference
3. **Facepoint count** — absolute and % difference
4. **Faces-per-cell distribution** — histogram, max, cells > 8
5. **Cell area distribution** — mean, median, std, percentiles
6. **Face length distribution** — mean, median, std

### HDF paths to read:
```
Geometry/2D Flow Areas/MainArea/Cells Center Coordinate         # (N, 2)
Geometry/2D Flow Areas/MainArea/Cells Face and Orientation Info  # (N, 2) → col[1] = face count
Geometry/2D Flow Areas/MainArea/FacePoints Coordinate           # (M, 2)
Geometry/2D Flow Areas/MainArea/Faces FacePoint Indexes         # (F, 2)
Geometry/2D Flow Areas/MainArea/Faces NormalUnitVector and Length # (F, 3) → col[2] = face length
```

### Existing tools to reuse:
- `ras_commander.hdf.HdfMesh.get_mesh_area_attributes()` — cell/face counts
- `ras_commander.hdf.HdfMesh.get_mesh_cell_polygons()` — cell geometries for area calc
- `ras_commander.hdf.HdfMesh.get_mesh_cell_faces()` — face geometries for length calc

### Convergence criteria:
- Cell count within 1% of reference
- Face count within 2% of reference
- No cells with > 8 faces (MaxFacesPerCellExceeded resolved)
- Cell area distribution correlation > 0.95

---

## Phase 3: Iterative Codex Subagent Loop

After each generation cycle:

1. **User** runs `GeomMesh.generate()` on the Spring Creek project (requires Windows)
2. **User** runs `python benchmarks/compare_meshes.py g01.hdf g02.hdf`
3. **Codex subagent** receives: comparison output + current GeomMesh.py + ILSpy references
   - Analyzes remaining gaps
   - Suggests specific code changes
4. **We** implement the changes
5. **Repeat** from step 1 until convergence criteria met

### Codex task template:
```
Read benchmarks/compare_output.txt for the latest metrics.
Read ras_commander/geom/GeomMesh.py for the current algorithm.
Read the ILSpy references in RASDecomp/headless_mesh/ilspy/.
Identify what causes the remaining cell/face count difference.
Suggest specific code changes with file paths and line numbers.
```

---

## Build Sequence

1. **1B** — `_compute_filter_tolerance`, `_find_duplicate_seeds`, `_remove_seeds_by_index` (new functions)
2. **1E** — Modify `_autofix_max_faces` return signature
3. **1C** — Remove perimeter face exclusion from `_autofix_max_faces`
4. **1D** — Restructure iteration loop
5. **Phase 2** — Create `benchmarks/compare_meshes.py`
6. **Phase 3** — First iteration: user generates → compare → codex analysis

## Verification

1. User generates mesh on Windows with the new code
2. Run `compare_meshes.py` — cell count should be closer to 85,187
3. Visual inspection in RAS Mapper — breakline bands should show regular rectangular cells
4. No MaxFacesPerCellExceeded errors in the MeshResult
5. The mesh should converge within 3-4 iterations of the fix loop (matching 2 "Fix All" clicks)

## Files to Modify

| File | Change |
|------|--------|
| `G:\GH\ras-commander\ras_commander\geom\GeomMesh.py` | Add dedup functions, restructure loop, fix _autofix_max_faces |
| `G:\GH\ras-agent\benchmarks\compare_meshes.py` | New: HDF mesh comparison harness |
