"""
api.py — FastAPI HTTP interface for the RAS Agent job queue

Exposes the SQLite-backed job queue from runner.py via a REST API.
Designed to be called by the React dashboard (Cloudflare Pages) or any
HTTP client.  No HEC-RAS installation is required to run this service.

Endpoints:
    POST   /api/jobs              Submit a new job
    GET    /api/jobs              List jobs (optional ?status= filter)
    GET    /api/jobs/{job_id}     Get a single job by ID
    DELETE /api/jobs/{job_id}     Cancel/delete a queued job
    GET    /api/health            Health check
    GET    /api/stats             Queue summary statistics

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULT_DB = _REPO_ROOT / "data" / "jobs.db"

VERSION = "0.1.0"


def _db_path() -> Path:
    """Return the active DB path from env or default."""
    env_val = os.environ.get("JOBS_DB_PATH")
    if env_val:
        return Path(env_val)
    return _DEFAULT_DB


# ── Lazy runner imports (avoid importing h5py at module load time) ────────────

def _get_runner():
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    import runner
    return runner


# ── Pydantic models ───────────────────────────────────────────────────────────

class JobSubmit(BaseModel):
    name: str = Field(..., description="Human-readable job name")
    project_dir: str = Field(..., description="Absolute path to HEC-RAS project directory")
    plan_hdf: str = Field(..., description="Filename of plan HDF (e.g., project.p01.hdf)")
    geom_ext: str = Field("g01", description="Geometry file extension (e.g., g01, x04)")
    return_period_yr: Optional[int] = Field(None, description="Return period in years")


class JobResponse(BaseModel):
    id: str
    name: str
    project_dir: str
    plan_hdf: str
    geom_ext: str
    return_period_yr: Optional[int]
    status: str
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    error_msg: Optional[str]
    log_path: Optional[str]


class StatsResponse(BaseModel):
    total: int
    queued: int
    running: int
    complete: int
    error: int


class HealthResponse(BaseModel):
    status: str
    version: str


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="RAS Agent API",
    description="Job queue for automated HEC-RAS 2D modeling",
    version=VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    """Ensure the jobs table exists on startup."""
    runner = _get_runner()
    runner._init_db(_db_path())
    logger.info(f"RAS Agent API v{VERSION} started. DB: {_db_path()}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _job_to_response(job: dict) -> JobResponse:
    """Convert a runner job dict to a JobResponse, stripping internal fields."""
    return JobResponse(
        id=job["id"],
        name=job["name"],
        project_dir=job["project_dir"],
        plan_hdf=job["plan_hdf"],
        geom_ext=job["geom_ext"],
        return_period_yr=job.get("return_period_yr"),
        status=job["status"],
        created_at=job["created_at"],
        started_at=job.get("started_at"),
        completed_at=job.get("completed_at"),
        error_msg=job.get("error_msg"),
        log_path=job.get("log_path"),
    )


def _delete_queued_job(job_id: str, db: Path) -> bool:
    """
    Delete a job record only if it is in 'queued' status.

    Returns True on success, False if job not found or not queued.
    """
    runner = _get_runner()
    runner._init_db(db)
    conn = runner._get_conn(db)
    try:
        cur = conn.execute(
            "SELECT status FROM jobs WHERE id = ?", (job_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None          # not found
        if row["status"] != "queued":
            return False         # wrong status
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()
        return True
    finally:
        conn.close()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/health", response_model=HealthResponse)
def health():
    """Health check — always returns 200 if the server is running."""
    return HealthResponse(status="ok", version=VERSION)


@app.get("/api/stats", response_model=StatsResponse)
def stats():
    """Return job queue summary statistics."""
    runner = _get_runner()
    db = _db_path()
    all_jobs = runner.list_jobs(db_path=db)
    counts = {"queued": 0, "running": 0, "complete": 0, "error": 0}
    for job in all_jobs:
        s = job["status"]
        if s in counts:
            counts[s] += 1
    return StatsResponse(
        total=len(all_jobs),
        queued=counts["queued"],
        running=counts["running"],
        complete=counts["complete"],
        error=counts["error"],
    )


@app.post("/api/jobs", response_model=JobResponse, status_code=201)
def submit_job(body: JobSubmit):
    """Enqueue a new HEC-RAS job."""
    runner = _get_runner()
    db = _db_path()
    try:
        job_id = runner.enqueue_job(
            name=body.name,
            project_dir=body.project_dir,
            plan_hdf=body.plan_hdf,
            geom_ext=body.geom_ext,
            return_period_yr=body.return_period_yr,
            db_path=db,
        )
    except Exception as exc:
        logger.error(f"Failed to enqueue job: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to enqueue job: {exc}")

    job = runner.get_job(job_id, db_path=db)
    return _job_to_response(job)


@app.get("/api/jobs", response_model=list[JobResponse])
def list_jobs_endpoint(status: Optional[str] = Query(None)):
    """List all jobs, optionally filtered by status."""
    runner = _get_runner()
    db = _db_path()
    jobs = runner.list_jobs(status=status, db_path=db)
    return [_job_to_response(j) for j in jobs]


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job_endpoint(job_id: str):
    """Get a single job by ID."""
    runner = _get_runner()
    db = _db_path()
    job = runner.get_job(job_id, db_path=db)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return _job_to_response(job)


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job_endpoint(job_id: str):
    """
    Cancel and delete a queued job.

    Only jobs with status='queued' may be deleted.
    Returns 404 if the job does not exist, 409 if it is not queued.
    """
    db = _db_path()
    result = _delete_queued_job(job_id, db)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if result is False:
        raise HTTPException(
            status_code=409,
            detail="Only queued jobs can be deleted. This job is already running or finished.",
        )
    # 204 No Content — no response body


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
