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
import re
import shutil
import sqlite3
import subprocess
import sys
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
_VENDOR_ROOT = _REPO_ROOT / "vendor" / "hecras-v66-linux"
_RAS_PREPROCESS = _VENDOR_ROOT / "ras_preprocess.py"

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
    attempts INTEGER NOT NULL DEFAULT 0,
    results_dir TEXT,         -- set after results.export_results() completes
    preprocess_mode TEXT NOT NULL DEFAULT 'linux'  -- 'linux', 'windows', or 'skip'
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
    """Create the jobs table if it does not exist, and migrate schema."""
    with _get_conn(db_path) as conn:
        conn.executescript(_DDL)
        # Migrate: add columns to existing databases
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "results_dir" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN results_dir TEXT")
            conn.commit()
        if "preprocess_mode" not in cols:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN preprocess_mode TEXT NOT NULL DEFAULT 'linux'"
            )
            conn.commit()


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
    preprocess_mode: str = "linux",
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
        preprocess_mode:  Geometry preprocessing mode:
                          ``'linux'``   — run ras_preprocess.py (vendor/hecras-v66-linux)
                                          to compute hydraulic tables on Linux (default);
                          ``'windows'`` — geometry was preprocessed by windows_agent.py
                                          (RasPreprocess on Windows), skip recompute;
                          ``'skip'``    — geometry tables already present, just strip
                                          Results and run RasUnsteady directly.

    Returns:
        job_id as a UUID4 string.
    """
    _init_db(db_path)
    job_id = str(uuid.uuid4())
    sql = """
        INSERT INTO jobs
            (id, name, project_dir, plan_hdf, geom_ext, return_period_yr,
             status, created_at, preprocess_mode)
        VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
    """
    with _get_conn(db_path) as conn:
        conn.execute(sql, (
            job_id, name, str(project_dir), str(plan_hdf),
            geom_ext, return_period_yr, _now_iso(), preprocess_mode,
        ))
        conn.commit()
    logger.info(f"Enqueued job {job_id} ({name}, preprocess={preprocess_mode})")
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


def update_job_results_dir(
    job_id: str,
    results_dir: Path,
    db_path: Optional[Path] = None,
) -> None:
    """Store the results directory path on the job record after export."""
    _update_job(job_id, db_path, results_dir=str(results_dir))
    logger.info(f"Job {job_id}: results_dir set to {results_dir}")


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


# ── Linux Preprocessing ───────────────────────────────────────────────────────

def _extract_plan_number(plan_hdf: Path) -> str:
    """Extract the plan number digits from a plan HDF filename.

    Args:
        plan_hdf: Path like ``Muncie.p04.hdf``.

    Returns:
        Plan number string, e.g. ``'04'``.  Falls back to ``'01'`` if the
        filename does not match the expected pattern.
    """
    match = re.search(r'\.p(\d+)$', plan_hdf.stem, re.IGNORECASE)
    return match.group(1) if match else "01"


def _run_linux_preprocess(
    project_dir: Path,
    plan_hdf: Path,
    log_path: Path,
    env: dict,
) -> Path:
    """Run ras_preprocess.py to compute geometry hydraulic tables on Linux.

    Replicates the HEC-RAS GUI "Compute Geometry" step entirely in Python,
    producing a ``p{N}.tmp.hdf`` ready for ``RasGeomPreprocess`` + ``RasUnsteady``.

    Uses ``vendor/hecras-v66-linux/ras_preprocess.py`` (github.com/neeraip/hecras-v66-linux).

    Args:
        project_dir: Path to the HEC-RAS project directory.
        plan_hdf:    Path to the plan HDF file (e.g., ``Muncie.p04.hdf``).
        log_path:    Path to the log file (output is appended).
        env:         OS environment dict passed to subprocess.

    Returns:
        Path to the generated ``.tmp.hdf`` file in ``project_dir``.

    Raises:
        RuntimeError: If ``ras_preprocess.py`` is missing, exits non-zero,
                      or does not produce the expected output file.
    """
    if not _RAS_PREPROCESS.exists():
        raise RuntimeError(
            f"ras_preprocess.py not found at {_RAS_PREPROCESS}. "
            "Ensure vendor/hecras-v66-linux is present "
            "(git clone https://github.com/neeraip/hecras-v66-linux vendor/hecras-v66-linux)."
        )
    plan_number = _extract_plan_number(plan_hdf)
    cmd = [
        sys.executable, str(_RAS_PREPROCESS),
        str(project_dir),
        "--plan", plan_number,
        "--output-dir", str(project_dir),
    ]
    logger.info(
        "[linux-preprocess] ras_preprocess.py project=%s plan=%s",
        project_dir.name, plan_number,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as log_fh:
        log_fh.write(f"[linux-preprocess] cmd: {' '.join(cmd)}\n")
        rc = subprocess.call(cmd, stdout=log_fh, stderr=subprocess.STDOUT, env=env)
    if rc != 0:
        raise RuntimeError(
            f"ras_preprocess.py exited with code {rc}. See log: {log_path}"
        )
    # Expected output: {project_name}.p{plan_number}.tmp.hdf in project_dir
    tmp_hdf = plan_hdf.with_suffix(".tmp.hdf")
    if not tmp_hdf.exists():
        # Fallback: glob for any matching .tmp.hdf in the project dir
        matches = list(project_dir.glob(f"*.p{plan_number}.tmp.hdf"))
        if matches:
            tmp_hdf = matches[0]
        else:
            raise RuntimeError(
                f"ras_preprocess.py succeeded but {tmp_hdf.name} not found in {project_dir}"
            )
    logger.info("[linux-preprocess] produced %s", tmp_hdf.name)
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
    preprocess_mode: Optional[str] = None,
) -> bool:
    """
    Execute a single queued job.

    Marks the job 'running', attempts execution up to twice (retry after 30s
    on first failure), then marks 'complete' or 'error'.

    In mock mode the actual RasUnsteady binary is never called; a simulated
    run is performed instead for testing without a HEC-RAS installation.

    Args:
        job_id:           UUID of the job to run.
        ras_exe_dir:      Directory containing the RasUnsteady (and optionally
                          RasGeomPreprocess) executable.
        mock:             If True, simulate execution without calling RasUnsteady.
        db_path:          Override default DB location.
        logs_dir:         Override default logs directory.
        preprocess_mode:  Override the preprocess_mode stored on the job record.
                          ``'linux'``   — run ras_preprocess.py + RasGeomPreprocess
                                          before RasUnsteady (default for new jobs);
                          ``'windows'`` — geometry was preprocessed on Windows,
                                          skip recompute (same as 'skip' at runtime);
                          ``'skip'``    — geometry tables already present, just strip
                                          Results and run RasUnsteady directly.
                          Pass ``None`` to use the value stored on the job.

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
    # Resolve preprocess_mode: caller override → job record → fallback 'linux'
    _preprocess_mode = (
        preprocess_mode
        if preprocess_mode is not None
        else (job.get("preprocess_mode") or "linux")
    )

    for attempt in range(1, 3):  # attempts 1 and 2
        _update_job(job_id, db_path, attempts=attempt)
        logger.info(
            f"Job {job_id} attempt {attempt}/2 — {job['name']} "
            f"(preprocess={_preprocess_mode})"
        )

        if mock:
            try:
                _run_mock(job, _logs_dir)
                mock_results = Path(tempfile.gettempdir()) / "mock_results" / job_id
                _update_job(job_id, db_path,
                            status="complete",
                            completed_at=_now_iso(),
                            results_dir=str(mock_results))
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
            # ── Real execution ────────────────────────────────────────────
            ras_exe_dir_path = Path(ras_exe_dir)
            lib_base = ras_exe_dir_path.parent / "libs"
            ld_path = (
                f"{lib_base}:"
                f"{lib_base / 'mkl'}:"
                f"{lib_base / 'rhel_8'}"
            )
            env = os.environ.copy()
            env["LD_LIBRARY_PATH"] = ld_path
            log_path = _logs_dir / f"{job_id}.log"

            tmp_hdf: Optional[Path] = None
            try:
                if _preprocess_mode == "linux":
                    # Linux geometry compute: ras_preprocess.py → RasGeomPreprocess → RasUnsteady
                    tmp_hdf = _run_linux_preprocess(project_dir, plan_hdf, log_path, env)
                else:
                    # "windows" or "skip": geometry already computed; strip Results + run
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

            # ── RasGeomPreprocess (linux mode only) ──────────────────────
            if _preprocess_mode == "linux":
                geom_pre_exe = ras_exe_dir_path / "RasGeomPreprocess"
                if geom_pre_exe.exists():
                    logger.info(
                        f"[linux-preprocess] RasGeomPreprocess {tmp_hdf.name} {geom_ext}"
                    )
                    with open(log_path, "a") as log_fh:
                        gp_rc = subprocess.call(
                            [str(geom_pre_exe), str(tmp_hdf), geom_ext],
                            stdout=log_fh,
                            stderr=subprocess.STDOUT,
                            env=env,
                        )
                    if gp_rc != 0:
                        err_msg = f"RasGeomPreprocess exited with code {gp_rc}"
                        logger.error(f"Job {job_id}: {err_msg}")
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
                else:
                    logger.warning(
                        "[linux-preprocess] RasGeomPreprocess not found at %s — "
                        "skipping (proceeding directly to RasUnsteady)",
                        geom_pre_exe,
                    )

            ras_exe = ras_exe_dir_path / "RasUnsteady"
            success = False
            err_msg = ""

            try:
                # Append to log (linux mode may have already written preprocess output)
                with open(log_path, "a") as log_fh:
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
    preprocess_mode: Optional[str] = None,
) -> None:
    """
    Process all queued jobs, running up to max_parallel concurrently.

    Loops until the queue is empty.  Uses a thread pool so multiple
    RasUnsteady processes can run simultaneously for different return periods
    or watersheds.

    Args:
        ras_exe_dir:      Directory containing the RasUnsteady executable.
        max_parallel:     Maximum number of simultaneous RasUnsteady processes.
        mock:             If True, use mock execution (no HEC-RAS required).
        db_path:          Override default DB location.
        logs_dir:         Override default logs directory.
        preprocess_mode:  Override preprocess_mode for all jobs in this run.
                          Pass ``None`` to use each job's stored value.
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
                pool.submit(
                    run_job, job["id"], ras_exe_dir, mock, db_path, logs_dir, preprocess_mode
                ): job["id"]
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
    @click.option(
        "--preprocess-mode",
        default="linux",
        show_default=True,
        type=click.Choice(["linux", "windows", "skip"]),
    )
    def enqueue(name, project_dir, plan_hdf, geom_ext, return_period, preprocess_mode):
        """Add a job to the queue."""
        jid = enqueue_job(name, project_dir, plan_hdf, geom_ext, return_period,
                          preprocess_mode=preprocess_mode)
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
    @click.option(
        "--preprocess-mode",
        default=None,
        type=click.Choice(["linux", "windows", "skip"]),
    )
    def run(job_id, ras_exe_dir, mock, preprocess_mode):
        """Run a single job by ID."""
        ok = run_job(job_id, Path(ras_exe_dir), mock=mock, preprocess_mode=preprocess_mode)
        click.echo("success" if ok else "error")

    @cli.command("run-queue")
    @click.argument("ras_exe_dir", type=click.Path())
    @click.option("--max-parallel", default=2, show_default=True)
    @click.option("--mock", is_flag=True)
    @click.option(
        "--preprocess-mode",
        default=None,
        type=click.Choice(["linux", "windows", "skip"]),
    )
    def run_queue_cmd(ras_exe_dir, max_parallel, mock, preprocess_mode):
        """Process all queued jobs."""
        run_queue(Path(ras_exe_dir), max_parallel=max_parallel, mock=mock,
                  preprocess_mode=preprocess_mode)

    return cli


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli = _build_cli()
    if cli:
        cli()
