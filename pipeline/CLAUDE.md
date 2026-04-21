# CLAUDE.md — pipeline/

Python backend for the RAS Agent modeling pipeline. All modules use bare imports (e.g., `import terrain`), not package-relative imports.

## Module Map

| Module | Role | Key types |
|--------|------|-----------|
| `orchestrator.py` | Chains 7 stages into `run_watershed()` | `OrchestratorResult` |
| `terrain.py` | DEM download + mosaic + NLCD land cover | `get_terrain()`, `get_nlcd()` |
| `watershed.py` | pysheds D8 delineation | `WatershedResult`, `BasinCharacteristics` |
| `streamstats.py` | USGS StreamStats + IL regression fallback | `PeakFlowEstimates` |
| `hydrograph.py` | NRCS DUH synthetic hydrographs | `HydrographResult`, `HydrographSet` |
| `model_builder.py` | Template clone + Cartesian mesh + RC wiring + HDF5 fallback | `HecRasProject`, `build_model()`, `_fmt_coord()`, `_generate_cartesian_cell_centers()`, `_write_cell_centers_to_geometry_file()` |
| `runner.py` | SQLite job queue + Linux geometry preprocess + RasUnsteady | `enqueue_job()`, `run_queue()` |
| `windows_agent.py` | Windows RASMapper mesh creation (`g01.hdf`) | `WindowsAgent`, `MeshRequest`, `MeshResult` |
| `results.py` | HDF5 → raster/vector export + cloud-native GeoParquet via ras2cng | `FlowAreaGeometry`, `FlowAreaResults`, `export_results()`, `extract_max_velocity()`, `extract_flow_area_results()`, `export_cloud_native()` |
| *(ras2cng)* | HEC-RAS project → GeoParquet archive, DuckDB analytics, PMTiles (MIT, optional dep) | `archive_project`, `inspect_project`, `export_results_layer`, `DuckSession` |
| `api.py` | FastAPI REST endpoints | runs on `:8000` |
| `batch.py` | Multi-watershed parallel execution | `run_batch()` |
| `report.py` | Self-contained HTML run reports | `generate_report()` |
| `notify.py` | Webhook + email notifications | `NotifyConfig` |
| `storage.py` | Cloudflare R2 upload | `R2Config`, `upload_results_dir()` |

## Patterns

- **Graceful degradation:** `model_builder.py` tries `ras-commander` first, falls back to `shutil`/`h5py`. `streamstats.py` tries API, falls back to regression equations. Never hard-fail on optional deps.
- **Mock mode:** `runner.py` with `mock=True` creates fake HDF5 output. All downstream code handles this.
- **Error handling:** `orchestrator.py` raises `OrchestratorError` for fatal stages (1-2), returns `status="partial"` with `errors` list for stages 3-7.
- **Lazy imports:** `api.py` lazy-imports `runner`, `storage`, `report`, `notify` to avoid pulling heavy deps at module load time.
- **Output structure:** `{output_dir}/terrain/`, `model/`, `results/{rp}yr/`, `logs/`, `jobs.db`, `report.html`

## HEC-RAS HDF5 Paths

### HEC-RAS 6.x schema (primary)

| Dataset | HDF Path | Shape |
|---------|----------|-------|
| Cell centers | `Geometry/2D Flow Areas/<name>/Cells Center Coordinate` | (N, 2) float64 |
| Face-point coords | `Geometry/2D Flow Areas/<name>/FacePoints Coordinate` | (P, 2) float64 |
| Face → face-point | `Geometry/2D Flow Areas/<name>/Faces FacePoint Indexes` | (F, 2) int32 |
| Cell → face connectivity | `Geometry/2D Flow Areas/<name>/Cells Face and Orientation` | (N, *) int32 |
| Depth time series | `Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/<name>/Depth` | (T, N) float32 |
| WSE time series | `Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/<name>/Water Surface` | (T, N) float32 |
| Velocity time series | `Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/<name>/Velocity` | (T, N) float32 |
| Face velocity | `Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/<name>/Face Velocity` | (T, F) float32 |

### HEC-RAS 2025 schema (fallback, auto-detected)

| Dataset | HDF Path |
|---------|----------|
| Cell centers | `Geometry/2D Flow Areas/<name>/Cell Coordinates` |
| Depth | `Results/Output Blocks/Base Output/2D Flow Areas/<name>/Depth` |
| WSE | `Results/Output Blocks/Base Output/2D Flow Areas/<name>/Water Surface` |
| Velocity | `Results/Output Blocks/Base Output/2D Flow Areas/<name>/Velocity` |
| Face velocity | `Results/Output Blocks/Base Output/2D Flow Areas/<name>/Face Velocity` |

