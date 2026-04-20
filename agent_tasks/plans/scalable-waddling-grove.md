# Plan: 2D BC Line Generation for geometry_first Models

## Context

The `geometry_first` pipeline produces valid 2D geometry (`.g01` with `Storage Area` + `Storage Area Is2D=-1`) and HEC-RAS successfully preprocesses it. However, the `.u01` still uses **1D boundary references** (`Boundary Location=RAS_AGENT,MAIN,1.0`) that reference non-existent 1D geometry. HEC-RAS ignores these during preprocessing but **simulation cannot run** without proper 2D BC Lines.

This plan adds automatic BC Line generation — the last blocker before geometry-first models can run actual simulations.

### Reference Models Studied

| Model | BC Lines | Pattern |
|-------|----------|---------|
| BaldEagleCrkMulti2D (.g02/.u02) | 2 | DSNormalDepth (friction slope) + Upstream Inflow (flow hydrograph) |
| Upper Calcasieu RAS2D (.g01/.u01) | 18 | 14 Normal_Depth perimeter BCs + 4 named tributary inflow BCs |

---

## BC Line Format (.g01)

```
BC Line Name=DSNormDepth                     
BC Line Storage Area=MainArea         
BC Line Start Position= {x0} , {y0} 
BC Line Middle Position= {xm} , {ym} 
BC Line End Position= {xn} , {yn} 
BC Line Arc= {N}
{16-char fixed-width coords: x1 y1 x2 y2 per line — same as Surface Line format}
BC Line Text Position= 0 , 0 
```

- `BC Line Arc= N` = total vertex count (includes start+end)
- Coordinates: 4 values per line, 16-char right-justified fields, no delimiters
- Names padded to 32 chars with trailing spaces
- BC Line blocks appear BEFORE `LCMann Time=` in .g01

## Boundary Location Format (.u01)

```
Boundary Location=                ,                ,        ,        ,                ,{area:16s},                ,{bc_name:16s}
```

- 8 comma-separated fixed-width fields
- Fields 1-5 blank for 2D BCs (those are 1D River/Reach/RS/Structure)
- Field 6 = 2D area name (16 chars)
- Field 8 = BC Line name
- Followed by `Friction Slope=X,0` (normal depth) or `Flow Hydrograph= N` (inflow)

---

## Algorithm

### Inputs Available at Model Build Time
- `WatershedResult.basin` — watershed polygon (EPSG:5070)
- `WatershedResult.streams` — TauDEM stream network (EPSG:5070)
- `WatershedResult.pour_point` — outlet (EPSG:5070)
- `WatershedResult.characteristics.main_channel_slope_m_per_m` — for normal depth slope
- `dem_clipped` — clipped DEM for elevation sampling
- 2D area name = `MainArea`

### Step 1: Find stream-boundary intersections

Intersect each stream LineString with `basin.boundary`. Collect all intersection points with their position along the boundary ring (normalized 0–1 via `boundary.project(pt, normalized=True)`). Sort by boundary position.

### Step 2: Classify outlet vs. inflow

The intersection closest to `pour_point` (within 500m) is the **outlet**. All others are **upstream inflow** entry points.

### Step 3: Determine BC Line extents via terrain sampling

At each stream crossing point:
1. Get the DEM elevation at the crossing point
2. Walk along the boundary in both directions (10m steps via `boundary.interpolate()`)
3. Stop when terrain exceeds `crossing_elev + threshold_ft × 0.3048`
4. Record boundary positions `(t_left, t_right)` — the extent of this BC Line

**Default `threshold_ft = 30`** (configurable 20–40). This captures the floodplain width at each crossing without extending into high ground.

### Step 4: Build offset polylines

For each crossing's boundary segment `(t_left, t_right)`:
1. Extract the sub-arc from the boundary
2. Offset outward from `basin_poly` by `offset_ft × 0.3048` (default 500 ft ≈ 152m)
3. Use Shapely `offset_curve()` — test which side is outward via `basin_poly.contains()`
4. Simplify to 3–8 vertices via `simplify(tolerance)` proportional to cell size

### Step 5: Gap between adjacent BC Lines

