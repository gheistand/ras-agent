# CLAUDE.md — tests/

415 tests, all pass without HEC-RAS or network access.

## Running Tests

```bash
python -m pytest tests/ -v                          # all tests
python -m pytest tests/test_runner.py -v             # single file
python -m pytest tests/test_api.py::test_health -v   # single test
python -m pytest tests/ -v --tb=short                # CI style
```

## Test Conventions

- **Path setup:** Every test file does `sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))` to enable bare imports.
- **No real network calls:** HTTP interactions are mocked (`unittest.mock.patch`). StreamStats, terrain tile downloads, NLCD — all mocked.
- **No HEC-RAS required:** `runner.py` mock mode creates fake HDF5 output. Results tests use synthetic HDF fixtures built with `h5py`.
- **Temp DB per test:** `test_api.py` uses FastAPI `TestClient` with a temp SQLite DB per test (`tmp_path`).
- **Fixtures:** Tests build their own fixtures inline (e.g., `_make_watershed_result()`, `_make_peak_flows()`). No shared conftest.py.

## Test Files → Pipeline Modules

| Test file | Tests for |
|-----------|-----------|
| `test_terrain.py` | terrain download, mosaic, NLCD |
| `test_hydrograph.py` | NRCS DUH, Kirpich Tc |
| `test_model_builder.py` | template selection, model build |
| `test_runner.py` | job queue, mock execution |
| `test_results.py` | HDF5 extraction, COG export |
| `test_orchestrator.py` | full pipeline (all stages mocked) |
| `test_api.py` | FastAPI endpoints |
| `test_results_api.py` | results/flood-extent/download endpoints |
| `test_map_api.py` | multi-return-period map endpoints |
| `test_batch.py` | batch mode, CSV input, resume |
| `test_report.py` | HTML report generation |
| `test_notify.py` | webhook + email notifications |
| `test_storage.py` | R2 upload, presigned URLs |
