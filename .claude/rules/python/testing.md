---
description: Conventions for the test suite
globs: tests/**
---

# Test Conventions

## Path Setup
Every test file begins with:
```python
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
```
This enables bare imports matching the pipeline convention.

## Test Baseline
- **125 tests** — this count must never decrease
- Adding new pipeline functionality requires corresponding tests
- Run `python -m pytest tests/ -v` to verify

## Mocking Strategy
- **All HTTP mocked:** StreamStats, terrain downloads, NLCD, webhooks — all use `unittest.mock.patch`
- **No HEC-RAS required:** `runner.py` mock mode creates fake HDF5 output
- **No shared conftest.py:** Each test file builds its own fixtures inline (e.g., `_make_watershed_result()`)
- **Temp DB per test:** API tests use FastAPI `TestClient` with `tmp_path` SQLite DB

## Test File Naming
- One test file per pipeline module: `test_{module}.py`
- API endpoint tests: `test_api.py`, `test_results_api.py`, `test_map_api.py`

## What to Test
- Happy path with representative inputs
- Edge cases (empty watershed, zero peak flow, missing optional data)
- Graceful degradation paths (API fallback, ras-commander unavailable)
- Error conditions that should raise `OrchestratorError`

## Running Tests
```bash
python -m pytest tests/ -v                          # all tests
python -m pytest tests/test_runner.py -v             # single file
python -m pytest tests/test_api.py::test_health -v   # single test
```
