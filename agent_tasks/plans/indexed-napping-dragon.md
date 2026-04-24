# Plan: Match RAS Mapper Mesh — Follow RASDecomp Architecture

## Context

Our `GeomMesh.generate()` has drifted into reimplementing RasMapperLib algorithms in Python — seed generation, breakline enforcement, dedup, perimeter filtering. RASDecomp's `HeadlessMeshGenerator` (`G:\GH\RASDecomp\headless_mesh\mesh_fix.py`) proves the right approach: call .NET methods for everything, only reimplement `TryAutoFix` in Python (because it genuinely requires GUI `FeatureEditor`).

**Current problem:** Our Python `_generate_breakline_seeds()` adds ~5,000 excess breakline-corridor points. Our `.NET _generate_seeds_via_net()` (RegenerateMeshPoints) produces an exact face count match (176,473), but the overall `generate()` flow is tangled with Python reimplementations instead of clean .NET calls.

**Goal:** Restructure `generate()` to mirror RASDecomp — load geometry from .NET, seed via .NET, mesh via .NET, fix via Python TryAutoFix (justified), save via .NET.

**Reference:** `G:\GH\RASDecomp\headless_mesh\mesh_fix.py` lines 692–1037

---

## RASDecomp's Pattern (what we should match)

```
1. RASGeometry(path)           ← .NET: load geometry
2. d2fa.Geometry.MeshPerimeters.Polygon(fid)  ← .NET: get perimeter
3. _build_breaklines(d2fa)     ← .NET: merge BreakLines + Regions + Structures
4. PointGenerator.GeneratePoints(perim, cell_size)  ← .NET: generate seeds
5. MeshFV2D(perim, seeds, breaklines, None, ratio)  ← .NET: compute mesh
6. Fix loop (Python TryAutoFix) — Tier 1: ratio, Tier 2: midpoints,
   Tier 3: perimeter, Tier 4: Douglas-Peucker
7. _save_mesh() → geom.Save() ← .NET: save topology
8. Patch HDF Cell Points + .g01 text with cell centers
```

Our ONE addition: use `RegenerateMeshPoints()` (step 4) instead of `GeneratePoints()` for breakline-aware seeds. This is still a .NET call — via reflection on the private instance method.

---

## Step 1: Refactor `generate()` to Follow RASDecomp Pattern

**File:** `G:\GH\ras-commander\ras_commander\geom\GeomMesh.py`

### 1A. Load geometry from .NET (not Python text parsing)

Replace `_parse_geom_text()` call and manual Polygon/Polyline construction with:

```python
# Compile text → HDF if needed (one .NET call)
hdf_path = Path(geom_text_path).with_suffix(Path(geom_text_path).suffix + ".hdf")
if not hdf_path.exists():
    hdf_path = cls.compile_geometry(geom_text_path, hecras_dir=hecras_dir)

# Load geometry from .NET — same as RASDecomp line 743
geom = ns["RASGeometry"](str(hdf_path))
d2fa = geom.D2FlowArea
```

### 1B. Get perimeter and breaklines from .NET objects

```python
# Same as RASDecomp lines 767, 773
perim = d2fa.Geometry.MeshPerimeters.Polygon(fid)
breaklines = _build_breaklines(d2fa, ns)   # new function, from RASDecomp
```

### 1C. Generate seeds via .NET

```python
# Breakline-aware seeds via RegenerateMeshPoints (our enhancement over RASDecomp)
seeds_pm = _generate_seeds_via_net(str(hdf_path), ns)

# Fallback: base-grid via PointGenerator.GeneratePoints (same as RASDecomp)
if seeds_pm is None:
    seeds_pm = ns["PointGenerator"].GeneratePoints(perim, float(cell_size))
```

### 1D. Fix loop — same structure as RASDecomp

```python
# Tier 0: short segment removal + reseed via PointGenerator.GeneratePoints
# Tier 1: ratio escalation (no reseed)
# Tier 2: MaxFaces → add midpoints (Python _autofix_max_faces)
# Tier 3: perimeter fix → reseed via PointGenerator.GeneratePoints
# Tier 4: Douglas-Peucker → reseed via PointGenerator.GeneratePoints
```

Key: ALL reseeding in the fix loop uses `PointGenerator.GeneratePoints(perim, cell_size)` — the same .NET call RASDecomp uses. No Python seed generation.