Leave a gap of ~1 cell width between each stream crossing BC and the adjacent Normal Depth BC. This ensures clean face assignment without overlap.

### Step 6: Fill gaps with Normal Depth BC Lines

All boundary segments NOT covered by stream crossing BCs get Normal Depth boundaries:
1. Extract boundary sub-arc between adjacent stream BCs (minus gaps)
2. Offset outward same as Step 4
3. Assign `bc_type="normal_depth"`, slope = `main_channel_slope_m_per_m`

### Step 7: Naming

- Outlet: `DSOutflow` (normal depth at downstream exit)
- Upstream inflows: `USInflow1`, `USInflow2`, ... (ordered by boundary position)
- Perimeter normal depth: `NormDepth1`, `NormDepth2`, ... (fill segments)
- All names ≤ 16 chars

### Step 8: Assign hydrographs

- **Outlet** → Normal Depth (friction slope = channel slope)
- **Upstream inflows** → Flow Hydrograph (split from design hydrograph proportionally by upstream contributing area if available, else equal split)
- **Perimeter normal depth** → Normal Depth (friction slope = 0.00033) — standard low value prevents ponding while maintaining stability; NOT channel slope

---

## Fallbacks

| Condition | Behavior |
|-----------|----------|
| No DEM available | Use fixed angular extent (~10° arc from centroid) at each crossing |
| No stream crossings found | Single downstream Normal Depth BC near pour_point + single upstream Flow Hydrograph at farthest boundary point |
| DEM nodata at boundary | Extend BC Line through nodata zones (conservative) |
| Single stream crossing | One Flow Hydrograph (upstream face) + one Normal Depth (downstream face) at same crossing location |

Always produce **at least 1 inflow + 1 outflow** BC Line.

---

## Files

### New: `pipeline/bc_lines.py` (~400 lines)

```
BCLineSpec          — dataclass: name, storage_area, coords, bc_type, stream_index
BCLineSet           — dataclass: bc_lines list, area_name, metadata

generate_bc_lines() — main entry point (basin, streams, pour_point, dem → BCLineSet)

_find_stream_boundary_intersections()  — Step 1
_classify_outlet()                     — Step 2
_find_bc_extent_along_boundary()       — Step 3 (DEM sampling walk)
_build_offset_polyline()               — Step 4
_fill_normal_depth_gaps()              — Step 6
_simplify_bc_line()                    — Step 8

format_bc_line_block()          — produce .g01 text block for one BC Line
format_2d_boundary_location()   — produce .u01 Boundary Location line
_format_bc_arc_coords()         — 16-char, 4-values-per-line coordinate formatter
_sample_dem_elevation()         — rasterio point sampling with nodata handling
```

### Modify: `pipeline/model_builder.py` (~80 lines changed)

- `_build_geometry_first()` — add BC Line generation after geom file write
- New `_append_bc_lines_to_geom(geom_file, bc_line_set)` — insert BC blocks before LCMann
- New `_write_unsteady_flow_file_2d(flow_file, hydro_set, rp, slope, bc_line_set)` — replaces 1D-ref writer for geometry_first strategy
- Keep `_write_unsteady_flow_file()` intact for `hdf5_direct` / `template_clone` backward compat

### New: `tests/test_bc_lines.py` (~300 lines)

- Synthetic box watershed + straight stream → verify 2 intersections
- Classify outlet by proximity to pour_point
- BC Line offset is outside basin polygon
- Format tests against BaldEagle golden data
- 2D Boundary Location field widths
- End-to-end: box watershed → BCLineSet → formatted blocks
- Fallback: no DEM, no stream crossings
- All names ≤ 16 chars

### Modify: `tests/test_model_builder.py` (~30 lines)

- geometry_first tests: assert `.g01` contains `BC Line Name=`
- Assert `.u01` contains 2D `Boundary Location` (not 1D `RAS_AGENT,MAIN`)
- Assert BC Line names in .g01 match those referenced in .u01

---

## Build Sequence

