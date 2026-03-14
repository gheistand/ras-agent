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

## Human in the Loop (HITL)

The domain expert is the **operator** — the licensed engineer or hydrologist responsible for
the modeling results. On the reference deployment: Glenn Heistand, PE, CFM (CHAMP / ISWS).

**Always pause for:** Tc method selection, peak flow source conflicts (>±20%), Manning's n
for unmapped land cover, out-of-bounds outputs, FEMA compliance decisions.

**Proceed autonomously:** Bug fixes, refactoring, data ingestion, template-based model
generation, QAQC checks (run automatically; route findings to expert).

HITL routing uses `HITLConfig` — portable, channel-agnostic. Default: `blocking + stdin`
(zero-config for any user). Reference deployment: `blocking + telegram` (Glenn's instance).

See `.claude/rules/human-in-the-loop.md` for the full decision tree.
See `.claude/agents/expert-liaison/SUBAGENT.md` for routing and question format.

## Validation Bounds (Quick Reference)

All scientific outputs cross-checked against bounds. Full table in `.claude/rules/scientific-validation.md`.

| Parameter | Typical (IL) | HITL Trigger |
|-----------|-------------|--------------|
| Tc | 0.5–12 hr | < 0.25 or > 15 hr |
| Q100 unit peak flow | 50–150 csm | < 30 or > 800 csm |
| Manning's n | 0.030–0.140 | Outside safety bounds by NLCD class |
| Cell size | 15–200 m | < 10 or > 300 m |
| Max flood depth | 0.5–15 ft | < 0.1 or > 30 ft |
| FEMA depth accuracy | < 0.5 ft RMSE | > 0.5 ft |

## Mesh Strategy

RAS Commander cannot create greenfield 2D projects. Current approach (Path A): clone template HEC-RAS projects, swap terrain/BCs/Manning's n. `model_builder.py` gracefully degrades if `ras-commander` is not installed (falls back to shutil + direct HDF5 writes).

See `docs/KNOWLEDGE.md` for detailed phase history, data source URLs, Manning's n table, and HEC-RAS Linux execution details.
