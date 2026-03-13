"""
runner.py — HEC-RAS job queue and execution engine

Manages a SQLite-backed job queue for running HEC-RAS 6.6 Linux compute jobs.
Handles pre-run preparation (dos2unix, HDF Results strip), subprocess invocation
of RasUnsteady, parallel execution, 4-hour timeout, and single retry on failure.

Supports mock=True mode for full pipeline testing without a HEC-RAS installation.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import h5py

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULT_DB = _REPO_ROOT / "data" / "jobs.db"
_DEFAULT_LOGS = _REPO_ROOT / "data" / "logs"

JOB_TIMEOUT_SEC = 4 * 60 * 60    # 4-hour hard limit per job
RETRY_DELAY_SEC = 30              # wait between attempt 1 and attempt 2

# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    project_dir TEXT NOT NULL,
    plan_hdf TEXT NOT NULL,
    geom_ext TEXT NOT NULL DEFAULT 'g01',
    return_period_yr INTEGER,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error_msg TEXT,
    log_path TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);
"""


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a SQLite connection with row_factory set."""
    db_path = db_path or _DEFAULT_DB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(db_path: Optional[Path] = None) -> None:
    """Create the jobs table if it does not exist."""
    with _get_conn(db_path) as conn:
        conn.executescript(_DDL)


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row) if row else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Queue API ─────────────────────────────────────────────────────────────────

def enqueue_job(
    name: str,
    project_dir: str,
    plan_hdf: str,
    geom_ext: str = "g01",
    return_period_yr: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> str:
    """
    Insert a new job into the queue with status='queued'.

    Args:
        name:             Human-readable job name.
        project_dir:      Absolute path to the HEC-RAS project directory.
        plan_hdf:         Absolute path to the plan HDF file (e.g., Muncie.p04.hdf).
        geom_ext:         Geometry file extension (e.g., 'g01', 'x04').
        return_period_yr: Return period in years (2, 5, 10, 25, 50, 100, 500).
        db_path:          Override default DB location (useful for testing).

    Returns:
        job_id as a UUID4 string.
    """
    _init_db(db_path)
    job_id = str(uuid.uuid4())
    sql = """
        INSERT INTO jobs
            (id, name, project_dir, plan_hdf, geom_ext, return_period_yr,
             status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
    """
    with _get_conn(db_path) as conn:
        conn.execute(sql, (
            job_id, name, str(project_dir), str(plan_hdf),
            geom_ext, return_period_yr, _now_iso(),
        ))
        conn.commit()
    logger.info(f"Enqueued job {job_id} ({name})")
    return job_id


def get_job(job_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """
    Retrieve a job record by ID.

    Args:
        job_id:   UUID string of the job.
        db_path:  Override default DB location.

    Returns:
        Job dict or None if not found.
    """
    _init_db(db_path)
    with _get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_dict(row)


def list_jobs(
    status: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """
    List jobs, optionally filtered by status.

    Args:
        status:   Filter by status ('queued', 'running', 'complete', 'error').
                  Pass None to return all jobs.
        db_path:  Override default DB location.

    Returns:
        List of job dicts ordered by created_at ascending.
    """
    _init_db(db_path)
    with _get_conn(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at"
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _update_job(job_id: str, db_path: Optional[Path], **fields) -> None:
    """Update arbitrary columns on a job record."""
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    with _get_conn(db_path) as conn:
        conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", values)
        conn.commit()


# ── Pre-run Preparation ───────────────────────────────────────────────────────

def _dos2unix_dir(project_dir: Path) -> None:
    """
    Strip carriage returns (\\r) from all .b## and .g## text files in
    project_dir.  Done in-place using pure Python (no shell dependency).
    """
    import re
    pattern = re.compile(r'\.(b|g)\d+$', re.IGNORECASE)
    for fpath in project_dir.iterdir():
        if fpath.is_file() and pattern.search(fpath.name):
            try:
                raw = fpath.read_bytes()
                if b'\r' in raw:
                    fpath.write_bytes(raw.replace(b'\r\n', b'\n').replace(b'\r', b'\n'))
                    logger.debug(f"dos2unix: {fpath.name}")
            except (OSError, PermissionError) as exc:
                logger.warning(f"dos2unix skipped {fpath.name}: {exc}")


def _prepare_run(project_dir: Path, plan_hdf: Path) -> Path:
    """
    Prepare the project directory for a RasUnsteady run.

    1. dos2unix all .b## and .g## text files in project_dir.
    2. Copy plan_hdf → plan_hdf.stem + '.tmp.hdf', then delete the Results
       group from the copy so RasUnsteady starts with a clean slate.

    Args:
        project_dir: Path to the HEC-RAS project directory.
        plan_hdf:    Path to the plan HDF file (e.g., Muncie.p04.hdf).

    Returns:
        Path to the prepared .tmp.hdf file passed to RasUnsteady.

    Raises:
        FileNotFoundError: If plan_hdf does not exist.
        OSError:           On copy or HDF write failure.
    """
    _dos2unix_dir(project_dir)

    # Build .tmp.hdf path: Muncie.p04.hdf → Muncie.p04.tmp.hdf
    tmp_hdf = plan_hdf.with_suffix(".tmp.hdf")
    shutil.copy(str(plan_hdf), str(tmp_hdf))
    logger.debug(f"Copied plan HDF to {tmp_hdf.name}")

    with h5py.File(str(tmp_hdf), "a") as hf:
        if "Results" in hf:
            del hf["Results"]
            logger.debug("Stripped 'Results' group from tmp HDF")

    return tmp_hdf


# ── Mock Execution ────────────────────────────────────────────────────────────

def _run_mock(job: dict, logs_dir: Path) -> None:
    """
    Simulate a successful RasUnsteady run without the actual binary.

    Sleeps 2 seconds, writes a fake log, and creates a minimal HDF5 output
    at the expected results path (plan_hdf with /Results group populated).

    Args:
        job:      Job dict from the DB.
        logs_dir: Directory where the log file should be written.
    """
    job_id = job["id"]
    plan_hdf = Path(job["plan_hdf"])
    log_path = logs_dir / f"{job_id}.log"

    logs_dir.mkdir(parents=True, exist_ok=True)
    time.sleep(2)

    with open(log_path, "w") as fh:
        fh.write(f"[MOCK] RasUnsteady simulation — job {job_id}\n")
        fh.write(f"[MOCK] plan_hdf: {plan_hdf}\n")
        fh.write("[MOCK] Completed successfully.\n")

    # Create a minimal output HDF at the plan_hdf path
    plan_hdf.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(str(plan_hdf), "w") as hf:
        results = hf.create_group("Results")
        results.create_dataset("mock_wse", data=[1.0, 2.0, 3.0])
    logger.debug(f"Mock: created fake output HDF at {plan_hdf}")


# ── Job Execution ─────────────────────────────────────────────────────────────

def run_job(
    job_id: str,
    ras_exe_dir: Path,
    mock: bool = False,
    db_path: Optional[Path] = None,
    logs_dir: Optional[Path] = None,
) -> bool:
    """
    Execute a single queued job.

    Marks the job 'running', attempts execution up to twice (retry after 30s
    on first failure), then marks 'complete' or 'error'.

    In mock mode the actual RasUnsteady binary is never called; a simulated
    run is performed instead for testing without a HEC-RAS installation.

    Args:
        job_id:      UUID of the job to run.
        ras_exe_dir: Directory containing the RasUnsteady executable.
        mock:        If True, simulate execution without calling RasUnsteady.
        db_path:     Override default DB location.
        logs_dir:    Override default logs directory.

    Returns:
        True on success, False on error.

    Raises:
        ValueError:      If job_id is not found or job is not in 'queued' state.
        RuntimeError:    If RasUnsteady binary is not found (non-mock mode only).
    """
    _logs_dir = logs_dir or _DEFAULT_LOGS
    _logs_dir.mkdir(parents=True, exist_ok=True)

    job = get_job(job_id, db_path)
    if job is None:
        raise ValueError(f"Job not found: {job_id}")
    if job["status"] not in ("queued", "running"):
        raise ValueError(f"Job {job_id} has status '{job['status']}'; expected 'queued'")

    _update_job(job_id, db_path,
                status="running",
                started_at=_now_iso(),
                log_path=str(_logs_dir / f"{job_id}.log"))

    project_dir = Path(job["project_dir"])
    plan_hdf = Path(job["plan_hdf"])
    geom_ext = job["geom_ext"]

    for attempt in range(1, 3):  # attempts 1 and 2
        _update_job(job_id, db_path, attempts=attempt)
        logger.info(f"Job {job_id} attempt {attempt}/2 — {job['name']}")

        if mock:
            try:
                _run_mock(job, _logs_dir)
                _update_job(job_id, db_path,
                            status="complete",
                            completed_at=_now_iso())
                logger.info(f"Job {job_id} complete (mock)")
                return True
            except Exception as exc:
                err_msg = f"Mock execution failed: {exc}"
                logger.error(f"Job {job_id} mock error: {err_msg}")
                if attempt < 2:
                    logger.info(f"Retrying in {RETRY_DELAY_SEC}s…")
                    time.sleep(RETRY_DELAY_SEC)
                    continue
                _update_job(job_id, db_path,
                            status="error",
                            completed_at=_now_iso(),
                            error_msg=err_msg)
                return False
        else:
            # ── Real RasUnsteady execution ────────────────────────────────
            tmp_hdf: Optional[Path] = None
            try:
                tmp_hdf = _prepare_run(project_dir, plan_hdf)
            except Exception as exc:
                err_msg = f"Pre-run preparation failed: {exc}"
                logger.error(f"Job {job_id}: {err_msg}")
                if attempt < 2:
                    logger.info(f"Retrying in {RETRY_DELAY_SEC}s…")
                    time.sleep(RETRY_DELAY_SEC)
                    continue
                _update_job(job_id, db_path,
                            status="error",
                            completed_at=_now_iso(),
                            error_msg=err_msg)
                return False

            ras_exe = Path(ras_exe_dir) / "RasUnsteady"
            lib_base = Path(ras_exe_dir).parent / "libs"
            ld_path = (
                f"{lib_base}:"
                f"{lib_base / 'mkl'}:"
                f"{lib_base / 'rhel_8'}"
            )
            env = os.environ.copy()
            env["LD_LIBRARY_PATH"] = ld_path

            log_path = _logs_dir / f"{job_id}.log"
            success = False
            err_msg = ""

            try:
                with open(log_path, "w") as log_fh:
                    proc = subprocess.Popen(
                        [str(ras_exe), str(tmp_hdf), geom_ext],
                        stdout=log_fh,
                        stderr=subprocess.STDOUT,
                        env=env,
                    )
                try:
                    rc = proc.wait(timeout=JOB_TIMEOUT_SEC)
                    if rc == 0:
                        success = True
                    else:
                        tail = _read_log_tail(log_path, 500)
                        err_msg = f"RasUnsteady exited with code {rc}. Log tail: {tail}"
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    err_msg = "timeout after 4h"

            except FileNotFoundError:
                raise RuntimeError(
                    f"RasUnsteady binary not found at {ras_exe}. "
                    "Ensure HEC-RAS 6.6 Linux build is installed and ras_exe_dir is correct."
                )

            if success:
                # Rename .tmp.hdf → .hdf (overwrites original plan HDF)
                if tmp_hdf.exists():
                    shutil.move(str(tmp_hdf), str(plan_hdf))
                    logger.debug(f"Renamed {tmp_hdf.name} → {plan_hdf.name}")
                _update_job(job_id, db_path,
                            status="complete",
                            completed_at=_now_iso())
                logger.info(f"Job {job_id} complete")
                return True
            else:
                logger.error(f"Job {job_id} attempt {attempt} failed: {err_msg}")
                # Clean up tmp file on failure
                if tmp_hdf and tmp_hdf.exists():
                    tmp_hdf.unlink(missing_ok=True)
                if attempt < 2:
                    logger.info(f"Retrying in {RETRY_DELAY_SEC}s…")
                    time.sleep(RETRY_DELAY_SEC)
                    continue
                _update_job(job_id, db_path,
                            status="error",
                            completed_at=_now_iso(),
                            error_msg=err_msg)
                return False

    # Should not reach here, but guard anyway
    return False


def _read_log_tail(log_path: Path, max_chars: int) -> str:
    """Return the last max_chars characters of a log file."""
    try:
        text = log_path.read_text(errors="replace")
        return text[-max_chars:]
    except OSError:
        return "(log unreadable)"


# ── Queue Runner ──────────────────────────────────────────────────────────────

def run_queue(
    ras_exe_dir: Path,
    max_parallel: int = 2,
    mock: bool = False,
    db_path: Optional[Path] = None,
    logs_dir: Optional[Path] = None,
) -> None:
    """
    Process all queued jobs, running up to max_parallel concurrently.

    Loops until the queue is empty.  Uses a thread pool so multiple
    RasUnsteady processes can run simultaneously for different return periods
    or watersheds.

    Args:
        ras_exe_dir:  Directory containing the RasUnsteady executable.
        max_parallel: Maximum number of simultaneous RasUnsteady processes.
        mock:         If True, use mock execution (no HEC-RAS required).
        db_path:      Override default DB location.
        logs_dir:     Override default logs directory.
    """
    _init_db(db_path)
    logger.info(f"Starting queue runner (max_parallel={max_parallel}, mock={mock})")

    while True:
        queued = list_jobs(status="queued", db_path=db_path)
        if not queued:
            logger.info("Queue is empty — runner exiting.")
            break

        batch = queued[:max_parallel]
        logger.info(f"Dispatching {len(batch)} job(s) from queue ({len(queued)} total queued)")

        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures = {
                pool.submit(run_job, job["id"], ras_exe_dir, mock, db_path, logs_dir): job["id"]
                for job in batch
            }
            for future in as_completed(futures):
                jid = futures[future]
                try:
                    ok = future.result()
                    logger.info(f"Job {jid} finished: {'success' if ok else 'error'}")
                except RuntimeError as exc:
                    logger.error(f"Job {jid} raised RuntimeError: {exc}")
                except Exception as exc:
                    logger.error(f"Job {jid} raised unexpected exception: {exc}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_cli():
    try:
        import click
    except ImportError:
        return None

    @click.group()
    def cli():
        """HEC-RAS job queue manager."""

    @cli.command()
    @click.argument("name")
    @click.argument("project_dir", type=click.Path())
    @click.argument("plan_hdf", type=click.Path())
    @click.option("--geom-ext", default="g01", show_default=True)
    @click.option("--return-period", type=int, default=None)
    def enqueue(name, project_dir, plan_hdf, geom_ext, return_period):
        """Add a job to the queue."""
        jid = enqueue_job(name, project_dir, plan_hdf, geom_ext, return_period)
        click.echo(f"Enqueued job: {jid}")

    @cli.command("list")
    @click.option("--status", default=None)
    def list_cmd(status):
        """List jobs in the queue."""
        jobs = list_jobs(status)
        for j in jobs:
            click.echo(f"{j['id'][:8]}  {j['status']:10}  {j['name']}")

    @cli.command()
    @click.argument("job_id")
    @click.argument("ras_exe_dir", type=click.Path())
    @click.option("--mock", is_flag=True)
    def run(job_id, ras_exe_dir, mock):
        """Run a single job by ID."""
        ok = run_job(job_id, Path(ras_exe_dir), mock=mock)
        click.echo("success" if ok else "error")

    @cli.command("run-queue")
    @click.argument("ras_exe_dir", type=click.Path())
    @click.option("--max-parallel", default=2, show_default=True)
    @click.option("--mock", is_flag=True)
    def run_queue_cmd(ras_exe_dir, max_parallel, mock):
        """Process all queued jobs."""
        run_queue(Path(ras_exe_dir), max_parallel=max_parallel, mock=mock)

    return cli


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli = _build_cli()
    if cli:
        cli()
