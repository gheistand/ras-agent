# RAS Agent — Project Knowledge File
*Created 2026-03-13 from session context before compaction*

---

## Project Identity

- **Name:** RAS Agent
- **Repo:** https://github.com/gheistand/ras-agent (public)
- **Local path:** `~/ras-agent/`
- **Cloudflare Pages project:** `ras-agent` → https://ras-agent.pages.dev/
- **License:** Apache 2.0
- **Attribution:** Glenn Heistand / CHAMP — Illinois State Water Survey
- **Status:** Phase 0+1 complete (commit `c4e0e26`), Phase 2 next
- **Cloudflare Git connection:** NOT YET DONE — Glenn needs to connect in dashboard (Workers & Pages → ras-agent → Settings → Build & Deployments → Connect to Git → gheistand/ras-agent, build cmd: `cd web && npm ci && npm run build`, output: `web/dist`)

---

## Purpose

Automate the full end-to-end 2D HEC-RAS hydraulic modeling workflow:
terrain ingestion → watershed delineation → peak flow estimation → hydrograph generation → mesh build → model execution → GIS results export.

Long-term goal: continuous automated modeling of all stream reaches in Illinois and beyond.

Users: H&H engineers. Built at CHAMP-ISWS. Open source from day one.

---

## IP / Legal Status

- **Open source (Apache 2.0)** — this significantly reduces university IP conflict risk vs. proprietary
- **OTM email drafted and ready to send** — Glenn should email otm@illinois.edu with project description before or just after starting development
- **Key decision:** Open source intent + ISWS attribution is the clean path; BaseFlood Engineering's value comes from expertise/reputation, not IP ownership
- **OTM contact:** otm@illinois.edu | 217-333-7862 | 319 Ceramics Building, 105 S. Goodwin, Urbana
- **Form to use if formal disclosure needed:** https://otm.illinois.edu/form/software-and-copyright-report
- **Bill Katzenmeyer** (CLB Engineering, RAS Commander) is a collaborator — Glenn has bi-weekly calls with him. No formal agreement. RAS Commander is also Apache 2.0 open source.

---

## Architecture

### Two-Layer Build Strategy
- **BaseBot (me):** Architect + orchestrator. Writes specs, reviews domain correctness, handles accounts/keys/deployments.
- **Claude Code (`claude --permission-mode bypassPermissions --print`):** Builder. Implements from specs, no PTY needed, commits and pushes, notifies when done.
- **Cost rationale:** Claude Code uses fresh context window per task; keeps BaseBot session lean.

### Execution Architecture
```
Windows (minimal):
  - RAS2025: 2D mesh generation → export .hdf (alpha, no Linux yet)
  - HEC-RAS 6.6 GUI: first run per watershed template → generates .p04.tmp.hdf, .b04, .x04

Linux (cloud-scalable, all simulation runs):
  - RasUnsteady: headless 2D unsteady flow compute engine ✅ (HEC-RAS 6.6 Linux build)
  - RasGeomPreprocess, RasSteady also available
  - All Python pipeline modules run here

Python (cross-platform):
  - terrain.py, watershed.py, streamstats.py, hydrograph.py (done)
  - model_builder.py, runner.py, results.py (planned)
  - FastAPI orchestrator + SQLite job queue (planned)

Web (Cloudflare Pages):
  - Vite + React + Tailwind dashboard (scaffold done)
  - Job submission, phase progress bars, results download
```

### HEC-RAS Linux Build Key Facts
- **Source:** https://www.hec.usace.army.mil/software/hec-ras/documentation/HEC-RAS_66_Linux_Build_Release_Notes.pdf
- **Executables:** `RasUnsteady`, `RasGeomPreprocess`, `RasSteady` — all headless CLI
- **2D unsteady flow:** ✅ fully supported on Linux
- **Invocation:** `RasUnsteady <plan_hdf_file> <geometry_extension>` (e.g., `Muncie.p04.tmp.hdf x04`)
- **Requires:** RHEL 8 compatible (Rocky, Alma, CentOS 8+); all libs bundled in zip; set `LD_LIBRARY_PATH`
- **NOT on Linux:** GUI, RAS Mapper, model creation — Windows still required for initial template setup
- **Workflow:** Build template on Windows once → copy .b04, .x04, .p04.tmp.hdf to Linux → run RasUnsteady → post-process HDF5 results with Python
- **Line endings:** Must dos2unix text files before Linux run
- **RAS2025 mesh import to 6.6:** Right-click mesh → Export → HEC-RAS Mesh (Version 6) → save .hdf → import in RAS Mapper. Imported meshes are read-only in 6.6; all edits go back through RAS2025.

