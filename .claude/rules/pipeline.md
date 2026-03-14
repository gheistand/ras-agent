---
description: Conventions for Python pipeline modules
globs: pipeline/**
---

# Pipeline Conventions

## Imports
- **Bare imports only:** `import terrain`, not `from pipeline import terrain`
- Modules assume `pipeline/` is on `sys.path`
- **Lazy imports in `api.py`:** Heavy modules (`runner`, `storage`, `report`, `notify`) are imported inside endpoint functions, not at module level

## CRS
- All geospatial operations use **EPSG:5070** (NAD83 Albers Equal Area, meters)
- Never hardcode other CRS values — reproject inputs to 5070 on ingestion

## Graceful Degradation
- `model_builder.py`: tries `ras-commander` → falls back to `shutil` + direct HDF5 writes
- `streamstats.py`: tries USGS API → falls back to IL regression equations
- Never hard-fail on optional dependencies — always provide a fallback path

## Mock Mode
- `runner.py` with `mock=True` creates fake HDF5 output
- All downstream code (results, report, api) handles mock output
- Tests always run in mock mode — no HEC-RAS installation required

## Error Handling
- Stages 1-2 (terrain, watershed): fatal → raise `OrchestratorError`
- Stages 3-7: non-fatal → return `status="partial"` with `errors` list
- **Logging:** `orchestrator.py`, `batch.py`, and `notify.py` use `loguru`; all other pipeline modules use stdlib `logging`. Don't mix within a module — match the existing pattern

## Data Types
- Use `@dataclass` for all result types (`WatershedResult`, `PeakFlowEstimates`, etc.)
- Type hints on all public functions

## Output Directory Structure
```
{output_dir}/
├── terrain/          # DEMs, NLCD
├── model/            # HEC-RAS project files
├── results/{rp}yr/   # Per-return-period outputs
├── logs/             # Run logs
├── jobs.db           # Per-run SQLite job tracking
└── report.html       # Self-contained HTML report
```

**Note:** The per-run `jobs.db` in `output_dir/` is separate from the global job queue at `JOBS_DB_PATH` (default: `data/jobs.db`).

## Copyright Header
All new Python files should include the Apache 2.0 copyright header matching existing files.
