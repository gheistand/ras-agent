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
| `model_builder.py` | Template clone + RC wiring + HDF5 fallback | `HecRasProject`, `build_model()` |
| `runner.py` | SQLite job queue + RasUnsteady invocation | `enqueue_job()`, `run_queue()` |
| `results.py` | HDF5 → raster/vector export | `export_results()` |
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

Results are read from these HDF5 groups:
- `/Geometry/2D Flow Areas/<name>/Cells Center Coordinate`
- `/Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/<name>/Depth`
- `/Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/<name>/Water Surface`

## Dependencies

Requires system GDAL (`libgdal-dev`). Install order matters:
```bash
pip install gdal==$(gdal-config --version)   # must match system GDAL
pip install -r requirements.txt
```
