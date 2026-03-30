"""
Tests for runner.py — HEC-RAS job queue and execution engine

All tests use mock=True so no HEC-RAS installation is required.
A temporary SQLite DB and temporary directories are used to avoid
polluting the real data/ directory.
"""

import os
import sys
import tempfile
import time
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'pipeline'))

import pytest
from runner import (
    enqueue_job,
    get_job,
    list_jobs,
    run_job,
    run_queue,
    _init_db,
    _extract_plan_number,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_db(tmp_path):
    """Provide a fresh temporary SQLite DB path for each test."""
    return tmp_path / "test_jobs.db"


@pytest.fixture()
def tmp_project(tmp_path):
    """Create a minimal project directory with a fake plan HDF."""
    import h5py
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    plan_hdf = project_dir / "test.p04.hdf"
    # Create a minimal valid HDF so _prepare_run can copy it
    with h5py.File(str(plan_hdf), "w") as hf:
        hf.create_group("Geometry")
    return project_dir, plan_hdf


@pytest.fixture()
def logs_dir(tmp_path):
    """Provide a temporary logs directory."""
    d = tmp_path / "logs"
    d.mkdir()
    return d


# ── Queue CRUD ────────────────────────────────────────────────────────────────

class TestEnqueueJob:
    def test_enqueue_creates_job(self, tmp_db, tmp_project):
        """enqueue_job should insert a row with status='queued'."""
        project_dir, plan_hdf = tmp_project
        job_id = enqueue_job(
            name="test_job",
            project_dir=str(project_dir),
            plan_hdf=str(plan_hdf),
            db_path=tmp_db,
        )
        assert isinstance(job_id, str)
        assert len(job_id) == 36   # UUID4 format

        job = get_job(job_id, db_path=tmp_db)
        assert job is not None
        assert job["status"] == "queued"
        assert job["name"] == "test_job"
        assert job["geom_ext"] == "g01"   # default

    def test_enqueue_with_return_period(self, tmp_db, tmp_project):
        """Return period should be stored on the job."""
        project_dir, plan_hdf = tmp_project
        job_id = enqueue_job(
            "rp100", str(project_dir), str(plan_hdf),
            return_period_yr=100, db_path=tmp_db,
        )
        job = get_job(job_id, db_path=tmp_db)
        assert job["return_period_yr"] == 100

    def test_enqueue_returns_unique_ids(self, tmp_db, tmp_project):
        """Two enqueue calls should produce distinct job IDs."""
        project_dir, plan_hdf = tmp_project
        id1 = enqueue_job("a", str(project_dir), str(plan_hdf), db_path=tmp_db)
        id2 = enqueue_job("b", str(project_dir), str(plan_hdf), db_path=tmp_db)
        assert id1 != id2


class TestGetJob:
    def test_get_job_returns_dict(self, tmp_db, tmp_project):
        """get_job should return a dict with all schema fields."""
        project_dir, plan_hdf = tmp_project
        job_id = enqueue_job("j", str(project_dir), str(plan_hdf), db_path=tmp_db)
        job = get_job(job_id, db_path=tmp_db)
        assert isinstance(job, dict)
        for field in ("id", "name", "project_dir", "plan_hdf", "status",
                      "created_at", "attempts"):
            assert field in job

    def test_get_job_missing_returns_none(self, tmp_db):
        """get_job with an unknown ID should return None."""
        _init_db(tmp_db)
        assert get_job("nonexistent-id", db_path=tmp_db) is None


class TestListJobs:
    def test_list_all_jobs(self, tmp_db, tmp_project):
        """list_jobs() with no filter should return all jobs."""
        project_dir, plan_hdf = tmp_project
        for name in ("a", "b", "c"):
            enqueue_job(name, str(project_dir), str(plan_hdf), db_path=tmp_db)
        jobs = list_jobs(db_path=tmp_db)
        assert len(jobs) == 3

    def test_list_jobs_filter_by_status(self, tmp_db, tmp_project):
        """list_jobs(status='queued') should return only queued jobs."""
        project_dir, plan_hdf = tmp_project
        enqueue_job("j1", str(project_dir), str(plan_hdf), db_path=tmp_db)
        enqueue_job("j2", str(project_dir), str(plan_hdf), db_path=tmp_db)
        queued = list_jobs(status="queued", db_path=tmp_db)
        assert len(queued) == 2
        assert all(j["status"] == "queued" for j in queued)

        running = list_jobs(status="running", db_path=tmp_db)
        assert running == []

    def test_list_jobs_empty(self, tmp_db):
        """list_jobs on an empty DB should return an empty list."""
        _init_db(tmp_db)
        assert list_jobs(db_path=tmp_db) == []


# ── Job Execution ─────────────────────────────────────────────────────────────

class TestRunJobMock:
    def test_run_job_mock_completes(self, tmp_db, tmp_project, logs_dir):
        """run_job(mock=True) should transition the job to 'complete'."""
        project_dir, plan_hdf = tmp_project
        job_id = enqueue_job(
            "mock_run", str(project_dir), str(plan_hdf), db_path=tmp_db,
        )
        ok = run_job(
            job_id,
            ras_exe_dir=Path("/fake/ras/bin"),
            mock=True,
            db_path=tmp_db,
            logs_dir=logs_dir,
        )
        assert ok is True
        job = get_job(job_id, db_path=tmp_db)
        assert job["status"] == "complete"
        assert job["completed_at"] is not None
        assert job["started_at"] is not None

    def test_run_job_mock_creates_log(self, tmp_db, tmp_project, logs_dir):
        """Mock run should write a log file."""
        project_dir, plan_hdf = tmp_project
        job_id = enqueue_job(
            "log_check", str(project_dir), str(plan_hdf), db_path=tmp_db,
        )
        run_job(
            job_id,
            ras_exe_dir=Path("/fake/ras/bin"),
            mock=True,
            db_path=tmp_db,
            logs_dir=logs_dir,
        )
        log_path = logs_dir / f"{job_id}.log"
        assert log_path.exists()
        assert "[MOCK]" in log_path.read_text()

    def test_run_job_mock_creates_output_hdf(self, tmp_db, tmp_project, logs_dir):
        """Mock run should create a fake output HDF at plan_hdf path."""
        import h5py
        project_dir, plan_hdf = tmp_project
        job_id = enqueue_job(
            "hdf_check", str(project_dir), str(plan_hdf), db_path=tmp_db,
        )
        run_job(
            job_id,
            ras_exe_dir=Path("/fake/ras/bin"),
            mock=True,
            db_path=tmp_db,
            logs_dir=logs_dir,
        )
        # The fake HDF should have a /Results group
        with h5py.File(str(plan_hdf), "r") as hf:
            assert "Results" in hf


class TestRunJobError:
    def test_bad_project_dir_marks_error(self, tmp_db, tmp_path, logs_dir):
        """run_job(mock=False) with a non-existent plan_hdf marks status='error'."""
        bad_dir = tmp_path / "nonexistent_project"
        bad_hdf = bad_dir / "missing.p01.hdf"
        job_id = enqueue_job(
            "bad_job", str(bad_dir), str(bad_hdf), db_path=tmp_db,
        )
        # mock=False: _prepare_run will fail because plan_hdf doesn't exist;
        # but RasUnsteady would also not be found → we need to ensure the
        # _prepare_run failure is caught before Popen is called.
        # We use a clearly non-existent exe dir to avoid any accidental binary run.
        ok = run_job(
            job_id,
            ras_exe_dir=Path("/definitely/not/installed"),
            mock=False,
            db_path=tmp_db,
            logs_dir=logs_dir,
        )
        assert ok is False
        job = get_job(job_id, db_path=tmp_db)
        assert job["status"] == "error"
        assert job["error_msg"] is not None
        assert "preparation failed" in job["error_msg"].lower() or \
               "not found" in job["error_msg"].lower()

    def test_invalid_job_id_raises(self, tmp_db):
        """run_job with a non-existent job_id should raise ValueError."""
        _init_db(tmp_db)
        with pytest.raises(ValueError, match="not found"):
            run_job(
                "no-such-id",
                ras_exe_dir=Path("/fake"),
                mock=True,
                db_path=tmp_db,
            )


# ── Queue Runner ──────────────────────────────────────────────────────────────

class TestRunQueue:
    def test_run_queue_processes_all_jobs(self, tmp_db, tmp_project, logs_dir):
        """run_queue should process every queued job and mark them complete."""
        project_dir, plan_hdf = tmp_project
        ids = []
        for i in range(3):
            jid = enqueue_job(
                f"batch_{i}", str(project_dir), str(plan_hdf), db_path=tmp_db,
            )
            ids.append(jid)

        run_queue(
            ras_exe_dir=Path("/fake/ras/bin"),
            max_parallel=2,
            mock=True,
            db_path=tmp_db,
            logs_dir=logs_dir,
        )

        for jid in ids:
            job = get_job(jid, db_path=tmp_db)
            assert job["status"] == "complete", f"Job {jid} not complete: {job['status']}"

    def test_run_queue_empty_queue(self, tmp_db, logs_dir):
        """run_queue on an empty DB should return without error."""
        _init_db(tmp_db)
        # Should complete without raising
        run_queue(
            ras_exe_dir=Path("/fake/ras/bin"),
            mock=True,
            db_path=tmp_db,
            logs_dir=logs_dir,
        )
        assert list_jobs(db_path=tmp_db) == []


# ── preprocess_mode tests ──────────────────────────────────────────────────────

class TestPreprocessMode:
    def test_enqueue_stores_default_preprocess_mode(self, tmp_db, tmp_project):
        """Default preprocess_mode should be 'linux'."""
        project_dir, plan_hdf = tmp_project
        job_id = enqueue_job("p_default", str(project_dir), str(plan_hdf), db_path=tmp_db)
        job = get_job(job_id, db_path=tmp_db)
        assert job["preprocess_mode"] == "linux"

    def test_enqueue_stores_custom_preprocess_mode(self, tmp_db, tmp_project):
        """preprocess_mode='skip' should be stored on the job."""
        project_dir, plan_hdf = tmp_project
        job_id = enqueue_job(
            "p_skip", str(project_dir), str(plan_hdf),
            db_path=tmp_db, preprocess_mode="skip",
        )
        job = get_job(job_id, db_path=tmp_db)
        assert job["preprocess_mode"] == "skip"

    def test_enqueue_windows_preprocess_mode(self, tmp_db, tmp_project):
        """preprocess_mode='windows' should be stored on the job."""
        project_dir, plan_hdf = tmp_project
        job_id = enqueue_job(
            "p_win", str(project_dir), str(plan_hdf),
            db_path=tmp_db, preprocess_mode="windows",
        )
        job = get_job(job_id, db_path=tmp_db)
        assert job["preprocess_mode"] == "windows"

    def test_run_job_skip_mode_completes(self, tmp_db, tmp_project, logs_dir):
        """run_job with preprocess_mode='skip' and mock=True should complete."""
        project_dir, plan_hdf = tmp_project
        job_id = enqueue_job(
            "skip_run", str(project_dir), str(plan_hdf),
            db_path=tmp_db, preprocess_mode="skip",
        )
        ok = run_job(
            job_id,
            ras_exe_dir=Path("/fake/ras/bin"),
            mock=True,
            db_path=tmp_db,
            logs_dir=logs_dir,
        )
        assert ok is True
        assert get_job(job_id, db_path=tmp_db)["status"] == "complete"

    def test_run_job_linux_mode_mock_completes(self, tmp_db, tmp_project, logs_dir):
        """run_job with preprocess_mode='linux' and mock=True should skip ras_preprocess
        and complete (mock bypasses all real preprocessing)."""
        project_dir, plan_hdf = tmp_project
        job_id = enqueue_job(
            "linux_mock", str(project_dir), str(plan_hdf),
            db_path=tmp_db, preprocess_mode="linux",
        )
        ok = run_job(
            job_id,
            ras_exe_dir=Path("/fake/ras/bin"),
            mock=True,
            db_path=tmp_db,
            logs_dir=logs_dir,
        )
        assert ok is True
        assert get_job(job_id, db_path=tmp_db)["status"] == "complete"

    def test_run_job_linux_mode_calls_ras_preprocess(self, tmp_db, tmp_project, logs_dir):
        """run_job with preprocess_mode='linux' (non-mock) should call ras_preprocess.py."""
        import h5py
        import runner as runner_mod

        project_dir, plan_hdf = tmp_project

        # Build a fake .tmp.hdf that _run_linux_preprocess would produce
        tmp_hdf_path = plan_hdf.with_suffix(".tmp.hdf")

        def fake_linux_preprocess(project_dir, plan_hdf, log_path, env):
            """Simulate ras_preprocess.py creating a tmp.hdf."""
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("[mock linux-preprocess] done\n")
            with h5py.File(str(tmp_hdf_path), "w") as hf:
                hf.create_group("Geometry")
            return tmp_hdf_path

        job_id = enqueue_job(
            "linux_real", str(project_dir), str(plan_hdf),
            db_path=tmp_db, preprocess_mode="linux",
        )

        with unittest.mock.patch.object(runner_mod, "_run_linux_preprocess",
                                        side_effect=fake_linux_preprocess) as mock_pre, \
             unittest.mock.patch("subprocess.Popen") as mock_popen:
            # Make RasUnsteady appear to succeed
            mock_proc = unittest.mock.MagicMock()
            mock_proc.wait.return_value = 0
            mock_popen.return_value = mock_proc

            ok = run_job(
                job_id,
                ras_exe_dir=Path("/fake/ras/bin"),
                mock=False,
                db_path=tmp_db,
                logs_dir=logs_dir,
                preprocess_mode="linux",
            )

        assert mock_pre.called, "_run_linux_preprocess should have been called"
        # Job may complete or fail depending on file cleanup; just verify preprocess was called

    def test_schema_migration_adds_preprocess_mode(self, tmp_path):
        """_init_db() on a legacy DB without preprocess_mode column should add it."""
        import sqlite3
        db_path = tmp_path / "legacy.db"
        # Create a DB with the old schema (no preprocess_mode column)
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE jobs (
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
                results_dir TEXT
            )
        """)
        conn.commit()
        conn.close()

        # _init_db should migrate the schema
        _init_db(db_path)

        conn = sqlite3.connect(str(db_path))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        conn.close()
        assert "preprocess_mode" in cols


class TestExtractPlanNumber:
    def test_standard_plan_number(self):
        assert _extract_plan_number(Path("Muncie.p04.hdf")) == "04"

    def test_plan_01(self):
        assert _extract_plan_number(Path("BEC.p01.hdf")) == "01"

    def test_plan_two_digits(self):
        assert _extract_plan_number(Path("Project.p12.hdf")) == "12"

    def test_fallback_on_no_match(self):
        assert _extract_plan_number(Path("weirdfile.hdf")) == "01"

    def test_path_object(self):
        from pathlib import Path
        assert _extract_plan_number(Path("/abs/path/MyProj.p03.hdf")) == "03"