1. **Formatters first** — `_format_bc_arc_coords()`, `format_bc_line_block()`, `format_2d_boundary_location()` — testable against BaldEagle golden data immediately
2. **DEM sampler** — `_sample_dem_elevation()` + `_find_bc_extent_along_boundary()` with unit tests
3. **Core algorithm** — `generate_bc_lines()` with intersection, classification, offset, gap-fill
4. **model_builder integration** — `_append_bc_lines_to_geom()` + `_write_unsteady_flow_file_2d()` + wire into `_build_geometry_first()`
5. **Test suite** — unit tests + update model_builder tests
6. **Smoke test** — run on Spring Creek project, verify HEC-RAS preprocessing still passes with BC Lines present

## Verification

1. `pytest tests/test_bc_lines.py` — all BC Line unit tests pass
2. `pytest tests/test_model_builder.py` — no regressions, new geometry_first BC assertions pass
3. Spring Creek smoke test: build geometry_first project, confirm `.g01` has BC Lines, `.u01` has 2D Boundary Locations
4. HEC-RAS preprocessing: `compute_plan(force_geompre=True)` succeeds with BC Lines in .g01

---

## Mesh Generation — Porting RASDecomp into ras-commander

RASDecomp (`G:\GH\RASDecomp\headless_mesh\`) is a development/prototyping repo. Its capabilities must be **ported into ras-commander** as proper modules. After porting, ras-agent imports from `ras_commander.geom` like any other consumer.

### Source Material (RASDecomp, reference only)

| File | Capability | ~LOC |
|------|-----------|------|
| `headless_mesh/mesh_fix.py` | HeadlessMeshGenerator: mesh gen + 5-tier auto-fix | 1061 |
| `headless_mesh/mesh_bc_fix.py` | MeshBcFix: dual-BC-on-same-face detection + repair | 542 |
| `headless_mesh/mesh_sweep.py` | MeshSweep: parameter sweep orchestration | 510 |

### Target Location in ras-commander

**New file: `ras_commander/geom/GeomMesh.py`** (~800 lines)

Follows existing pattern: static class with `@log_call` and `@standardize_input` decorators. Fits alongside GeomStorage, GeomPreprocessor in the `geom/` subpackage.

```python
# ras_commander/geom/GeomMesh.py
class GeomMesh:
    """Headless 2D mesh generation, repair, and BC conflict resolution via RasMapperLib."""

    @staticmethod
    def setup_gdal_bridge(hecras_dir=None) -> bool: ...

    @staticmethod
    def set_breakline_spacing(geom_text_path, near=None, far=None) -> Path: ...

    @staticmethod
    def generate(hdf_path, mesh_name=None, cell_size=None,
                 min_face_length_ratio=0.05, max_iterations=8) -> MeshResult: ...

    @staticmethod
    def generate_all(hdf_path, cell_size=None, **kwargs) -> List[MeshResult]: ...

    @staticmethod
    def detect_bc_conflicts(geom_hdf_path, cell_size) -> List[BCConflict]: ...

    @staticmethod
    def fix_bc_conflicts(geom_hdf_path, cell_size, dry_run=False) -> BCFixResult: ...
```

**New file: `ras_commander/geom/GeomMeshDataclasses.py`** (~80 lines)

```python
@dataclass
class MeshResult:
    mesh_name: str
    status: str           # "complete", "error", "exception"
    cell_count: int = 0
    face_count: int = 0
    iterations: int = 0
    fixes_applied: List[str] = field(default_factory=list)
    error_message: str = ""
    geom_hdf_path: str = ""

@dataclass
class BCConflict:
    face_id: int
    bc_names: List[str]
    normal_depth_bc: Optional[str] = None

@dataclass
class BCFixResult:
    conflicts_found: int = 0
    conflicts_fixed: int = 0
    unresolveable: List[BCConflict] = field(default_factory=list)
    modified_hdf: bool = False
```

### Dependencies to Add to ras-commander

- **pythonnet ≥ 3.0.5** — add as optional dependency in setup.py (`extras_require["mesh"]`)
- h5py, shapely, numpy already present in ras-commander

### 5-Tier Auto-Fix (ported from RASDecomp)

| Tier | Trigger | Fix |
|------|---------|-----|
| 0 | Pre-flight | Remove perimeter segments < `cell_size × ratio` |
| 1 | Any error | Escalate ratio: `[0.05, 0.10, 0.15, 0.25]` |
| 2 | MaxFacesPerCell (>8) | Add midpoints of 2 longest faces per bad cell |
| 3 | FacePerimeterConnectionError | Remove near-duplicate perimeter vertices (tol=1e-6) |
| 4 | Persistent errors | Localized → global Douglas-Peucker simplification |

### Integration in ras-agent (after porting)

```python
from ras_commander.geom import GeomMesh

# Step A: Set breakline spacing in .g01 text
GeomMesh.set_breakline_spacing(geom_file, near=cell_size * 0.33, far=cell_size)

# Step B: HEC-RAS initial preprocessing (compiles .g01 → .g01.hdf)
RasCmdr.compute_plan(force_geompre=True)

# Step C: Headless mesh generation with auto-fix
result = GeomMesh.generate(geom_file, mesh_name="MainArea", cell_size=cell_size)
assert result.ok

# Step D: BC conflict detection/fix
bc_result = GeomMesh.fix_bc_conflicts(result.geom_hdf_path, cell_size)
assert bc_result.ok

# Step E: Re-preprocess with final mesh (terrain draping)
RasCmdr.compute_plan(force_geompre=True)
```

### Integration Architecture

```
pipeline/model_builder.py::_build_geometry_first()
    │
    ├── 1. Write .g01 text (GeomStorage) — existing
    ├── 2. Append BC Lines to .g01 text — NEW (bc_lines.py)
    ├── 3. Set breakline spacing in .g01 text — NEW (GeomMesh)
    ├── 4. HEC-RAS preprocessing (geom → .g01.hdf) — existing
    ├── 5. Headless mesh generation + auto-fix — NEW (GeomMesh)
    ├── 6. BC conflict detection/fix — NEW (GeomMesh)
    ├── 7. Re-preprocess with final mesh — existing
    └── 8. Write .u01 with 2D Boundary Locations — NEW (bc_lines.py)
```

---

## Branch Management

### ras-commander (`G:\GH\ras-commander`) — mesh porting

| Branch | Purpose | Base |
|--------|---------|------|
| `cherry/unreleased-features` | Current HEAD with latest features | — |
| `feat/headless-mesh` | **NEW** — Port RASDecomp mesh capabilities | `cherry/unreleased-features` |

**Work scope in ras-commander:**
1. Create `ras_commander/geom/GeomMesh.py` — port HeadlessMeshGenerator + MeshBcFix
2. Create `ras_commander/geom/GeomMeshDataclasses.py` — MeshResult, BCConflict, BCFixResult
3. Export from `geom/__init__.py` and top-level `__init__.py`
4. Add `pythonnet>=3.0.5` to `extras_require["mesh"]` in setup.py
5. Unit tests in `tests/test_geom_mesh.py`

**Do this work FIRST** — ras-agent depends on it.

### ras-agent (`G:\GH\ras-agent`) — BC Lines + integration

| Branch | Purpose | Base |
|--------|---------|------|
| `main` | Current stable (Phases A-E done, 1D placeholder BCs) | — |
| `feat/bc-lines-mesh` | **NEW** — BC Line generation + mesh integration | `main` |

**Create from current `main`:**
```bash
git checkout -b feat/bc-lines-mesh
```

**Work scope in ras-agent:**
1. `pipeline/bc_lines.py` — BC Line generation algorithm
2. `pipeline/model_builder.py` — wire BC Lines + GeomMesh into `_build_geometry_first()`
3. `tests/test_bc_lines.py` — unit tests
4. Update `pipeline/requirements.txt` — add `ras-commander[mesh]` or editable install

**Depends on `feat/headless-mesh` in ras-commander.** During development, use editable install:
```bash
pip install -e "G:/GH/ras-commander[mesh]"
```

### RASDecomp (`G:\GH\RASDecomp`)

- **Reference only** — no changes, no branches. Source material for porting.
- After porting is validated, RASDecomp's mesh capabilities become superseded by ras-commander.

### Merge Order

1. `feat/headless-mesh` → ras-commander `main` (or `cherry/unreleased-features`)
2. `feat/bc-lines-mesh` → ras-agent `main`

---

## Build Sequence

### Phase 1: Port mesh to ras-commander (on `feat/headless-mesh`)

1. **Create branch** — `cd G:/GH/ras-commander && git checkout -b feat/headless-mesh`
2. **Dataclasses** — `geom/GeomMeshDataclasses.py`: MeshResult, BCConflict, BCFixResult
3. **Core mesh gen** — `geom/GeomMesh.py`: port `HeadlessMeshGenerator.generate()` with 5-tier auto-fix
4. **Breakline spacing** — port `set_breakline_spacing()` (text file editor)
5. **BC conflict fix** — port `MeshBcFix.detect_and_fix()` → `GeomMesh.fix_bc_conflicts()`
6. **GDAL bridge** — port `setup_gdal_bridge()` (one-time junction creation)
7. **Exports** — update `geom/__init__.py`, top-level `__init__.py`, setup.py `extras_require`
8. **Tests** — `tests/test_geom_mesh.py` (unit tests with mock .NET bridge for CI)
9. **Validate** — run on Spring Creek `.g01.hdf` to confirm mesh gen + BC fix works standalone

### Phase 2: BC Lines + integration in ras-agent (on `feat/bc-lines-mesh`)

1. **Create branch** — `cd G:/GH/ras-agent && git checkout -b feat/bc-lines-mesh`
2. **Install** — `pip install -e "G:/GH/ras-commander[mesh]"`
3. **Formatters** — `_format_bc_arc_coords()`, `format_bc_line_block()`, `format_2d_boundary_location()`
4. **DEM sampler** — `_sample_dem_elevation()` + `_find_bc_extent_along_boundary()`
5. **Core BC algorithm** — `generate_bc_lines()` with intersection, classification, offset, gap-fill
6. **model_builder integration** — `_append_bc_lines_to_geom()` + `_write_unsteady_flow_file_2d()` + wire GeomMesh calls
7. **Test suite** — `tests/test_bc_lines.py` + update `tests/test_model_builder.py`
8. **Smoke test** — Spring Creek end-to-end: BC Lines → mesh gen → BC fix → HEC-RAS preprocess

## Verification

### ras-commander (`feat/headless-mesh`)
1. `pytest tests/test_geom_mesh.py` — mesh dataclass + format tests pass
2. Integration test: `GeomMesh.generate()` on Spring Creek `.g01` produces valid mesh
3. `GeomMesh.fix_bc_conflicts()` detects/resolves test conflicts

### ras-agent (`feat/bc-lines-mesh`)
1. `pytest tests/test_bc_lines.py` — all BC Line unit tests pass
2. `pytest tests/test_model_builder.py` — no regressions, new geometry_first BC assertions pass
3. Spring Creek smoke test: full pipeline produces valid `.g01` with BC Lines + mesh
4. HEC-RAS preprocessing: `compute_plan(force_geompre=True)` succeeds with final mesh + BC Lines
5. `.u01` has 2D Boundary Locations (not 1D refs)

## Risks

- **Shapely `offset_curve()` side convention** — must test which side is outward; flip if needed
- **Stream endpoints not touching boundary** — use 10m buffer for intersection or `snap()` to boundary
- **DEM nodata near boundary edges** — handle gracefully in sampler
- **Hydrograph splitting** for multiple inflows — use contributing area if available from TauDEM `DSContArea`
- **pythonnet DLL load order** — must load Utility.Core → Geospatial.Core → H5Assist → RasMapperLib in sequence; encapsulate in `GeomMesh._load_dlls()`
- **GDAL junction** — `setup_gdal_bridge()` uses PowerShell `New-Item -ItemType Junction`; fails silently if junction already exists (OK) or if no admin rights (need elevated shell once)
- **pythonnet as optional dep** — use `extras_require["mesh"]` so base ras-commander installs don't require .NET; guard imports with try/except at module level
- **CI cannot run mesh tests** — GitHub Actions is Linux; RasMapperLib.dll is Windows-only. Mesh tests must be marked `@pytest.mark.skipif(platform != "win32")` or mocked
- **Two-branch coordination** — ras-agent `feat/bc-lines-mesh` depends on ras-commander `feat/headless-mesh`; use editable install during development, pin to release after merge