Use `detect_ras_version(hdf_path)` → `"6.x"` or `"2025"` to identify schema automatically.

### Typed Result Dataclasses

```python
@dataclass
class FlowAreaGeometry:
    name: str
    cell_centers: np.ndarray        # (N, 2) x/y in project CRS
    face_points: Optional[np.ndarray]        # (P, 2) — None if absent
    face_point_indexes: Optional[np.ndarray] # (F, 2) int32 — None if absent
    cell_face_info: Optional[np.ndarray]     # connectivity — None if absent

@dataclass
class FlowAreaResults:
    name: str
    geometry: FlowAreaGeometry
    max_depth: np.ndarray           # (N,) float32, m
    max_wse: np.ndarray             # (N,) float32, m
    max_velocity: Optional[np.ndarray]       # (N,) float32, m/s — None if absent
```

Use `extract_flow_area_results(hdf_path, area_name)` to get all fields in one call.

### Raster Interpolation Methods (`cells_to_raster`)

| method | Description |
|--------|-------------|
| `"linear"` (default) | scipy griddata linear triangulation |
| `"nearest"` | scipy griddata nearest-neighbor |
| `"face_weighted"` | IDW (k=8 neighbors) — use face-point coords for RASMapper-equivalent rendering |

HDF path patterns and dataclass design inspired by rivia (github.com/gyanz/rivia, Apache 2.0).

## Dependencies

Requires system GDAL (`libgdal-dev`). Install order matters:
```bash
pip install gdal==$(gdal-config --version)   # must match system GDAL
pip install -r requirements.txt
```

### hecras-v66-linux (vendored, Apache 2.0)

`vendor/hecras-v66-linux/` — Linux geometry preprocessor by Neerai Prasad
(github.com/neeraip/hecras-v66-linux).  Replicates the HEC-RAS GUI "Compute Geometry"
step in pure Python + the `RasGeomPreprocess` / `RasUnsteady` Linux binaries.

Used by `runner.py` when `preprocess_mode='linux'` (the default for new jobs).  Eliminates
the Windows preprocessing bottleneck; Windows is now only needed for initial RASMapper
mesh creation (`g01.hdf`).

**Three preprocessing workflows:**

| Workflow | When | What happens |
|----------|------|--------------|
| **A** | `g01.hdf` has full hydraulic tables + `p01.hdf` exists | Strip Results → `p01.tmp.hdf`; reuse geometry |
| **B** | `g01.hdf` exists (RASMapper mesh), no hydraulic tables | Compute hydraulic tables on Linux → `p01.tmp.hdf` |
| **C** | No `g01.hdf` | Build Voronoi mesh from `.g01` seed points → full Workflow B |

ras-agent primary path: **Workflow B** (mesh from RASMapper, tables computed on Linux).

The Linux binaries (`bin/RasGeomPreprocess`, `bin/RasUnsteady`) are tracked via git LFS
and are not required for mock mode.  See `vendor/hecras-v66-linux/RAS_AGENT.md` for
integration details.

---

## Domain Knowledge — Illinois Hydrology & HEC-RAS Modeling

### Manning's Roughness Coefficient (n) — NLCD Lookup Table

Used by `model_builder.py`. Values from USACE practice guidelines and Chow (1959).

| NLCD Code | Description | Typical n | Safety Bounds | Notes |
|-----------|-------------|-----------|---------------|-------|
| 11 | Open water | 0.035 | 0.020–0.050 | Channel: 0.025–0.035; ponds/lakes: 0.030–0.040 |
| 21 | Dev. low intensity | 0.080 | 0.050–0.120 | Sparse structures, golf courses |
| 22 | Dev. medium intensity | 0.100 | 0.060–0.150 | Mixed structures |
| 23 | Dev. high intensity | 0.120 | 0.070–0.180 | Dense urban, parking |
| 24 | Dev. open space | 0.065 | 0.050–0.080 | Parks, utility corridors |
| 31 | Barren land | 0.030 | 0.020–0.050 | Rock, sand, bare soil |
| 41 | Deciduous forest | 0.120 | 0.080–0.160 | Oak, maple, ash (dominant IL) |
| 42 | Evergreen forest | 0.140 | 0.090–0.180 | Pine, fir, spruce |
| 43 | Mixed forest | 0.130 | 0.085–0.170 | |
| 52 | Shrub/scrub | 0.060 | 0.030–0.100 | Low brush, young forest |
| 71 | Grassland/herbaceous | 0.040 | 0.035–0.050 | Overland floodplain flow |
| 81 | Pasture/hay | 0.035 | 0.025–0.060 | Mowed/grazed grassland |
| 82 | Cultivated crops | 0.037 | 0.025–0.070 | Row crops; bare post-harvest soil: 0.020–0.030 |
| 90 | Woody wetlands | 0.080 | 0.050–0.150 | Swamp, bottomland forest |
| 95 | Herbaceous wetlands | 0.080 | 0.050–0.150 | Marsh, sedge meadow |

