# RAS Agent 🌊

**Automated 2D HEC-RAS hydraulic modeling pipeline — terrain ingestion to finished flood maps.**

Built at the [Illinois State Water Survey (CHAMP Section)](https://isws.illinois.edu/champ) in collaboration with the [RAS Commander](https://github.com/gpt-cmdr/ras-commander) community.

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

**Windows is only required for:** RAS2025 mesh generation and initial HEC-RAS project template creation. All simulation execution and pre/post-processing runs on Linux.

---

## Status

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Foundation — repo, CI/CD, web scaffold | 🔨 In progress |
| 1 | Data pipeline — terrain, watershed, StreamStats, hydrograph | 🔨 In progress |
| 2 | Model builder — geometry, BCs, Manning's n | 📋 Planned |
| 3 | Execution engine — RasUnsteady, job queue, parallel runs | 📋 Planned |
| 4 | Results pipeline — HDF5 → GIS export | 📋 Planned |
| 5 | Web dashboard — job submission, map viewer, download portal | 📋 Planned |

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
- Mapbox GL JS / deck.gl — results map viewer
- Cloudflare Pages — hosting

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
