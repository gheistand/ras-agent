"""
Tests for results-related API endpoints in api.py.

Covers GET /api/jobs/{id}/results, /flood-extent, /depth-stats, and PATCH /api/jobs/{id}.
Uses FastAPI TestClient with a temporary SQLite DB for full isolation.
No HEC-RAS installation or real results files are required.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Point JOBS_DB_PATH at a fresh temp DB for every test."""
    db = tmp_path / "test_jobs.db"
    monkeypatch.setenv("JOBS_DB_PATH", str(db))
    return db


@pytest.fixture()
def client(tmp_db):
    """Return a TestClient backed by a fresh temp DB."""
    from fastapi.testclient import TestClient
    import importlib
    import api as api_module
    importlib.reload(api_module)
    return TestClient(api_module.app)


_JOB_BODY = {
    "name": "Sangamon River 100yr",
    "project_dir": "/tmp/test_project",
    "plan_hdf": "test.p01.hdf",
    "geom_ext": "g01",
    "return_period_yr": 100,
}


def _submit_job(client):
    """Submit a job and return the job dict."""
    res = client.post("/api/jobs", json=_JOB_BODY)
    assert res.status_code == 201
    return res.json()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_results_endpoint_no_results_dir(client):
    """GET /results on a job with no results_dir → flood_extent_available=False."""
    job = _submit_job(client)
    job_id = job["id"]

    res = client.get(f"/api/jobs/{job_id}/results")
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"] == job_id
    assert data["results_dir"] is None
    assert data["flood_extent_available"] is False
    assert data["depth_grid_available"] is False
    assert data["return_periods"] == []


def test_flood_extent_not_available(client):
    """GET /results/flood-extent returns 404 when no results_dir is set."""
    job = _submit_job(client)
    job_id = job["id"]

    res = client.get(f"/api/jobs/{job_id}/results/flood-extent")
    assert res.status_code == 404
    assert "not available" in res.json()["detail"].lower()


def test_depth_stats_returns_structure(client):
    """GET /results/depth-stats returns expected JSON keys."""
    job = _submit_job(client)
    job_id = job["id"]

    res = client.get(f"/api/jobs/{job_id}/results/depth-stats")
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"] == job_id
    assert "return_period_yr" in data
    assert "max_depth_m" in data
    assert "flood_area_km2" in data
    assert "output_files" in data
    assert isinstance(data["output_files"], list)


def test_patch_job_results_dir(client):
    """PATCH /api/jobs/{id} updates results_dir; GET /results reflects it."""
    job = _submit_job(client)
    job_id = job["id"]

    # PATCH to set a mock results_dir
    mock_dir = f"/tmp/mock_results/{job_id}"
    patch_res = client.patch(f"/api/jobs/{job_id}", json={"results_dir": mock_dir})
    assert patch_res.status_code == 204

    # GET /results should now show results_dir set and mock data available
    results_res = client.get(f"/api/jobs/{job_id}/results")
    assert results_res.status_code == 200
    data = results_res.json()
    assert Path(data["results_dir"]) == Path(mock_dir)
    assert data["flood_extent_available"] is True   # mock path contains "mock"

    # GET /flood-extent should return sample GeoJSON for mock path.
    # Default (return_periods=all) now returns 3 features (10, 50, 100yr).
    extent_res = client.get(f"/api/jobs/{job_id}/results/flood-extent")
    assert extent_res.status_code == 200
    geojson = extent_res.json()
    assert geojson["type"] == "FeatureCollection"
    assert len(geojson["features"]) == 3
    feat = geojson["features"][0]
    assert feat["geometry"]["type"] == "Polygon"
    assert feat["properties"]["job_id"] == job_id
    assert feat["properties"]["job_name"] == _JOB_BODY["name"]
