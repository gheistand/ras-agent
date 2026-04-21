"""
Tests for slurm.py — SLURM batch job submission for NCSA Illinois Computes

All SSH/rsync calls are mocked. No cluster access required.
"""

import os
import sys
import tempfile
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'pipeline'))

import pytest
from slurm import (
    SlurmConfig,
    SlurmJobResult,
    generate_job_script,
    submit_slurm_job,
    check_slurm_status,
    slurm_config_from_env,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def cfg():
    """Minimal SlurmConfig for testing."""
    return SlurmConfig(
        host="cc-login.campuscluster.illinois.edu",
        user="testuser",
        partition="IllinoisComputes",
        account="heistand-ic",
        nodes=1,
        ntasks_per_node=8,
        time_limit="04:00:00",
        taiga_base="/projects/illinois/eng/cee/heistand",
    )


@pytest.fixture()
def tmp_project(tmp_path):
    """Create a minimal project directory with a fake plan HDF."""
    import h5py
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    plan_hdf = project_dir / "test.p04.hdf"
    with h5py.File(str(plan_hdf), "w") as hf:
        hf.create_group("Geometry")
    return project_dir, plan_hdf


# ── SlurmConfig ───────────────────────────────────────────────────────────────

class TestSlurmConfig:
    def test_defaults(self):
        """SlurmConfig should have correct default values."""
        cfg = SlurmConfig()
        assert cfg.host == "cc-login.campuscluster.illinois.edu"
        assert cfg.partition == "IllinoisComputes"
        assert cfg.account == "heistand-ic"
        assert cfg.nodes == 1
        assert cfg.ntasks_per_node == 8
        assert cfg.time_limit == "04:00:00"
        assert cfg.taiga_base == "/projects/illinois/eng/cee/heistand"
        assert cfg.ssh_key is None

    def test_from_env_reads_vars(self, monkeypatch):
        """SlurmConfig.from_env() should read all expected environment variables."""
        monkeypatch.setenv("SLURM_USER", "gheistand")
        monkeypatch.setenv("SLURM_HOST", "myhost.example.com")
        monkeypatch.setenv("SLURM_PARTITION", "IllinoisComputes-GPU")
        monkeypatch.setenv("SLURM_ACCOUNT", "heistand-ic")
        monkeypatch.setenv("SLURM_NODES", "2")
        monkeypatch.setenv("SLURM_NTASKS", "16")
        monkeypatch.setenv("SLURM_TIME", "08:00:00")
        monkeypatch.setenv("SLURM_TAIGA_BASE", "/projects/illinois/eng/cee/heistand")
        monkeypatch.setenv("SLURM_SSH_KEY", "/home/user/.ssh/id_rsa")

        cfg = SlurmConfig.from_env()
        assert cfg.user == "gheistand"
        assert cfg.host == "myhost.example.com"
        assert cfg.partition == "IllinoisComputes-GPU"
        assert cfg.nodes == 2
        assert cfg.ntasks_per_node == 16
        assert cfg.time_limit == "08:00:00"
        assert cfg.ssh_key == "/home/user/.ssh/id_rsa"

    def test_from_env_defaults(self, monkeypatch):
        """SlurmConfig.from_env() uses correct defaults when vars not set."""
        # Clear all SLURM_ vars
        for key in ["SLURM_USER", "SLURM_HOST", "SLURM_PARTITION", "SLURM_ACCOUNT",
                    "SLURM_NODES", "SLURM_NTASKS", "SLURM_TIME", "SLURM_TAIGA_BASE",
                    "SLURM_SSH_KEY"]:
            monkeypatch.delenv(key, raising=False)

        cfg = SlurmConfig.from_env()
        assert cfg.host == "cc-login.campuscluster.illinois.edu"
        assert cfg.partition == "IllinoisComputes"
        assert cfg.account == "heistand-ic"
        assert cfg.nodes == 1
        assert cfg.ntasks_per_node == 8


# ── generate_job_script ───────────────────────────────────────────────────────

class TestGenerateJobScript:
    def test_required_sbatch_headers(self, cfg):
        """Generated script must contain all required #SBATCH headers."""
        script = generate_job_script(
            job_id="abc12345-0000-0000-0000-000000000000",
            plan_hdf="/projects/illinois/eng/cee/heistand/jobs/abc12345/test.p04.hdf",
            geom_ext="g01",
            output_dir="/projects/illinois/eng/cee/heistand/jobs/abc12345",
            config=cfg,
        )
        assert "#SBATCH --partition=IllinoisComputes" in script
        assert "#SBATCH --account=heistand-ic" in script
        assert "#SBATCH --nodes=1" in script
        assert "#SBATCH --ntasks-per-node=8" in script
        assert "#SBATCH --time=04:00:00" in script

    def test_job_name_uses_short_id(self, cfg):
        """Job name in #SBATCH header should use first 8 chars of job_id."""
        script = generate_job_script(
            job_id="deadbeef-1234-5678-abcd-000000000000",
            plan_hdf="/projects/illinois/eng/cee/heistand/jobs/deadbeef/test.p04.hdf",
            geom_ext="g01",
            output_dir="/projects/illinois/eng/cee/heistand/jobs/deadbeef",
            config=cfg,
        )
        assert "#SBATCH --job-name=ras-deadbeef" in script

    def test_hecras_environment_setup(self, cfg):
        """Script must set LD_LIBRARY_PATH and PATH for HEC-RAS libs."""
        script = generate_job_script(
            job_id="abc12345-0000-0000-0000-000000000000",
            plan_hdf="/projects/illinois/eng/cee/heistand/jobs/abc12345/test.p04.hdf",
            geom_ext="g01",
            output_dir="/projects/illinois/eng/cee/heistand/jobs/abc12345",
            config=cfg,
        )
        assert "LD_LIBRARY_PATH" in script
        assert "hecras-v66-linux" in script
        assert "RasGeomPreprocess" in script
        assert "RasUnsteady" in script

    def test_geom_ext_in_commands(self, cfg):
        """Geometry extension must appear in RasGeomPreprocess and RasUnsteady commands."""
        script = generate_job_script(
            job_id="abc12345-0000-0000-0000-000000000000",
            plan_hdf="/projects/illinois/eng/cee/heistand/jobs/abc12345/test.p04.hdf",
            geom_ext="x04",
            output_dir="/projects/illinois/eng/cee/heistand/jobs/abc12345",
            config=cfg,
        )
        # x04 should appear after RasGeomPreprocess and RasUnsteady
        assert "x04" in script

    def test_output_log_paths(self, cfg):
        """Script must direct stdout and stderr to Taiga logs directory."""
        script = generate_job_script(
            job_id="abc12345-0000-0000-0000-000000000000",
            plan_hdf="/projects/illinois/eng/cee/heistand/jobs/abc12345/test.p04.hdf",
            geom_ext="g01",
            output_dir="/projects/illinois/eng/cee/heistand/jobs/abc12345",
            config=cfg,
        )
        assert "/projects/illinois/eng/cee/heistand/logs" in script
        assert "#SBATCH --output=" in script
        assert "#SBATCH --error=" in script

    def test_shebang_present(self, cfg):
        """Script must start with #!/bin/bash."""
        script = generate_job_script(
            job_id="abc12345-0000-0000-0000-000000000000",
            plan_hdf="/projects/illinois/eng/cee/heistand/jobs/abc12345/test.p04.hdf",
            geom_ext="g01",
            output_dir="/projects/illinois/eng/cee/heistand/jobs/abc12345",
            config=cfg,
        )
        assert script.startswith("#!/bin/bash")


# ── submit_slurm_job (mock mode) ──────────────────────────────────────────────

class TestSubmitSlurmJobMock:
    def test_mock_returns_mock_status(self, cfg, tmp_project):
        """In mock mode, submit_slurm_job returns SlurmJobResult with status='mock'."""
        project_dir, plan_hdf = tmp_project
        result = submit_slurm_job(
            job_id="test-job-uuid-1234",
            project_dir=project_dir,
            plan_hdf_path=plan_hdf,
            geom_ext="g01",
            config=cfg,
            mock=True,
        )
        assert isinstance(result, SlurmJobResult)
        assert result.status == "mock"

    def test_mock_returns_mock_job_id(self, cfg, tmp_project):
        """In mock mode, slurm_job_id should be a mock placeholder."""
        project_dir, plan_hdf = tmp_project
        result = submit_slurm_job(
            job_id="test-job-uuid-1234",
            project_dir=project_dir,
            plan_hdf_path=plan_hdf,
            geom_ext="g01",
            config=cfg,
            mock=True,
        )
        assert "mock" in result.slurm_job_id.lower()

    def test_mock_creates_local_script(self, cfg, tmp_project):
        """In mock mode, a local job script file should be created."""
        project_dir, plan_hdf = tmp_project
        result = submit_slurm_job(
            job_id="test-job-uuid-1234",
            project_dir=project_dir,
            plan_hdf_path=plan_hdf,
            geom_ext="g01",
            config=cfg,
            mock=True,
        )
        assert result.job_script_path is not None
        assert result.job_script_path.exists()

    def test_mock_script_has_correct_headers(self, cfg, tmp_project):
        """Mock mode script should have correct SBATCH headers."""
        project_dir, plan_hdf = tmp_project
        result = submit_slurm_job(
            job_id="test-job-uuid-1234",
            project_dir=project_dir,
            plan_hdf_path=plan_hdf,
            geom_ext="g01",
            config=cfg,
            mock=True,
        )
        script_text = result.job_script_path.read_text()
        assert "#SBATCH --partition=IllinoisComputes" in script_text
        assert "#SBATCH --account=heistand-ic" in script_text

    def test_mock_no_ssh_calls(self, cfg, tmp_project):
        """Mock mode must not make any SSH or subprocess calls to the cluster."""
        project_dir, plan_hdf = tmp_project
        with unittest.mock.patch("subprocess.run") as mock_run:
            submit_slurm_job(
                job_id="test-job-uuid-1234",
                project_dir=project_dir,
                plan_hdf_path=plan_hdf,
                geom_ext="g01",
                config=cfg,
                mock=True,
            )
            # subprocess.run should NOT be called (no ssh/rsync/scp)
            mock_run.assert_not_called()

    def test_mock_remote_work_dir_set(self, cfg, tmp_project):
        """Mock mode result should contain the expected remote_work_dir."""
        project_dir, plan_hdf = tmp_project
        result = submit_slurm_job(
            job_id="test-job-uuid-1234",
            project_dir=project_dir,
            plan_hdf_path=plan_hdf,
            geom_ext="g01",
            config=cfg,
            mock=True,
        )
        assert "test-job-uuid-1234" in result.remote_work_dir
        assert cfg.taiga_base in result.remote_work_dir


# ── check_slurm_status ────────────────────────────────────────────────────────

class TestCheckSlurmStatus:
    def _make_completed_process(self, stdout="", returncode=0):
        result = unittest.mock.MagicMock()
        result.stdout = stdout
        result.returncode = returncode
        result.stderr = ""
        return result

    def test_running_state(self, cfg):
        """check_slurm_status should return RUNNING when squeue shows R."""
        with unittest.mock.patch("slurm._run_ssh") as mock_ssh:
            mock_ssh.return_value = self._make_completed_process(stdout="RUNNING\n")
            status = check_slurm_status("12345", cfg)
        assert status == "RUNNING"

    def test_pending_state(self, cfg):
        """check_slurm_status should return PENDING when squeue shows PENDING."""
        with unittest.mock.patch("slurm._run_ssh") as mock_ssh:
            mock_ssh.return_value = self._make_completed_process(stdout="PENDING\n")
            status = check_slurm_status("12345", cfg)
        assert status == "PENDING"

    def test_completed_state(self, cfg):
        """check_slurm_status should return COMPLETED when sacct shows COMPLETED."""
        with unittest.mock.patch("slurm._run_ssh") as mock_ssh:
            mock_ssh.return_value = self._make_completed_process(stdout="COMPLETED\n")
            status = check_slurm_status("12345", cfg)
        assert status == "COMPLETED"

    def test_failed_state(self, cfg):
        """check_slurm_status should return FAILED when job failed."""
        with unittest.mock.patch("slurm._run_ssh") as mock_ssh:
            mock_ssh.return_value = self._make_completed_process(stdout="FAILED\n")
            status = check_slurm_status("12345", cfg)
        assert status == "FAILED"

    def test_unknown_on_ssh_error(self, cfg):
        """check_slurm_status returns UNKNOWN on SSH timeout or connection error."""
        import subprocess
        with unittest.mock.patch("slurm._run_ssh", side_effect=subprocess.TimeoutExpired("ssh", 30)):
            status = check_slurm_status("12345", cfg)
        assert status == "UNKNOWN"

    def test_empty_output_triggers_sacct(self, cfg):
        """Empty squeue output should trigger sacct fallback."""
        with unittest.mock.patch("slurm._run_ssh") as mock_ssh:
            # First call (squeue) returns empty; second call (sacct) returns COMPLETED
            mock_ssh.side_effect = [
                self._make_completed_process(stdout=""),
                self._make_completed_process(stdout="COMPLETED"),
            ]
            status = check_slurm_status("12345", cfg)
        assert status == "COMPLETED"


# ── slurm_config_from_env ─────────────────────────────────────────────────────

class TestSlurmConfigFromEnv:
    def test_returns_none_without_slurm_user(self, monkeypatch):
        """slurm_config_from_env returns None when SLURM_USER is not set."""
        monkeypatch.delenv("SLURM_USER", raising=False)
        result = slurm_config_from_env()
        assert result is None

    def test_returns_config_with_slurm_user(self, monkeypatch):
        """slurm_config_from_env returns SlurmConfig when SLURM_USER is set."""
        monkeypatch.setenv("SLURM_USER", "gheistand")
        result = slurm_config_from_env()
        assert isinstance(result, SlurmConfig)
        assert result.user == "gheistand"

    def test_config_has_correct_defaults(self, monkeypatch):
        """Returned SlurmConfig should have correct NCSA cluster defaults."""
        monkeypatch.setenv("SLURM_USER", "gheistand")
        for key in ["SLURM_HOST", "SLURM_PARTITION", "SLURM_ACCOUNT"]:
            monkeypatch.delenv(key, raising=False)
        result = slurm_config_from_env()
        assert result.host == "cc-login.campuscluster.illinois.edu"
        assert result.partition == "IllinoisComputes"
        assert result.account == "heistand-ic"