### 1E. Save via .NET + dual patching (same as RASDecomp)

```python
# Same as RASDecomp lines 877, 899-936
_save_mesh(geom, d2fa, fid, mesh, ns)   # new function, from RASDecomp

# Patch HDF Cell Points + Cell Info + Attributes via h5py
# Patch .g01 text via _patch_text_seeds()
```

---

## Step 2: Add Functions from RASDecomp

**File:** `GeomMesh.py`

### `_build_breaklines(d2fa, ns)` — from RASDecomp lines 258-293
Merges `d2fa.Geometry.BreakLines`, `MeshRegions`, `Structures` into a single multipart Polyline. Pure .NET calls.

### `_save_mesh(geom, d2fa, fid, mesh, ns)` — from RASDecomp lines 447-457
```python
d2fa.SetMeshHasBeenRecomputed(fid, True)
d2fa.SetFeature(fid, mesh)
d2fa.SetMeshUpToDate(fid, True)
geom.Save()
```

---

## Step 3: Remove Python Reimplementations

**Delete or deprecate:**
- `_generate_breakline_seeds()` — Python reimplementation of EnforceBreaklines. Replace with `_generate_seeds_via_net()` (.NET RegenerateMeshPoints)
- `_generate_seeds_safe()` — replace with `PointGenerator.GeneratePoints()` (.NET)
- `_find_duplicate_seeds()` / `_remove_seeds_by_index()` / `_compute_filter_tolerance()` — seed dedup is handled by the .NET seed generation. Remove from fix loop
- Complex seed manipulation in fix loop (snap dedup, perimeter filtering) — replace with `PointGenerator.GeneratePoints()` reseed
- `_parse_geom_text()` for perimeter/breaklines — read from HDF via .NET instead

**Keep (justified Python reimplementations, same as RASDecomp):**
- `_autofix_max_faces()` — TryAutoFix can't run headlessly (FeatureEditor)
- `_autofix_perimeter()` — wrapper around `.ConsecutiveNearbyPointsIndices()`
- `_remove_perimeter_points()` — Polygon vertex removal
- `_douglas_peucker_polygon()` / `_localized_douglas_peucker()` — Shapely-based, not in RasMapperLib
- `_remove_short_perimeter_segments()` — Tier 0 pre-flight
- `_patch_text_seeds()` / `_set_point_generation_data()` — text file patching

---

## Step 4: Verify

```bash
cd "G:/GH/ras-agent/workspace/.../ras_agent_103mi2"

python -c "
from ras_commander.geom import GeomMesh
result = GeomMesh.generate(
    'ras_agent_103mi2.g01',
    mesh_name='MainArea',
    cell_size=200*0.3048,
)
print(f'Status: {result.status}, Cells: {result.cell_count}, Faces: {result.face_count}')
"

python benchmarks/compare_meshes.py \
    ras_agent_103mi2.g01.hdf \
    ras_agent_103mi2.g02.hdf
```

**Expected:** Face count 176,473 (exact match), cells within 1% of 83,600.

---

## Build Sequence

1. Add `_build_breaklines()` and `_save_mesh()` from RASDecomp
2. Refactor `generate()` entry: compile → load .NET → perimeter/breaklines from .NET
3. Replace seed generation with .NET calls (RegenerateMeshPoints + GeneratePoints fallback)
4. Simplify fix loop: remove Python seed manipulation, reseed via `PointGenerator.GeneratePoints()`
5. Replace save: `_save_mesh()` + HDF patching + text patching (match RASDecomp)
6. Remove/deprecate Python reimplementations
7. Verify against g02.hdf

## Files to Modify

| File | Change |
|------|--------|
| `G:\GH\ras-commander\ras_commander\geom\GeomMesh.py` | Refactor `generate()` to follow RASDecomp pattern, add `_build_breaklines`/`_save_mesh`, remove Python reimplementations |

## Reference Files

| File | Purpose |
|------|---------|
| `G:\GH\RASDecomp\headless_mesh\mesh_fix.py` | Reference implementation — `HeadlessMeshGenerator.generate()` |
| `G:\GH\RASDecomp\headless_mesh\ilspy\ilspy_pointgenerator.txt` | RegenerateMeshPoints API |
| `G:\GH\RASDecomp\headless_mesh\ilspy\ilspy_meshfv2d.txt` | TryAutoFix API (why Python reimpl needed) |