Flag if assigned n falls outside Safety Bounds. HITL required if NLCD class not in table.

### Time of Concentration (Tc) — Kirpich Formula

Standard method for Illinois ungauged agricultural watersheds (10–500 mi²).

```
Tc = 0.0078 * L^0.77 * S^(-0.385)   [result in minutes → divide by 60 for hours]

L = longest flow path (main channel length, feet)
    ⚠️ L is the longest flow path, NOT watershed perimeter or straight-line distance
S = main channel slope (ft/ft) — computed by 10–85% method
```

| Drainage Area | Typical Tc |
|---------------|-----------|
| 10 mi² | 0.5–2.0 hr |
| 50 mi² | 1.5–5.0 hr |
| 100 mi² | 2.5–7.0 hr |
| 200 mi² | 4.0–10.0 hr |
| 500 mi² | 6.0–12.0 hr |

Tc < 0.25 hr or > 15 hr → HITL required. S < 0.0001 ft/ft → Kirpich unreliable.

Reference: NRCS NEH Part 630, Section 16.1

### Peak Flow Estimation

Priority order:
1. **USGS StreamStats API** (preferred) — watershed-specific, includes 95% CI
2. **IL regression fallback** (Soong et al. 2008, USGS SIR 2008-5176) — regional equations

Valid range: 10–500 mi², rural Illinois (< 10% impervious).

Typical unit peak flow for Q100 in Illinois: **50–150 csm** (CFS per square mile).
Safety bounds: 30–800 csm. Outside bounds → HITL.

If API and regression differ by > ±20% → HITL (which source is authoritative?).
Always document confidence intervals — do not report Q100 as a single point estimate.

### NRCS Peak Rate Factor

| Terrain | Factor | When |
|---------|--------|------|
| Standard | 484 | Default for IL (slope ≥ 0.5%) |
| Flat | 300 | Mean slope < 0.5% (prairie, deltaic) |

Implemented in `hydrograph.py` as `peak_rate_factor` parameter (default: 484).

### Hydrograph Generation — NRCS Dimensionless Unit Hydrograph

Standard: NEH Part 630, Chapter 16.

```
Tp = Tc/2 + 0.6 * Tc = 1.1 * Tc/2    (time to peak, hr — simplified)
Lag = 0.6 * Tc
Qp = (484 * A) / Tp                    (peak discharge, CFS; A = drainage area mi²)
```

Duration: 1.5–3.0 × Tp typical (full recession to baseflow).

### Cell Size (2D Mesh Resolution)

| Watershed Area | Recommended | Min (LiDAR) | Max (stability) |
|----------------|------------|------------|-----------------|
| < 50 mi² | 15–30 m | 10 m | 50 m |
| 50–150 mi² | 25–60 m | 10 m | 100 m |
| 150–300 mi² | 50–100 m | 15 m | 150 m |
| > 300 mi² | 75–200 m | 20 m | 300 m |

< 10 m or > 300 m → HITL. Selection rationale must be logged (transparency.md).

### Perimeter Update — Confirmed Approach (Bill Katzenmeyer, 2026-03-13)

Write watershed boundary coordinates to ASCII `.g##` geometry file.
HEC-RAS regenerates geometry HDF on next save/open.
Mesh regeneration after perimeter change requires RASMapper (Windows GUI).
Ajith Sundarraj (CLB Engineering) is building RASMapper automation for this step.

Implemented in `model_builder.py:_write_perimeter_to_geometry_file()`.

### Cartesian Mesh Generation — Breaking the RAS 6.6 Mesh Lock (CLB Engineering, April 2026)

**Key insight:** HEC-RAS 6.6 reads cell center coordinates from the `Storage Area 2D Points`
section of the `.g##` text file, runs Voronoi tessellation, and writes full mesh topology to
`.g##.hdf`.  Whoever controls the cell centers controls the mesh — no RASMapper or GUI needed.

