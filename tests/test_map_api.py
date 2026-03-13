"""
Tests for multi-return-period flood extent API endpoint (Phase 10b).

Verifies that GET /api/jobs/{id}/results/flood-extent supports the
?return_periods=all and ?return_periods=10,50,100 query parameters and
that mock jobs return the expected three-feature FeatureCollection.
"""

import os
import sys
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
    "name": "Test River 100yr",
    "project_dir": "/tmp/test_project",
    "plan_hdf": "test.p01.hdf",
    "geom_ext": "g01",
    "return_period_yr": 100,
}

_MOCK_RESULTS_DIR = "/tmp/mock_results/map_test"


def _submit_mock_job(client):
    """Submit a job and patch it with a mock results_dir."""
    res = client.post("/api/jobs", json=_JOB_BODY)
    assert res.status_code == 201
    job_id = res.json()["id"]
    patch = client.patch(f"/api/jobs/{job_id}", json={"results_dir": _MOCK_RESULTS_DIR})
    assert patch.status_code == 204
    return job_id


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_flood_extent_all_return_periods(client):
    """GET ?return_periods=all returns FeatureCollection with multiple features."""
    job_id = _submit_mock_job(client)
    res = client.get(f"/api/jobs/{job_id}/results/flood-extent?return_periods=all")
    assert res.status_code == 200
    data = res.json()
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) > 1
    # Each feature must carry a return_period_yr property
    for feat in data["features"]:
        assert "return_period_yr" in feat["properties"]
    # Return periods should be unique
    rps = [f["properties"]["return_period_yr"] for f in data["features"]]
    assert len(set(rps)) == len(rps)


def test_flood_extent_single_return_period(client):
    """GET ?return_periods=100 returns only the 100yr feature."""
    job_id = _submit_mock_job(client)
    res = client.get(f"/api/jobs/{job_id}/results/flood-extent?return_periods=100")
    assert res.status_code == 200
    data = res.json()
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 1
    assert data["features"][0]["properties"]["return_period_yr"] == 100


def test_mock_job_returns_three_features(client):
    """Default call to a mock job returns exactly 3 features (10, 50, 100yr)."""
    job_id = _submit_mock_job(client)
    res = client.get(f"/api/jobs/{job_id}/results/flood-extent")
    assert res.status_code == 200
    data = res.json()
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 3
    rps = sorted(f["properties"]["return_period_yr"] for f in data["features"])
    assert rps == [10, 50, 100]
    # Polygons should be different sizes (100yr largest)
    def _bbox_area(feat):
        coords = feat["geometry"]["coordinates"][0]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        return (max(lons) - min(lons)) * (max(lats) - min(lats))
    areas = {f["properties"]["return_period_yr"]: _bbox_area(f) for f in data["features"]}
    assert areas[10] < areas[50] < areas[100]
