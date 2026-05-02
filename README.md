# RAS Agent

**Automated 2D HEC-RAS hydraulic modeling pipeline — terrain ingestion to finished flood maps.**

Built at the [Illinois State Water Survey (CHAMP Section)](https://isws.illinois.edu/champ) in collaboration with [CLB Engineering Corporation](https://clbengineering.com) and the [RAS Commander](https://github.com/gpt-cmdr/ras-commander) community.

**216 tests passing · Docker confirmed working · All stages run end-to-end in mock mode**

---

## Current Integration Direction

- `ras-agent` should remain the Illinois-first orchestration layer and consume
  shared functionality from the latest published `ras-commander` and
  `hms-commander` pip packages rather than local sibling-repo branches.
- Mesh generation should be geometry-first and RASMapper-aligned through
  `ras-commander`; Cartesian mesh generation is not a fallback path for new
  work.
- Spring Creek is the immediate headwater pilot for BLE-style data generation,
  rain-on-grid AORC/MRMS setup, and gauge-based calibration/validation.
- Rain-on-grid boundary setup should be implemented first; HMS modeling should
  proceed in parallel, with HMS-linked boundary construction added once that
  path is complete enough to trust.
- Future calibration work should add precipitation-source QAQC for rain-on-grid
  events and reviewer-in-the-loop batched sensitivity runs that reserve
  high-resolution parameter exploration for the most influential parameters.

---

## Quick Start

### Option 1: Docker (recommended)

```bash
# Clone and start the API server
git clone https://github.com/gheistand/ras-agent
cd ras-agent
docker-compose up api

# In another terminal — run a test watershed in mock mode
docker-compose run --rm api python3 pipeline/orchestrator.py \
  --lon -88.578 --lat 40.021 --output /app/output/test --mock

# Open the dashboard
open https://ras-agent.pages.dev
```

### Option 2: Local (Python 3.11+)

```bash
# Install system deps (macOS)
brew install gdal geos proj

# Install Python deps
cd pipeline
pip install gdal==$(gdal-config --version)
pip install -r requirements.txt

# Run API server
python3 pipeline/api.py

# Run a test watershed
python3 pipeline/orchestrator.py --lon -88.578 --lat 40.021 --output ./output/test --mock
```

### Running Tests
```bash
python3 -m pytest tests/ -v
```

### SLURM (NCSA Illinois Computes Campus Cluster)

To submit HEC-RAS jobs to the Illinois Computes cluster, set these environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `SLURM_USER` | NetID for SSH to campus cluster | *(required)* |
| `SLURM_HOST` | Cluster login node | `cc-login.campuscluster.illinois.edu` |
| `SLURM_PARTITION` | SLURM partition | `IllinoisComputes` |
| `SLURM_ACCOUNT` | SLURM account | `heistand-ic` |
| `SLURM_SSH_KEY` | Path to SSH private key (optional) | *(system default)* |

All jobs default to local execution. SLURM submission requires `SLURM_USER` to be set.

---

## What It Does

RAS Agent automates the full HEC-RAS 2D modeling workflow:

1. **Terrain** — Downloads and mosaics LiDAR-derived GeoTIFFs from the [Illinois Height Modernization Program (ILHMP)](https://clearinghouse.isgs.illinois.edu/data/elevation/illinois-height-modernization-ilhmp) and other public sources
2. **Watershed** — Delineates watershed boundaries and stream networks from DEMs
3. **Hydrology** — Queries the [USGS StreamStats API](https://streamstats.usgs.gov) for peak flow estimates; generates synthetic inflow hydrographs using the NRCS Unit Hydrograph method
4. **Model Build** — Constructs HEC-RAS 6.6 input files (geometry, plan, unsteady flow, boundary conditions) using [RAS Commander](https://github.com/gpt-cmdr/ras-commander)
5. **Mesh Generation** — Uses geometry-first, RASMapper-aligned `ras-commander` workflows to write watershed-derived 2D flow area geometry, breaklines, and mesh instructions through authoritative `.g##` text geometry
6. **Linux Preprocessing** — Runs `ras_preprocess.py` ([hecras-v66-linux](https://github.com/neeraip/hecras-v66-linux)) to build hydraulic tables on Linux; verified 0.000–0.001 ft accuracy vs. GUI
7. **Execution** — Runs HEC-RAS 6.6 Linux compute engine (`RasUnsteady`) headlessly on NCSA Illinois Computes Campus Cluster (100K CPU-hours allocated); supports parallel multi-watershed execution
8. **Results** — Exports flood inundation extents, depth grids, and velocity rasters as shapefiles, GeoPackages, Cloud-Optimized GeoTIFFs, and **cloud-native GeoParquet archives** via [ras2cng](https://github.com/gpt-cmdr/ras2cng) for DuckDB analytics and PMTiles delivery

The long-term goal: continuous automated modeling of all stream reaches in Illinois and beyond, producing base-level 2D flood inundation models at scale.

---

## Architecture

### Linux Geometry Preprocessing (as of 2026-03)

Geometry preprocessing — building hydraulic tables (volume-elevation curves,
face area-elevation, Manning's *n*, infiltration, BC external faces) — now runs
entirely on Linux via `vendor/hecras-v66-linux/ras_preprocess.py`
([github.com/neeraip/hecras-v66-linux](https://github.com/neeraip/hecras-v66-linux),
verified 0.000–0.001 ft WSE accuracy vs. GUI on Muncie, BEC, VA models).

**Windows is now only required for initial mesh creation in RASMapper** (generating the `.g01.hdf` mesh topology file). All geometry compute, preprocessing, and simulation runs on Linux.

### SLURM / HPC Batch Execution (as of 2026-04)

Simulation jobs can be submitted to the **NCSA Illinois Computes Campus Cluster** via SLURM:

```python
from pipeline.slurm import SlurmConfig
from pipeline.orchestrator import run_watershed

slurm = SlurmConfig(user="yournetid")  # or set SLURM_USER env var
result = run_watershed(lon=-88.578, lat=40.021, output_dir="./output", slurm_config=slurm)
```

Jobs are submitted to `IllinoisComputes` partition under account `heistand-ic`. Results are rsynced back to local output directory on completion. Set `execution_mode="local"` (default) to run without SLURM.
Once a `.g01.hdf` mesh topology file exists, the full pipeline runs headlessly on Linux.

```
Windows (mesh creation only — one-time per project):
  pipeline/windows_agent.py
    → RASMapper: draw perimeter + seed points → g01.hdf

Linux (geometry compute + simulation — fully automated):
  runner.py (preprocess_mode='linux', the default)
    → ras_preprocess.py   builds hydraulic tables → p{N}.tmp.hdf
    → RasGeomPreprocess   adds internal solver index tables
    → RasUnsteady         runs the simulation
  Full Docker stack: docker-compose up api
```

**Three preprocessing workflows** (auto-detected by `ras_preprocess.py`):

| Workflow | When | Use case |
|----------|------|---------|
| **A** | `g01.hdf` fully computed + `p01.hdf` exists | Re-run an existing Windows/GUI project on Linux |
| **B** | `g01.hdf` (RASMapper mesh), no hydraulic tables | Standard ras-agent path |
| **C** | No `g01.hdf` | Build mesh from `.g01` seed points + full geometry compute |

### Legacy Two-Phase Windows → Linux Workflow

The old Windows preprocessing path (`preprocess_mode='windows'` or `'skip'`) is still
supported for projects where Windows preprocessing was already performed:

```
Windows (CHAMP Dell Precision 5860 — Intel Xeon W5-2545, 128GB DDR5):
  pipeline/windows_agent.py
    → RasPreprocess.preprocess_plan(plan_number)
    → returns .tmp.hdf + .b## + .x## files

Linux (Cloud VM — Rocky 8 / RHEL 8):
  runner.py (preprocess_mode='skip')
    → RasUnsteady headless compute engine (HEC-RAS 6.6 Linux)
```

### Full Pipeline Stack

```
┌─────────────────────────────────────────────────────────┐
│           ORCHESTRATOR (Linux / Cloud)                   │
│  FastAPI job queue · Cloudflare Pages dashboard          │
├──────────┬──────────┬─────────────┬─────────────────────┤
│ terrain  │watershed │ streamstats │  hydrograph          │
│  .py     │  .py     │    .py      │    .py               │
├──────────┴──────────┴─────────────┴─────────────────────┤
│          model_builder.py  (RAS Commander)               │
├─────────────────────────────────────────────────────────┤
│  windows_agent.py (RasPreprocess → .tmp.hdf + .b## + .x##)│
├─────────────────────────────────────────────────────────┤
│    runner.py  (HEC-RAS 6.6 Linux RasUnsteady)           │
├─────────────────────────────────────────────────────────┤
│         results.py  (h5py · rasterio · GDAL)            │
└─────────────────────────────────────────────────────────┘
```

---

## Status

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Foundation — repo, CI/CD, web scaffold | ✅ Done |
| 1 | Data pipeline — terrain (ILHMP/3DEP + NLCD), watershed delineation, StreamStats, NRCS hydrograph | ✅ Done |
| 2 | Model builder — template clone, .g## perimeter update, Manning's n, flow/plan ASCII files | ✅ Done |
| 3 | Execution engine — SQLite job queue, RasUnsteady subprocess, parallel runs, 4hr timeout | ✅ Done |
| 4 | Results pipeline — h5py HDF5 reader, max depth/WSE → COG GeoTIFF, flood extent → GeoPackage | ✅ Done |
| 5 | FastAPI backend — job CRUD, results endpoints, R2 presigned URLs, health/stats | ✅ Done |
| 6 | Orchestrator — `run_watershed()` chains all 7 stages; batch processor for multi-watershed runs | ✅ Done |
| 7 | Reporting — self-contained HTML report with hydrograph plots, basin stats, flood extent preview | ✅ Done |
| 8 | Web map viewer — MapLibre GL JS, OSM base tiles, toggleable 10/50/100yr flood extent layers | ✅ Done |
| 9 | Deployment — Docker + docker-compose, RAS Commander wiring (clone + Manning's n), webhook/email notifications | ✅ Done |
| 10 | Cloud storage — Cloudflare R2 results upload, presigned download URLs | ✅ Done |
| 11 | Perimeter writing — watershed boundary → .g## ASCII (HEC-RAS regenerates HDF on next open) | ✅ Done |
| HITL/QAQC | Human-in-the-loop + autonomous QA/QC — expert liaison, bounds validation, transparency logging | ✅ Done |
| Windows Agent | Windows mesh interface — RasPreprocess API for .tmp.hdf/.b##/.x## generation | ✅ Done |
| Linux Preprocessor | hecras-v66-linux integration — geometry compute on Linux, Windows dependency reduced to mesh creation only | ✅ Done |

**Pending (requires Windows + HEC-RAS GUI):**
- Build first HEC-RAS template project (Windows) for real (non-mock) runs
- Mesh regeneration automation after perimeter update through the geometry-first RASMapper path
- Download hecras-v66-linux git LFS binaries (`cd vendor/hecras-v66-linux && git lfs pull`) for real runs

---

## Dependencies

### Python Pipeline
- `rasterio` — raster I/O and terrain processing
- `pysheds` — DEM-based watershed delineation
- `gdal` — geospatial data processing
- `geopandas` — vector operations
- `h5py` — HEC-RAS HDF5 results access
- `streamstats-access` — USGS StreamStats API client
- `ras-commander` — HEC-RAS 6.x automation (Bill Katzenmeyer / CLB Engineering)
- `ras2cng` — cloud-native GIS post-processing (optional)
- `numpy`, `scipy`, `pandas` — numerical and data processing
- `fastapi` — job orchestration API

### Web Dashboard
- Vite + React + Tailwind CSS
- MapLibre GL JS (CDN) — flood extent map viewer with per-return-period toggle
- Cloudflare Pages — hosting + auto-deploy from `main`

### HEC Software (free, public domain)
- [HEC-RAS 6.6](https://www.hec.usace.army.mil/software/hec-ras/) — hydraulic simulation engine
- [HEC-RAS 6.6 Linux Build](https://www.hec.usace.army.mil/software/hec-ras/documentation/HEC-RAS_66_Linux_Build_Release_Notes.pdf) — headless compute
- [HEC-RAS 2025](https://www.hec.usace.army.mil/software/hec-ras/2025/) — 2D mesh generation

---

## Data Sources
- **Terrain:** [ILHMP LiDAR Clearinghouse](https://clearinghouse.isgs.illinois.edu/data/elevation/illinois-height-modernization-ilhmp) (Illinois Height Modernization Program)
- **Hydrology:** [USGS StreamStats](https://streamstats.usgs.gov) — peak flow regression equations (USGS SIR 2008-5176 for Illinois)
- **Land Cover:** [NLCD](https://www.mrlc.gov/) — Manning's n assignment

---

## Related Projects
- [RAS Commander](https://github.com/gpt-cmdr/ras-commander) — Python API for HEC-RAS automation (CLB Engineering / Bill Katzenmeyer)
- [pyHMT2D](https://github.com/psu-efd/pyHMT2D) — Python HEC-RAS 2D results processing
- [ras2cng](https://ras2cng.readthedocs.io) — HEC-RAS results → cloud-native GIS formats

---

## License

Apache License 2.0 — see [LICENSE](LICENSE)

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey

---

## Credits

### Glenn Heistand, P.E., C.F.M. — Illinois State Water Survey, CHAMP Section
Project lead, architecture, domain expertise.

### William Katzenmeyer, P.E., C.F.M. — CLB Engineering Corporation
Author of [RAS Commander](https://github.com/gpt-cmdr/ras-commander) (Apache 2.0) — the HEC-RAS automation library this project depends on. GitHub collaborator on ras-agent.

Key contributions to ras-agent:
- **PR #1 (2026-03-14):** Updated `model_builder.py` + `results.py` to RC 0.89+ APIs; added the full `.claude/` multi-agent infrastructure (6 specialist agents, 8 skills, 6 coding rules, 4 hooks)
- **RasPreprocess API (2026-03-17, commit 8c0c1c8):** Implemented `_generate_local()` in `windows_agent.py` using the public `RasPreprocess.preprocess_plan()` API — closes the Windows→Linux preprocessing gap
- **Two-phase Linux execution workflow design:** Windows preprocessing → Linux headless compute architecture
- **gui-subpackage branch (in progress):** GUI automation for RASMapper mesh generation

### Ajith Sundarraj — CLB Engineering Corporation
RASMapper automation development (gui-subpackage, in progress). Leading the Win32 API + mouse-click automation for RASMapper mesh generation operations.

### Additional Acknowledgments
- **pyHMT2D** (Xiaofeng Liu, Penn State) — HDF file handling patterns
- **FEMA-FFRD rashdf** — incorporated into HDF libraries
- **Sean Micek** — funkshuns, TXTure, RASmatazz utilities (via RAS Commander lineage)
- **Illinois State Water Survey, CHAMP Section** — institutional support
- **US Army Corps of Engineers, Hydrologic Engineering Center** — HEC-RAS software
- **Illinois State Geological Survey (ISGS)** — ILHMP terrain data
