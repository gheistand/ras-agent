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
- **Status:** All phases complete (0–10b), 112/112 tests, 21 commits
- **Cloudflare Git connection:** ✅ Connected — ras-agent.pages.dev auto-deploys from main branch

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
| 2 | Model builder | ✅ Done | model_builder.py |
| 3 | Execution engine | ✅ Done | runner.py |
| 4 | Results pipeline | ✅ Done | results.py |
| 5 | FastAPI + live dashboard | ✅ Done | api.py, web/src/api.js, App.jsx |
| 6a | Orchestrator | ✅ Done | orchestrator.py |
| 6b | NLCD terrain layer | ✅ Done | terrain.py (get_nlcd, download_nlcd, reproject_nlcd, clip_nlcd_to_watershed) |
| 7a | Batch mode | ✅ Done | batch.py |
| 7b | HTML run report | ✅ Done | report.py |
| 8 | Web map viewer | ✅ Done | MapViewer.jsx, api.py results endpoints |
| 9a | Docker + docker-compose | ✅ Done | Dockerfile, docker-compose.yml, docker/ scripts |
| 9b | RAS Commander wiring | ✅ Done | model_builder.py (RC clone + Manning's n + HDF5 fallback) |
| 9c | Webhook/email notifications | ✅ Done | notify.py, orchestrator.py, batch.py |
| 10a | Cloudflare R2 storage | ✅ Done | storage.py, results.py, api.py |
| 10b | Multi-RP map layers | ✅ Done | MapViewer.jsx (toggle UI), api.py, api.js |

**Test count: 112/112 passing** (as of 2026-03-13)
**Latest commit:** `95ba892` — Phase 10b: multi-return-period map layers

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

## Phase 3 — runner.py (DONE)

- SQLite job queue at `data/jobs.db` — states: queued/running/complete/error
- `enqueue_job()`, `get_job()`, `list_jobs()`, `run_job()`, `run_queue()`
- Pre-run prep: dos2unix all .b##/.g## files; strip Results group from .hdf → .tmp.hdf
- Invocation: `LD_LIBRARY_PATH=../libs:../libs/mkl:../libs/rhel_8 RasUnsteady <plan.tmp.hdf> <geom_ext>`
- After success: rename .tmp.hdf → .hdf
- Parallel: `max_parallel` kwarg, runs multiple RasUnsteady processes
- **Mock mode:** `mock=True` skips real binary, creates fake output HDF → all tests pass without HEC-RAS
- Timeout: 4hr per job; retry once on failure
- 6 tests in `tests/test_runner.py`

---

## Phase 4 — results.py (DONE)

- `get_2d_area_names(hdf_path)` — lists 2D flow areas from HDF
- `extract_max_depth()`, `extract_max_wse()` — (cell_centers_xy, max_values) from HDF time series
- `cells_to_raster()` — scipy griddata interpolation → COG GeoTIFF (LZW, tiled 256x256, overviews)
- `extract_flood_extent()` — cells > threshold → union → GeoDataFrame polygon
- `export_results()` — full pipeline: depth_grid.tif, wse_grid.tif, flood_extent.gpkg, flood_extent.shp
- Key HDF paths confirmed in implementation:
  - `/Geometry/2D Flow Areas/<name>/Cells Center Coordinate`
  - `/Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/<name>/Depth`
  - `/Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/<name>/Water Surface`
- COG: BIGTIFF=IF_SAFER, nodata=-9999, overviews [2,4,8,16]
- 5 tests in `tests/test_results.py` (synthetic HDF fixture, no HEC-RAS needed)

---

## Phase 5 — FastAPI backend + live dashboard (DONE)

### pipeline/api.py
- FastAPI app with CORS middleware
- `POST /api/jobs` — submit job (returns 201)
- `GET /api/jobs` — list jobs (optional ?status= filter)
- `GET /api/jobs/{id}` — single job
- `DELETE /api/jobs/{id}` — cancel queued job
- `GET /api/stats` — {total, queued, running, complete, error}
- `GET /api/health` — {"status":"ok","version":"0.1.0"}
- DB path via `JOBS_DB_PATH` env var (default: `data/jobs.db`)
- Run: `python3 pipeline/api.py` → localhost:8000

### web/src/api.js
- Thin fetch-based client; `VITE_API_URL` env var (default: http://localhost:8000)
- `fetchJobs()`, `fetchJob()`, `submitJob()`, `deleteJob()`, `fetchStats()`, `fetchHealth()`

### web/src/App.jsx (updated)
- Polls jobs + stats every 10 seconds
- Health check every 30s → green/red dot in header
- 🗑️ delete button on queued jobs
- Error banner when API unreachable
- Form submits to real API

- 10 tests in `tests/test_api.py` (FastAPI TestClient, temp DB per test)

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

## Phase 6 — orchestrator.py (DONE, commit 3b19ffc)

- `run_watershed(pour_point_lon, pour_point_lat, output_dir, ...)` — chains all 7 stages
- `OrchestratorResult` dataclass with full provenance (terrain, watershed, peak_flows, hydro_set, project, job_ids, results, duration_sec, status, errors)
- Per-stage INFO logging: `[Stage N/7] ...`
- Graceful partial-failure: non-fatal errors append to `result.errors`, set `status="partial"`
- CLI: `python3 pipeline/orchestrator.py --lon X --lat Y --output DIR --mock`
- Per-run `jobs.db` at `output_dir/jobs.db` to avoid DB conflicts across runs
- 5 tests in `tests/test_orchestrator.py`

## Phase 6b — terrain.py NLCD layer (DONE, commit a06b466)

- `download_nlcd(bbox_wgs84, output_dir, year=2021)` — WCS request to MRLC, idempotent
- `reproject_nlcd(nlcd_path, target_crs, output_path, resampling="nearest")` — preserves uint8
- `clip_nlcd_to_watershed(nlcd_path, watershed_geom, output_path, buffer_m=500)` — rasterio mask
- `get_nlcd(bbox_wgs84, output_dir, watershed_geom, target_crs, year)` — full pipeline entry
- NLCD source: MRLC WCS `https://www.mrlc.gov/geoserver/mrlc_download/NLCD_2021_Land_Cover_L48/wcs`
- 3 tests in `tests/test_terrain.py` (mocked HTTP, no real network calls)

## Phase 7a — batch.py (DONE, commit d2d0c3f)
- `run_batch(input_file, output_dir, max_workers=3, resume=True, dry_run=False)`
- CSV or JSON watershed spec input; idempotent resume (skips completed runs)
- ThreadPoolExecutor parallel execution; per-watershed error isolation
- `run_metadata.json` written per run with full provenance + git commit
- `write_summary_csv()` — one row per watershed with drainage area, Q100, flood extent
- CLI: `python3 pipeline/batch.py watersheds.csv output/ --workers 3 --mock`
- 8 tests in `tests/test_batch.py`

## Phase 7b — report.py (DONE, commit ed269bc)
- `generate_report(result, output_path, include_plots=True)` → self-contained HTML
- Sections: header, basin characteristics, peak flow table, hydrograph plots, results summary, flood extent preview, data provenance, footer
- `_plot_hydrographs()` / `_plot_flood_extents()` → base64-encoded PNG inline
- No external dependencies (no Jinja2) — pure Python string formatting, inline CSS
- `orchestrator.run_watershed()` calls `generate_report()` by default (`write_report=True`)
- CHAMP/FEMA-quality: suitable for attaching to memos
- 6 tests in `tests/test_report.py`

## Phase 8 — Map Viewer (DONE, commit 3cc0002)
- `GET /api/jobs/{id}/results` — available result files + return periods
- `GET /api/jobs/{id}/results/flood-extent?return_period=100` → GeoJSON
- `GET /api/jobs/{id}/results/depth-stats` → max depth, flood area, file list
- `PATCH /api/jobs/{id}` → update results_dir
- `runner.py`: `results_dir TEXT` column + `update_job_results_dir()`
- `MapViewer.jsx`: MapLibre GL JS (CDN), OSM base tiles, flood extent overlay, fitBounds
- `App.jsx`: selectedJob state, JobCard click highlight, MapViewer below job list
- Mock jobs return sample Illinois polygon — demo-ready without HEC-RAS
- 4 tests in `tests/test_results_api.py`

## Phase 9a — Docker (DONE, commit 530e629)
- `Dockerfile`: python:3.11-slim + libgdal-dev + pipeline source; exposes port 8000
- `docker-compose.yml`: api service (port 8000, data/ + output/ volumes) + optional web dev service (profile=dev)
- `.dockerignore`: excludes data/, output/, HDF/GeoTIFF, tests/
- `docker/run-pipeline.sh` + `docker/run-batch.sh`: convenience wrappers
- `README.md`: Quick Start section with Docker and local options

## Phase 9b — RAS Commander wiring (DONE, commit 464dcc8)
- `_clone_project()`: tries RasPrj.clone_project(), falls back to shutil.copytree
- `_update_mannings_n()`: tries RasPrj.set_mannings_n()/update_mannings(), falls back to HDF5 direct
- `_update_mannings_n_hdf5()`: writes to `/Geometry/2D Flow Areas/{name}/Mann` dataset (col 1)
- `check_ras_commander()`: probes installation + capabilities dict
- All RC usage gracefully degrades — never hard-requires ras-commander

## Phase 9c — Notifications (DONE, commit f29a173)
- `notify.py`: `NotifyConfig` dataclass, `notify_run_complete()`, `notify_batch_complete()`
- Webhook: POST JSON payload; optional HMAC-SHA256 signature (X-RAS-Agent-Signature)
- Email: smtplib plain text with peak flows + output paths
- `orchestrator.run_watershed()`: `notify_config` param
- `batch.run_batch()`: per-watershed + batch-level notifications
- CLI: `--webhook` and `--notify-email` flags

## Phase 10a — R2 Storage (DONE, commit 1d0c6f3)
- `storage.py`: `R2Config`, `upload_file()`, `upload_results_dir()`, `get_presigned_url()`, `r2_config_from_env()`
- R2 endpoint: `https://{account_id}.r2.cloudflarestorage.com`
- `results.py`: optional `r2_config` param — upload after local write, warning-only on failure
- `api.py`: R2_CONFIG loaded at startup; `GET /api/jobs/{id}/results/download/{filename}` (presigned URL or FileResponse); `r2_configured` in /api/stats
- Env vars: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, R2_PUBLIC_URL, R2_PREFIX

## Phase 10b — Multi-RP Map Layers (DONE, commit 95ba892)
- `api.py`: flood-extent endpoint supports `?return_periods=all|10,50,100`; mock jobs return 3 features (10/50/100yr, nested polygon sizes)
- `api.js`: `fetchFloodExtent(jobId, returnPeriods="all")`
- `MapViewer.jsx`: per-RP MapLibre sources/layers; toggle legend panel (top-right checkboxes + color swatches); click popup; cursor change on hover; fitBounds to all visible layers

## Pending Actions

1. ~~Phases 2–10~~ ✅ All done (112/112 tests, 20 commits)
2. Send OTM email (Glenn — otm@illinois.edu from heistand@illinois.edu)
3. **Awaiting Bill K.:** Can RAS Commander update 2D flow area perimeter on cloned project?
4. **Glenn to build:** 3 template HEC-RAS projects on Windows (small/medium/large IL watershed)
5. **Docker smoke test:** Start Docker Desktop → `docker-compose up api` → run Muncie test case
6. **Cloud VM:** AWS/Azure x86 Linux for production-scale runs
7. **Next features (when ready):** FIRM validation, stream network batch from NHD, user auth for API

## CI Status
- ubuntu-24.04 runner requires: `apt-get install libgdal-dev gdal-bin libgeos-dev libproj-dev`
- Then: `pip install gdal==$(gdal-config --version)` before `pip install -r requirements.txt`
- CI workflow updated accordingly (commit `5243607`)