---

## Tech Stack

### Python Pipeline
- `rasterio` — raster I/O, terrain processing, reprojection
- `pysheds` — D8 flow direction, accumulation, watershed delineation
- `gdal` — geospatial processing
- `geopandas` / `shapely` — vector operations
- `h5py` — HEC-RAS HDF5 results access
- `streamstats-access` — USGS StreamStats API client
- `ras-commander` — HEC-RAS 6.x automation (Bill Katzenmeyer / CLB Engineering)
- `numpy`, `scipy`, `pandas` — numerical
- `fastapi` + `uvicorn` — job orchestration API
- All CRS operations use **EPSG:5070** (NAD83 Albers Equal Area, meters) as pipeline-wide standard

### Web
- Vite + React + Tailwind CSS (v3)
- Cloudflare Pages hosting
- `web/wrangler.toml` — Pages config, build output `web/dist`

### CI/CD
- `.github/workflows/ci.yml` — runs pytest on pipeline + npm build on every push

---

## Data Sources

- **Terrain:** ILHMP LiDAR clearinghouse (Illinois) → https://clearinghouse.isgs.illinois.edu/data/elevation/illinois-height-modernization-ilhmp
  - REST tile index: `https://clearinghouse.isgs.illinois.edu/arcgis/rest/services/Elevation/IL_Height_Modernization_DEM/MapServer/0/query`
  - Fallback: USGS 3DEP 1/3 arc-second via National Map API
- **Hydrology:** USGS StreamStats REST API → https://streamstats.usgs.gov/streamstatsservices
  - Region code: `IL`
  - Fallback: Illinois regression equations (USGS SIR 2008-5176, Soong et al. 2008)
  - Returns Q2, Q5, Q10, Q25, Q50, Q100, Q500 in CFS
