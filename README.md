# RAS Agent 🌊

**Automated 2D HEC-RAS hydraulic modeling pipeline — terrain ingestion to finished flood maps.**

Built at the [Illinois State Water Survey (CHAMP Section)](https://isws.illinois.edu/champ) in collaboration with the [RAS Commander](https://github.com/gpt-cmdr/ras-commander) community.

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

---

## What It Does

RAS Agent automates the full HEC-RAS 2D modeling workflow:

1. **Terrain** — Downloads and mosaics LiDAR-derived GeoTIFFs from the [Illinois Height Modernization Program (ILHMP)](https://clearinghouse.isgs.illinois.edu/data/elevation/illinois-height-modernization-ilhmp) and other public sources
2. **Watershed** — Delineates watershed boundaries and stream networks from DEMs
3. **Hydrology** — Queries the [USGS StreamStats API](https://streamstats.usgs.gov) for peak flow estimates; generates synthetic inflow hydrographs using the NRCS Unit Hydrograph method
4. **Model Build** — Constructs HEC-RAS 6.6 input files (geometry, plan, unsteady flow, boundary conditions) using [RAS Commander](https://github.com/gpt-cmdr/ras-commander)
5. **Mesh Generation** — Integrates with [HEC-RAS 2025](https://www.hec.usace.army.mil/software/hec-ras/2025/) for 2D computational mesh creation
6. **Execution** — Runs HEC-RAS 6.6 Linux compute engine (`RasUnsteady`) headlessly; supports parallel multi-watershed execution
7. **Results** — Exports flood inundation extents, depth grids, and velocity rasters as shapefiles, GeoPackages, and Cloud-Optimized GeoTIFFs for ArcGIS Pro and QGIS

The long-term goal: continuous automated modeling of all stream reaches in Illinois and beyond, producing base-level 2D flood inundation models at scale.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│           ORCHESTRATOR (Linux / Cloud)           │
│  FastAPI job queue · Cloudflare Pages dashboard  │
├──────────┬──────────┬─────────────┬─────────────┤
│ terrain  │watershed │ streamstats │  hydrograph  │
│  .py     │  .py     │    .py      │    .py       │
├──────────┴──────────┴─────────────┴─────────────┤
│          model_builder.py  (RAS Commander)       │
├─────────────────────────────────────────────────┤
│    runner.py  (HEC-RAS 6.6 Linux RasUnsteady)   │
├─────────────────────────────────────────────────┤
│         results.py  (h5py · rasterio · GDAL)    │
└─────────────────────────────────────────────────┘
```

**Windows is only required for:** Initial HEC-RAS template project creation and mesh regeneration after perimeter update (RASMapper). All simulation execution and pre/post-processing runs on Linux or in Docker.

---

## Status

**117 tests passing · Docker confirmed working · All stages run end-to-end in mock mode**

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

**Pending (requires Windows + HEC-RAS GUI):**
- Build first HEC-RAS template project (Windows) for real (non-mock) runs
- Mesh regeneration automation after perimeter update (RASMapper — in development by CLB Engineering / Ajith Sundarraj)

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

---

## License

Apache License 2.0 — see [LICENSE](LICENSE)

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey

---

## Acknowledgments

Built with support from the [Illinois State Water Survey, CHAMP Section](https://isws.illinois.edu/champ).  
Computational framework informed by [RAS Commander](https://github.com/gpt-cmdr/ras-commander) (CLB Engineering Corporation).  
HEC-RAS software by the [US Army Corps of Engineers Hydrologic Engineering Center](https://www.hec.usace.army.mil/).  
Terrain data from the [Illinois State Geological Survey (ISGS)](https://isgs.illinois.edu/) ILHMP clearinghouse.
