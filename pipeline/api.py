"""
api.py — FastAPI HTTP interface for the RAS Agent job queue

Exposes the SQLite-backed job queue from runner.py via a REST API.
Designed to be called by the React dashboard (Cloudflare Pages) or any
HTTP client.  No HEC-RAS installation is required to run this service.

Endpoints:
    POST   /api/jobs                              Submit a new job
    GET    /api/jobs                              List jobs (optional ?status= filter)
    GET    /api/jobs/{job_id}                     Get a single job by ID
    PATCH  /api/jobs/{job_id}                     Update job fields (e.g. results_dir)
    DELETE /api/jobs/{job_id}                     Cancel/delete a queued job
    GET    /api/jobs/{job_id}/results             Results metadata and availability
    GET    /api/jobs/{job_id}/results/flood-extent  Flood extent GeoJSON
    GET    /api/jobs/{job_id}/results/depth-stats  Depth stats and output file list
    GET    /api/health                            Health check
    GET    /api/stats                             Queue summary statistics

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

import geopandas as gpd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULT_DB = _REPO_ROOT / "data" / "jobs.db"

VERSION = "0.1.0"

# Loaded at startup via _startup(); None if R2 env vars not set
R2_CONFIG = None


def _get_storage():
    """Lazy import of storage module (avoids hard boto3 dependency at load time)."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    import storage
    return storage


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
    r2_configured: bool


class HealthResponse(BaseModel):
    status: str
    version: str


class JobPatch(BaseModel):
    results_dir: Optional[str] = None


# ── Mock GeoJSON (used for jobs with results_dir containing "mock") ────────────

# ~5 km² square polygon centered on (-89.5, 40.0) in WGS84
# delta_lon = 0.024°, delta_lat = 0.022°  →  ~2.04 km × ~2.44 km ≈ 4.98 km²
_MOCK_FLOOD_POLYGON_COORDS = [[
    [-89.512, 39.989],
    [-89.488, 39.989],
    [-89.488, 40.011],
    [-89.512, 40.011],
    [-89.512, 39.989],
]]


# ── Results helpers ───────────────────────────────────────────────────────────

def _detect_return_periods(results_dir: Path) -> list[int]:
    """Scan results_dir for subdirectories named {n}yr and return sorted list."""
    rps = []
    if results_dir.is_dir():
        for d in results_dir.iterdir():
            if d.is_dir():
                m = re.match(r"^(\d+)yr$", d.name)
                if m:
                    rps.append(int(m.group(1)))
    return sorted(rps)


def _flood_extent_path(results_dir: Path, return_period: Optional[int]) -> Optional[Path]:
    """
    Resolve the flood_extent.gpkg path given a results_dir and optional return period.

    Checks return-period subdirectory first, then results_dir root.
    Returns the Path if the file exists, otherwise None.
    """
    candidates = []
    if return_period is not None:
        candidates.append(results_dir / f"{return_period}yr" / "flood_extent.gpkg")
    candidates.append(results_dir / "flood_extent.gpkg")
    for p in candidates:
        if p.exists():
            return p
    return None


def _output_files_list(results_dir: Path) -> list[str]:
    """Return names of known output files present in results_dir."""
    known = ["depth_grid.tif", "wse_grid.tif", "flood_extent.gpkg", "flood_extent.shp"]
    return [f for f in known if (results_dir / f).exists()]


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
    global R2_CONFIG
    runner = _get_runner()
    runner._init_db(_db_path())
    storage = _get_storage()
    R2_CONFIG = storage.r2_config_from_env()
    r2_status = "configured" if R2_CONFIG else "not configured"
    logger.info(f"RAS Agent API v{VERSION} started. DB: {_db_path()}. R2: {r2_status}")


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
        r2_configured=R2_CONFIG is not None,
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


@app.patch("/api/jobs/{job_id}", status_code=204)
def patch_job_endpoint(job_id: str, body: JobPatch):
    """Update job fields. Currently supports: results_dir."""
    runner = _get_runner()
    db = _db_path()
    job = runner.get_job(job_id, db_path=db)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if body.results_dir is not None:
        runner.update_job_results_dir(job_id, Path(body.results_dir), db_path=db)
        logger.info(f"PATCH job {job_id}: results_dir={body.results_dir}")
    # 204 No Content


@app.get("/api/jobs/{job_id}/results")
def get_job_results(job_id: str):
    """Return results metadata and file availability for a job."""
    runner = _get_runner()
    db = _db_path()
    job = runner.get_job(job_id, db_path=db)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    results_dir_str = job.get("results_dir")
    if not results_dir_str:
        return {
            "job_id": job_id,
            "results_dir": None,
            "flood_extent_available": False,
            "depth_grid_available": False,
            "return_periods": [],
        }

    results_dir = Path(results_dir_str)
    # Mock jobs: treat as available with sample data
    is_mock = "mock" in results_dir_str
    if is_mock:
        return {
            "job_id": job_id,
            "results_dir": results_dir_str,
            "flood_extent_available": True,
            "depth_grid_available": True,
            "return_periods": [],
        }

    return_periods = _detect_return_periods(results_dir)
    flood_available = _flood_extent_path(results_dir, None) is not None
    depth_available = (results_dir / "depth_grid.tif").exists()

    return {
        "job_id": job_id,
        "results_dir": results_dir_str,
        "flood_extent_available": flood_available,
        "depth_grid_available": depth_available,
        "return_periods": return_periods,
    }