**Fixed-width encoding (CRITICAL):** Each coordinate is encoded in exactly 16 characters.
Do NOT use `f"{x:.6f}"` (wrong length → garbage mesh).  Use `_fmt_coord(x)`.

```python
def _fmt_coord(x: float) -> str:
    n_int = len(str(int(abs(x))))   # digits before decimal
    n_dec = 16 - n_int - 1          # remaining chars after decimal point
    return f"{x:.{n_dec}f}"         # exactly 16 characters
```

**Grid shift (topological safety):** Voronoi boundaries (VBs) sit halfway between adjacent
cell centers.  When a perimeter polygon vertex falls within `tol = MinFaceLength × CellSize`
of a VB, the preprocessor reports face errors.  `_generate_cartesian_cell_centers()` scans
(dx_shift, dy_shift) ∈ [0, cell_size) × [0, cell_size) to find a safe grid origin.

Functions:
- `_fmt_coord(x)` — 16-char fixed-width encoder
- `_generate_cartesian_cell_centers(polygon, cell_size_m)` → (centers, dx, dy)
- `_write_cell_centers_to_geometry_file(geom_file, area_name, centers)` — writes section

`template_clone` strategy now automatically generates and writes Cartesian cell centers
after the perimeter update (step 4b in `_build_from_template`).  If generation fails,
logs a warning and continues — the preprocessor falls back to the perimeter-only approach.

Reference: `vendor/RASAlphaCLI/docs/Breaking_The_RAS66_Mesh_Lock.md`

### Flood Depth Accuracy Target

| Use | Target | Standard |
|-----|--------|---------|
| FEMA FIRM (regulatory) | < 0.5 ft RMSE | FEMA standard |
| Engineering study | < 1.0 ft RMSE | |
| Planning-level | < 2.0 ft RMSE | |

### Illinois Watershed Characteristics (typical, 10–500 mi² ag basins)

| Parameter | Typical Range | Flag Condition |
|-----------|---------------|----------------|
| Relief | 200–600 ft | < 50 ft or > 1500 ft → HITL |
| Mean slope | 0.5–2.5% | < 0.1% or > 5% → WARN |
| Cultivated crops (NLCD 82) | 40–70% | < 10% or > 90% → note context |
| Pour point snap distance | < 300 m | > 500 m → HITL |

### QAQC Integration Pattern (Orchestrator)

The `qaqc-validator` agent defines validation logic. When pipeline code integration
happens (Phase C+), `orchestrator.py` will call it after each stage:

```python
# Pattern for orchestrator.py integration (not yet implemented in code)
from qaqc_validator import validate_stage   # future module

result_stage_2 = watershed.delineate(...)
qaqc = validate_stage("watershed", result_stage_2, hitl_config=hitl_config)

if qaqc.status == "HITL":
    # Route via expert_liaison — blocks in blocking mode, flags in async
    expert_liaison.ask(urgency="blocking", context=qaqc.findings)
elif qaqc.status == "WARN":
    logger.warning("[QAQC] Stage 2 warnings: %s", qaqc.findings)
    # Proceed — warn is non-blocking but logged
# PASS: proceed silently
```

The pattern: **stage completes → validate → PASS continues → WARN logs → HITL routes to expert.**
Stages 1–2 are fatal (raise `OrchestratorError` on HITL). Stages 3–7 use partial failure pattern.

### HITL Configuration

HITL routing uses `HITLConfig` — a portable abstraction that keeps the repo channel-agnostic.

Default (zero-config): `mode=blocking`, `channel=stdin` — print question to terminal, wait for reply.
Reference deployment (Glenn's instance): `mode=blocking`, `channel=telegram` (env vars).

Modes: `blocking` (wait for reply) | `async` (proceed, flag output) | `abort` (stop on trigger).

See `.claude/agents/expert-liaison/SUBAGENT.md` and `.claude/rules/human-in-the-loop.md`.

### Reference Documents

- NRCS NEH Part 630, Chapters 13 & 16 — Tc and Unit Hydrograph
- USGS SIR 2008-5176 (Soong et al. 2008) — Illinois Peak-Flow Regression Equations
- HEC-RAS 6.6 User Manual — 2D Unsteady Flow Modeling
- Chow, V.T. (1959) — Open Channel Hydraulics (Manning's n reference)
- USACE HEC-RAS 2D Modeling User's Manual — roughness guidance
