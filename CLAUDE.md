# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

RAS Agent automates end-to-end 2D HEC-RAS hydraulic modeling: terrain ingestion, watershed delineation, peak flow estimation, hydrograph generation, model build, HEC-RAS execution, and GIS results export. Built at CHAMP (Illinois State Water Survey). Apache 2.0.

## Commands

### Python pipeline (from repo root)
```bash
python -m pytest tests/ -v                    # all 112 tests
python -m pytest tests/test_runner.py -v      # single module
python -m pytest tests/test_api.py::test_health -v  # single test

python pipeline/api.py                        # FastAPI on :8000
python pipeline/orchestrator.py --lon -88.578 --lat 40.021 --output ./output/test --mock
python pipeline/batch.py watersheds.csv output/ --workers 3 --mock
```

### Web dashboard (from `web/`)
```bash
npm ci && npm run dev     # Vite dev server on :5173
npm run build             # production build → web/dist/
npm run lint              # ESLint
```

### Docker
```bash
docker-compose up api                     # API server on :8000
docker-compose --profile dev up           # API + web dev server
docker-compose run --rm api python3 pipeline/orchestrator.py --lon -88.578 --lat 40.021 --output /app/output/test --mock
```

## Architecture

Two-part system: **Python pipeline** (`pipeline/`) + **React dashboard** (`web/`).

### Pipeline data flow (7 stages, all in `pipeline/`)
```
orchestrator.py  ← entry point, chains all stages
  ├─ terrain.py      → DEM download/mosaic (ILHMP LiDAR, fallback USGS 3DEP)
  ├─ watershed.py    → D8 delineation via pysheds → WatershedResult
  ├─ streamstats.py  → USGS StreamStats API / IL regression fallback → PeakFlowEstimates
  ├─ hydrograph.py   → NRCS DUH synthetic hydrographs → HydrographSet
  ├─ model_builder.py→ template clone + Manning's n + BCs → HecRasProject
  ├─ runner.py       → SQLite job queue + RasUnsteady execution
  └─ results.py      → HDF5 → COG GeoTIFF + GeoPackage + Shapefile
```

Supporting modules: `batch.py` (parallel multi-watershed), `report.py` (HTML reports), `notify.py` (webhook/email), `storage.py` (Cloudflare R2 upload).

### Web dashboard
React + Vite + Tailwind. `api.js` calls the FastAPI backend. `MapViewer.jsx` renders flood extents via MapLibre GL JS with per-return-period toggle layers. Deployed to Cloudflare Pages (auto-deploys from `main`).

## Key Conventions

- **CRS:** EPSG:5070 (NAD83 Albers Equal Area, meters) for all pipeline operations
- **Mock mode:** `ras_exe_dir=None` or `--mock` flag — all tests run without HEC-RAS installed
- **Imports:** Pipeline modules use bare imports (`import terrain`, not `from pipeline import terrain`). `sys.path.insert(0, pipeline/)` is used in tests.
- **Job DB:** SQLite at `data/jobs.db` (configurable via `JOBS_DB_PATH` env var), per-run DB at `output_dir/jobs.db` for orchestrator
- **R2 env vars:** `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, `R2_PUBLIC_URL`, `R2_PREFIX`
- **CI:** GitHub Actions runs `pytest` on `ubuntu-latest` with system GDAL, then `npm run build` for web

## Mesh Strategy

RAS Commander cannot create greenfield 2D projects. Current approach (Path A): clone template HEC-RAS projects, swap terrain/BCs/Manning's n. `model_builder.py` gracefully degrades if `ras-commander` is not installed (falls back to shutil + direct HDF5 writes).

See `docs/KNOWLEDGE.md` for detailed phase history, data source URLs, Manning's n table, and HEC-RAS Linux execution details.