@app.get("/api/jobs/{job_id}/results/flood-extent")
def get_flood_extent(
    job_id: str,
    return_period: Optional[int] = Query(None, description="Return period in years"),
):
    """
    Return flood extent as GeoJSON FeatureCollection.

    For mock jobs returns a sample polygon over central Illinois.
    For real jobs reads flood_extent.gpkg via geopandas.
    Returns 404 if flood extent data is not available.
    """
    runner = _get_runner()
    db = _db_path()
    job = runner.get_job(job_id, db_path=db)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    results_dir_str = job.get("results_dir")
    if not results_dir_str:
        raise HTTPException(status_code=404, detail="Flood extent not available for this job")

    rp = return_period or job.get("return_period_yr")

    # Mock jobs: return sample polygon
    if "mock" in results_dir_str:
        return {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": _MOCK_FLOOD_POLYGON_COORDS,
                },
                "properties": {
                    "return_period_yr": rp,
                    "job_id": job_id,
                    "job_name": job.get("name"),
                },
            }],
        }

    # Real jobs: read gpkg
    results_dir = Path(results_dir_str)
    gpkg_path = _flood_extent_path(results_dir, rp)
    if gpkg_path is None:
        raise HTTPException(status_code=404, detail="Flood extent not available for this job")

    try:
        gdf = gpd.read_file(str(gpkg_path))
    except Exception as exc:
        logger.error(f"Failed to read flood extent for job {job_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to read flood extent: {exc}")

    if gdf.empty:
        raise HTTPException(status_code=404, detail="Flood extent not available for this job")

    # Reproject to WGS84 for GeoJSON
    gdf_wgs84 = gdf.to_crs(epsg=4326)

    geojson = {
        "type": "FeatureCollection",
        "features": [],
    }
    for _, row in gdf_wgs84.iterrows():
        geojson["features"].append({
            "type": "Feature",
            "geometry": row.geometry.__geo_interface__,
            "properties": {
                "return_period_yr": rp,
                "job_id": job_id,
                "job_name": job.get("name"),
            },
        })
    return geojson


@app.get("/api/jobs/{job_id}/results/download/{filename}")
def download_result_file(job_id: str, filename: str):
    """
    Download a result file for a job.

    If R2 is configured, returns a 302 redirect to a presigned R2 URL.
    Otherwise streams the file directly from local disk.
    Returns 404 if the job or file is not found.
    """
    runner = _get_runner()
    db = _db_path()
    job = runner.get_job(job_id, db_path=db)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    results_dir_str = job.get("results_dir")
    if not results_dir_str:
        raise HTTPException(status_code=404, detail="No results directory for this job")

    results_dir = Path(results_dir_str)
    local_file = results_dir / filename

    if not local_file.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    if R2_CONFIG is not None:
        run_name = results_dir.name
        prefix = R2_CONFIG.prefix.rstrip("/")
        key = f"{prefix}/{run_name}/{filename}" if prefix else f"{run_name}/{filename}"
        try:
            storage = _get_storage()
            url = storage.get_presigned_url(key, R2_CONFIG)
            return RedirectResponse(url=url, status_code=302)
        except Exception as exc:
            logger.warning(f"R2 presigned URL failed for {filename}: {exc}; falling back to local")

    return FileResponse(path=str(local_file), filename=filename)


@app.get("/api/jobs/{job_id}/results/depth-stats")
def get_depth_stats(
    job_id: str,
    return_period: Optional[int] = Query(None, description="Return period in years"),
):
    """Return depth statistics and output file list for a job."""
    runner = _get_runner()
    db = _db_path()
    job = runner.get_job(job_id, db_path=db)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    rp = return_period or job.get("return_period_yr")
    results_dir_str = job.get("results_dir")

    # Mock jobs: return sample values
    if results_dir_str and "mock" in results_dir_str:
        return {
            "job_id": job_id,
            "return_period_yr": rp,
            "max_depth_m": 2.3,
            "flood_area_km2": 12.5,
            "output_files": ["depth_grid.tif", "wse_grid.tif", "flood_extent.gpkg", "flood_extent.shp"],
        }

    output_files = []
    if results_dir_str:
        output_files = _output_files_list(Path(results_dir_str))

    return {
        "job_id": job_id,
        "return_period_yr": rp,
        "max_depth_m": None,      # future: read from raster stats
        "flood_area_km2": None,   # future: compute from polygon area
        "output_files": output_files,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