- **Land cover (Manning's n):** NLCD → https://www.mrlc.gov/
- **Stream gage data (optional):** USGS NWIS via `dataretrieval` library

---

## Phase Status

| Phase | Description | Status | Key Files |
|-------|-------------|--------|-----------|
| 0 | Foundation — repo, CI/CD, web scaffold | ✅ Done | README, LICENSE, .gitignore, ci.yml, wrangler.toml |
| 1 | Data pipeline | ✅ Done | terrain.py, watershed.py, streamstats.py, hydrograph.py |
| 2 | Model builder | 📋 Next | model_builder.py (RAS Commander) |
| 3 | Execution engine | 📋 Planned | runner.py, job queue |
| 4 | Results pipeline | 📋 Planned | results.py (h5py → GIS) |
| 5 | Web dashboard | 📋 Planned | App.jsx expansion, map viewer |

---

## Phase 1 Module Details

### terrain.py
- `get_terrain(bbox_wgs84, output_dir, resolution_m=3.0)` — full pipeline entry point
- `find_ilhmp_tiles(bbox_wgs84)` → tile metadata list; falls back to `find_usgs_3dep_tiles()`
- `download_tiles(tiles, output_dir)` → list of downloaded paths (idempotent)
- `mosaic_tiles(tile_paths, output_path, target_crs=EPSG:5070, resolution_m=3.0)` → merged/reprojected GeoTIFF
- `clip_to_watershed(dem_path, watershed_geom, output_path)` → 500m buffered clip
- Output: LZW-compressed, tiled GeoTIFF, EPSG:5070

### watershed.py
- `delineate_watershed(dem_path, pour_point_lon, pour_point_lat, snap_threshold_m=300, min_stream_area_km2=2.0)` → `WatershedResult`
- Uses pysheds Grid: fill_pits → fill_depressions → resolve_flats → flowdir (D8) → accumulation → catchment → polygonize
- `BasinCharacteristics`: drainage_area_km2/mi2, mean_elevation_m, relief_m, main_channel_length_km, main_channel_slope_m_per_m, centroid/pour_point lat/lon
- `save_watershed(result, output_dir)` → GeoPackage files (watershed_boundary.gpkg, stream_network.gpkg)

### streamstats.py
- `get_peak_flows(pour_point_lon, pour_point_lat, drainage_area_mi2, channel_slope_m_per_m, region='IL')` → `PeakFlowEstimates`
- Tries StreamStats API first (delineate workspace → get flow statistics → parse IL codes)
- Falls back to Illinois regression equations by latitude region (northern/central/southern)
- `PeakFlowEstimates`: Q2, Q5, Q10, Q25, Q50, Q100, Q500 in CFS; source field indicates API vs regression

### hydrograph.py
- `nrcs_unit_hydrograph(peak_flow_cfs, drainage_area_mi2, channel_length_km, channel_slope_m_per_m, time_step_hr=0.25, baseflow_cfs=0)` → `HydrographResult`
- Uses NRCS dimensionless UH (NEH Part 630, Ch 16, Table 16-1) — 30 tabulated t/Tp : q/qp pairs
- Kirpich Tc: `0.0195 * L^0.77 / S^0.385` (L in meters, result in minutes → hours)
- Lag time: Tlag = 0.6 * Tc; Time to peak: Tp = D/2 + Tlag; D = Tc for design storms
- `generate_hydrograph_set(peak_flows, channel_length_km, channel_slope_m_per_m)` → `HydrographSet` (all return periods)
- `save_hydrographs_csv()` and `save_hydrographs_hecras_dss_input()` for export

---

## Phase 2 Spec (model_builder.py) — REVISED after SimTheory feedback (2026-03-13)

### Critical Finding: RAS Commander Cannot Create Greenfield 2D Projects
RAS Commander (as of v0.52) assumes an EXISTING project/geometry. It cannot:
- Define new 2D flow areas from scratch
- Draw/generate a new computational mesh
- Write initial geometry HDF5 from nothing

It CAN (on existing projects):
- Clone projects and modify ASCII plan/geometry/flow files
- Update Manning's n, infiltration, boundary conditions
- Parse and extract 2D results (depths, velocities, WSE from HDF5)
- Manage and execute simulations

Source: SimTheory assistant analysis of RAS Commander docs/PyPI, 2026-03-13

### Three Mesh Strategy Paths

**Path A: Template + Clone (implement now)**
- Build 3 archetype HEC-RAS 2D template projects on Windows (small ~50 mi², medium ~200 mi², large ~800 mi² IL watershed)
- RAS Commander clones template → swaps terrain reference → updates BCs/hydrograph/Manning's n → writes plan
- Limitation: mesh perimeter/cell structure inherited from template — won't perfectly fit every watershed
- Status: implement in Phase 2 weekend build

**Path B: Direct HDF5 Construction (future)**
- Write HEC-RAS geometry HDF5 files directly with h5py
- True greenfield: define 2D flow area perimeter from watershed polygon, generate mesh cells programmatically
- Effort: 2-3 weeks focused work
- Status: planned after Path A is proven

**Path C: RAS2025 API (future)**
- RAS2025's new public API handles mesh generation programmatically
- Still alpha, no Linux build, API may change
- Status: 6-12 months out

### Interface Design (path-agnostic from day one)
```python
def build_model(watershed: WatershedResult,
                hydro_set: HydrographSet,
                mesh_strategy: str = "template_clone") -> HecRasProject:
    if mesh_strategy == "template_clone":
        return _build_from_template(watershed, hydro_set)
    elif mesh_strategy == "hdf5_direct":
        return _build_hdf5_direct(watershed, hydro_set)   # stub
    elif mesh_strategy == "ras2025":
        return _build_ras2025(watershed, hydro_set)        # stub
```

### Open Question (awaiting Bill Katzenmeyer's input)
Can RAS Commander update the 2D flow area perimeter polygon on a cloned project, or is the mesh geometry fixed at clone time? This determines how closely the template mesh needs to match each watershed's shape.

### Template Projects Needed (Glenn to build on Windows)
- `templates/small_watershed/`  — ~50 mi², low-relief IL agricultural
- `templates/medium_watershed/` — ~200 mi², mixed land cover
- `templates/large_watershed/`  — ~800 mi², river corridor

### What model_builder.py Does (Path A implementation)
1. Select closest template by drainage area
2. RAS Commander clones template to new project directory
3. Update terrain reference to point to watershed DEM (clipped GeoTIFF from terrain.py)
4. Update 2D flow area Manning's n from NLCD lookup table
5. Write unsteady flow file with hydrographs from hydrograph.py
6. Set downstream BC: normal depth (slope = main channel slope from watershed.py)
7. Update simulation time window (warm-up 12hr + hydrograph duration)
8. Return project path ready for runner.py

### Manning's n Table (NLCD classes → standard IL values)
- Open water: 0.035
- Developed low intensity: 0.080
- Developed medium intensity: 0.100
- Developed high intensity: 0.120
- Barren land: 0.030
- Deciduous/Evergreen/Mixed forest: 0.120
- Shrub/scrub: 0.060
- Grassland/herbaceous: 0.035
- Pasture/hay: 0.033
- Cultivated crops: 0.037
- Woody wetlands: 0.075
- Herbaceous wetlands: 0.075
- Default: 0.040

### Simulation Settings
- Computation interval: 10-30 sec (adaptive, based on mesh cell size)
- Mapping output interval: 1 hour
- Hydrograph output interval: 15 minutes
- Warm-up: 12 hours at baseflow before hydrograph start

---

## Phase 3 Spec (runner.py) — planned

- Job queue: SQLite-backed (joblib or simple custom), states: queued/running/complete/error
- Execute `RasUnsteady` on Linux via subprocess: `LD_LIBRARY_PATH=../libs:... RasUnsteady <plan.tmp.hdf> x01`
- Before run: dos2unix on .b## and .g## files; strip Results group from .hdf → create .tmp.hdf
- After run: rename .p##.tmp.hdf → .p##.hdf
- Parallel execution: multiple RasUnsteady processes for different return periods or watersheds
- Timeout: 4-hour hard limit per run; retry once on failure
- Progress tracking via HDF5 file size growth (proxy for simulation progress)

---

## Phase 4 Spec (results.py) — planned

- Read HEC-RAS output HDF5 via h5py
- Key HDF paths in HEC-RAS 6.x output:
  - `/Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/<name>/Depth`
  - `/Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/<name>/Water Surface`
  - `/Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/<name>/Velocity`
  - `/Geometry/2D Flow Areas/<name>/Cells Center Coordinate`
- Export max depth grid → GeoTIFF (Cloud-Optimized)
- Export flood extent polygon → Shapefile + GeoPackage
- Export velocity grid → GeoTIFF
- Store results to Cloudflare R2 (S3-compatible)
- pyHMT2D is a useful reference: https://github.com/psu-efd/pyHMT2D

---

## Key Decisions Made

1. **EPSG:5070** as universal CRS for all pipeline operations (NAD83 Albers, meters, equal area)
2. **NRCS DUH** for synthetic hydrograph generation (NEH Part 630, defensible for FEMA work)
3. **Illinois regression equations** (USGS SIR 2008-5176) as StreamStats fallback
4. **Kirpich Tc** formula for time of concentration
5. **500m buffer** around watershed when clipping DEM (model stability)
6. **3m resolution** as default DEM (1/3 arc-second ≈ 10m, but ILHMP LiDAR is 1-3m)
7. **Apache 2.0** license — compatible with RAS Commander, USACE tools
8. **Open source from day one** — reduces IP conflict, enables collaboration with Bill
9. **Linux for compute, Windows for mesh prep** — HEC-RAS Linux build confirmed for RasUnsteady
10. **Two-layer build:** BaseBot specs + Claude Code implements — keeps context lean, cheaper

---

## External Resources

- RAS Commander docs: https://ras-commander.readthedocs.io/
- RAS Commander repo: https://github.com/gpt-cmdr/ras-commander
- HEC-RAS 6.6 Linux release notes: https://www.hec.usace.army.mil/software/hec-ras/documentation/HEC-RAS_66_Linux_Build_Release_Notes.pdf
- HEC-RAS 2025: https://www.hec.usace.army.mil/software/hec-ras/2025/
- ILHMP clearinghouse: https://clearinghouse.isgs.illinois.edu/data/elevation/illinois-height-modernization-ilhmp
- USGS StreamStats: https://streamstats.usgs.gov
- USGS SIR 2008-5176 (IL regression equations): Soong et al. 2008
- pyHMT2D: https://github.com/psu-efd/pyHMT2D

---

## People

- **Glenn Heistand** — project lead, CHAMP section lead ISWS
- **Bill Katzenmeyer** — CLB Engineering, RAS Commander developer; bi-weekly calls with Glenn; potential collaborator
- **OTM** — U of I Office of Technology Management; Glenn to email otm@illinois.edu re: open-source disclosure

---

## Pending Actions

1. Connect Cloudflare Pages to GitHub (dashboard step — Glenn to do)
2. Send OTM email (Glenn to do — draft ready in session history)
3. Phase 2: spawn Claude Code → implement model_builder.py
4. Phase 3: runner.py + job queue
5. Phase 4: results.py (h5py → GIS)
6. Acquire HEC-RAS 6.6 Linux build and install on Mac/Linux for testing
7. Phase 5: web dashboard expansion (map viewer, real API connection)
