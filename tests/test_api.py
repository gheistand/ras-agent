"""
Tests for api.py — FastAPI job queue HTTP interface

Uses FastAPI TestClient with a temporary SQLite DB for full isolation.
No HEC-RAS installation is required.
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
    # Import after env var is set so _db_path() picks up the override
    from fastapi.testclient import TestClient
    import importlib
    import api as api_module
    importlib.reload(api_module)          # ensure startup event re-runs with new DB path
    return TestClient(api_module.app)


_JOB_BODY = {
    "name": "Test River — 100yr",
    "project_dir": "/tmp/test_project",
    "plan_hdf": "test.p01.hdf",
    "geom_ext": "g01",
    "return_period_yr": 100,
}


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "version" in data


def test_stats_empty(client):
    res = client.get("/api/stats")
    assert res.status_code == 200
    data = res.json()
    for key in ("total", "queued", "running", "complete", "error"):
        assert key in data
    assert data["total"] == 0


def test_submit_job(client):
    res = client.post("/api/jobs", json=_JOB_BODY)
    assert res.status_code == 201
    data = res.json()
    assert data["name"] == _JOB_BODY["name"]
    assert data["status"] == "queued"
    assert data["return_period_yr"] == 100
    assert "id" in data
    assert data["started_at"] is None
    assert data["completed_at"] is None
    assert data["error_msg"] is None


def test_list_jobs(client):
    # Submit a job first
    create_res = client.post("/api/jobs", json=_JOB_BODY)
    job_id = create_res.json()["id"]

    res = client.get("/api/jobs")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert any(j["id"] == job_id for j in data)


def test_get_job(client):
    create_res = client.post("/api/jobs", json=_JOB_BODY)
    job_id = create_res.json()["id"]

    res = client.get(f"/api/jobs/{job_id}")
    assert res.status_code == 200
    assert res.json()["id"] == job_id


def test_get_job_not_found(client):
    res = client.get("/api/jobs/nonexistent-id")
    assert res.status_code == 404


def test_delete_queued_job(client):
    create_res = client.post("/api/jobs", json=_JOB_BODY)
    job_id = create_res.json()["id"]

    del_res = client.delete(f"/api/jobs/{job_id}")
    assert del_res.status_code == 204

    # Should now be gone
    get_res = client.get(f"/api/jobs/{job_id}")
    assert get_res.status_code == 404


def test_delete_nonexistent_job(client):
    res = client.delete("/api/jobs/does-not-exist")
    assert res.status_code == 404


def test_filter_by_status(client):
    # Submit two jobs
    client.post("/api/jobs", json=_JOB_BODY)
    client.post("/api/jobs", json={**_JOB_BODY, "name": "Second job"})

    res = client.get("/api/jobs?status=queued")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2
    assert all(j["status"] == "queued" for j in data)

    # Filter for running — should be empty
    res2 = client.get("/api/jobs?status=running")
    assert res2.status_code == 200
    assert res2.json() == []


def test_stats_after_submit(client):
    client.post("/api/jobs", json=_JOB_BODY)
    client.post("/api/jobs", json={**_JOB_BODY, "name": "Second job"})

    res = client.get("/api/stats")
    data = res.json()
    assert data["total"] == 2
    assert data["queued"] == 2
    assert data["running"] == 0
