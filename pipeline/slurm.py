"""
slurm.py — SLURM batch job submission for NCSA Illinois Computes Campus Cluster

Submits HEC-RAS RasUnsteady jobs to the IllinoisComputes partition.
Requires SSH access to the cluster (University of Illinois NetID credentials).

Account: heistand-ic
Partition: IllinoisComputes (CPU) or IllinoisComputes-GPU (GPU)
Storage: /projects/illinois/eng/cee/heistand (Taiga)
Login node: cc-login.campuscluster.illinois.edu

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_HOST = "cc-login.campuscluster.illinois.edu"
DEFAULT_PARTITION = "IllinoisComputes"
DEFAULT_ACCOUNT = "heistand-ic"
DEFAULT_TAIGA_BASE = "/projects/illinois/eng/cee/heistand"
DEFAULT_HECRAS_LIBS = "/projects/illinois/eng/cee/heistand/hecras-v66-linux"


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class SlurmConfig:
    """Configuration for SLURM job submission to NCSA Illinois Computes Campus Cluster.

    Attributes:
        host: SSH login node hostname.
        user: University of Illinois NetID for SSH authentication.
        partition: SLURM partition name (IllinoisComputes or IllinoisComputes-GPU).
        account: SLURM account name (heistand-ic).
        nodes: Number of compute nodes per job.
        ntasks_per_node: MPI tasks (cores) per node.
        time_limit: SLURM wall time limit in HH:MM:SS format.
        taiga_base: Base path on Taiga shared storage for job files.
        ssh_key: Path to SSH private key file, or None to use default.
    """
    host: str = DEFAULT_HOST
    user: str = ""
    partition: str = DEFAULT_PARTITION
    account: str = DEFAULT_ACCOUNT
    nodes: int = 1
    ntasks_per_node: int = 8
    time_limit: str = "04:00:00"
    taiga_base: str = DEFAULT_TAIGA_BASE
    ssh_key: Optional[str] = None

    @classmethod
    def from_env(cls) -> "SlurmConfig":
        """Load SlurmConfig from environment variables.

        Environment variables:
            SLURM_USER:      NetID for SSH (required for real submissions)
            SLURM_HOST:      Login node hostname (default: cc-login.campuscluster.illinois.edu)
            SLURM_PARTITION: SLURM partition (default: IllinoisComputes)
            SLURM_ACCOUNT:   SLURM account (default: heistand-ic)
            SLURM_NODES:     Nodes per job (default: 1)
            SLURM_NTASKS:    Tasks per node / cores (default: 8)
            SLURM_TIME:      Wall time limit HH:MM:SS (default: 04:00:00)
            SLURM_TAIGA_BASE: Taiga base path (default: /projects/illinois/eng/cee/heistand)
            SLURM_SSH_KEY:   Path to SSH private key file (optional)

        Returns:
            SlurmConfig populated from environment.
        """
        return cls(
            host=os.environ.get("SLURM_HOST", DEFAULT_HOST),
            user=os.environ.get("SLURM_USER", ""),
            partition=os.environ.get("SLURM_PARTITION", DEFAULT_PARTITION),
            account=os.environ.get("SLURM_ACCOUNT", DEFAULT_ACCOUNT),
            nodes=int(os.environ.get("SLURM_NODES", "1")),
            ntasks_per_node=int(os.environ.get("SLURM_NTASKS", "8")),
            time_limit=os.environ.get("SLURM_TIME", "04:00:00"),
            taiga_base=os.environ.get("SLURM_TAIGA_BASE", DEFAULT_TAIGA_BASE),
            ssh_key=os.environ.get("SLURM_SSH_KEY"),
        )


@dataclass
class SlurmJobResult:
    """Result of a SLURM job submission.

    Attributes:
        slurm_job_id: SLURM job ID returned by sbatch (e.g., "12345").
        job_script_path: Local path to the generated .sh job script.
        remote_work_dir: Working directory path on the cluster.
        status: Submission outcome: "submitted", "failed", or "mock".
        stderr: Standard error output from sbatch, if any.
    """
    slurm_job_id: str
    job_script_path: Path
    remote_work_dir: str
    status: str
    stderr: str = ""


# ── SSH Helpers ───────────────────────────────────────────────────────────────

def _ssh_args(config: SlurmConfig) -> list[str]:
    """Build base SSH argument list for the cluster login node.

    Args:
        config: SlurmConfig with host, user, and optional ssh_key.

    Returns:
        List of SSH command-line arguments (without the remote command).
    """
    args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no"]
    if config.ssh_key:
        args += ["-i", config.ssh_key]
    target = f"{config.user}@{config.host}" if config.user else config.host
    args.append(target)
    return args


def _run_ssh(cmd: str, config: SlurmConfig, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a single command on the cluster over SSH.

    Args:
        cmd:     Shell command string to execute on the remote host.
        config:  SlurmConfig with connection parameters.
        timeout: Subprocess timeout in seconds.

    Returns:
        CompletedProcess result with stdout and stderr captured.

    Raises:
        subprocess.TimeoutExpired: If the command does not complete in time.
        subprocess.SubprocessError: On SSH connection failure.
    """
    ssh_cmd = _ssh_args(config) + [cmd]
    return subprocess.run(
        ssh_cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ── Job Script Generation ─────────────────────────────────────────────────────

def generate_job_script(
    job_id: str,
    plan_hdf: str,
    geom_ext: str,
    output_dir: str,
    config: SlurmConfig,
    hecras_libs_path: str = DEFAULT_HECRAS_LIBS,
) -> str:
    """Generate a SLURM batch job script string for one RasUnsteady run.

    The script:
    - Sets required #SBATCH headers (partition, account, nodes, tasks, time)
    - Configures LD_LIBRARY_PATH and PATH for HEC-RAS 6.6 Linux libraries
    - Runs RasGeomPreprocess followed by RasUnsteady
    - On success, renames .tmp.hdf → .hdf
    - Writes stdout/stderr to Taiga logs/{slurm_job_id}.out / .err

    Args:
        job_id:           RAS Agent job UUID (used for SLURM job name).
        plan_hdf:         Remote path to the plan HDF file on the cluster.
        geom_ext:         Geometry file extension (e.g., "g01", "x04").
        output_dir:       Remote path for result output files.
        config:           SlurmConfig with partition, account, resource settings.
        hecras_libs_path: Remote path to hecras-v66-linux installation.

    Returns:
        SLURM job script as a string.
    """
    plan_hdf_path = plan_hdf
    # .tmp.hdf is what RasGeomPreprocess/RasUnsteady operate on
    plan_tmp_hdf = plan_hdf_path.replace(".hdf", ".tmp.hdf")
    logs_dir = f"{config.taiga_base}/logs"
    work_dir = f"{config.taiga_base}/jobs/{job_id}"
    libs = hecras_libs_path
    short_id = job_id[:8]

    script = f"""#!/bin/bash
#SBATCH --job-name=ras-{short_id}
#SBATCH --partition={config.partition}
#SBATCH --account={config.account}
#SBATCH --nodes={config.nodes}
#SBATCH --ntasks-per-node={config.ntasks_per_node}
#SBATCH --time={config.time_limit}
#SBATCH --output={logs_dir}/%j.out
#SBATCH --error={logs_dir}/%j.err

# HEC-RAS 6.6 Linux environment
export LD_LIBRARY_PATH={libs}/libs:{libs}/libs/rhel_8:{libs}/libs/mkl:$LD_LIBRARY_PATH
export PATH={libs}/bin:$PATH

cd {work_dir}

echo "Starting RasGeomPreprocess: $(date)"
RasGeomPreprocess {plan_tmp_hdf} {geom_ext}
gp_rc=$?
if [ $gp_rc -ne 0 ]; then
    echo "RasGeomPreprocess failed with exit code $gp_rc" >&2
    exit $gp_rc
fi

echo "Starting RasUnsteady: $(date)"
RasUnsteady {plan_tmp_hdf} {geom_ext}
ru_rc=$?

if [ $ru_rc -eq 0 ]; then
    mv {plan_tmp_hdf} {plan_hdf_path}
    echo "Simulation complete: $(date)"
else
    echo "RasUnsteady failed with exit code $ru_rc" >&2
    exit $ru_rc
fi
"""
    return script


# ── Job Submission ────────────────────────────────────────────────────────────

def submit_slurm_job(
    job_id: str,
    project_dir: Path,
    plan_hdf_path: Path,
    geom_ext: str,
    config: SlurmConfig,
    mock: bool = False,
) -> SlurmJobResult:
    """Upload project files to Taiga and submit a SLURM job via sbatch.

    Steps:
    1. Create remote work directory: {taiga_base}/jobs/{job_id}/
    2. rsync project files to remote work dir
    3. Generate job script and write to a local temp file
    4. scp job script to remote work dir
    5. ssh: sbatch job_script.sh → parse "Submitted batch job NNNN"
    6. Return SlurmJobResult with slurm_job_id

    In mock mode, skip all SSH/rsync calls and return status="mock".

    Args:
        job_id:       RAS Agent job UUID.
        project_dir:  Local HEC-RAS project directory.
        plan_hdf_path: Local path to the plan HDF file.
        geom_ext:     Geometry file extension (e.g., "g01").
        config:       SlurmConfig with connection and resource settings.
        mock:         If True, skip SSH and return a mock result.

    Returns:
        SlurmJobResult with submission outcome.

    Raises:
        RuntimeError: If SLURM_USER is not set (non-mock mode only).
    """
    remote_work_dir = f"{config.taiga_base}/jobs/{job_id}"
    remote_plan_hdf = f"{remote_work_dir}/{plan_hdf_path.name}"
    script_name = f"ras_job_{job_id[:8]}.sh"

    if mock:
        logger.info("[slurm] Mock mode — skipping SSH submission for job %s", job_id)
        with tempfile.NamedTemporaryFile(
            suffix=".sh", prefix="ras_job_", delete=False, mode="w"
        ) as tmp:
            script_content = generate_job_script(
                job_id=job_id,
                plan_hdf=remote_plan_hdf,
                geom_ext=geom_ext,
                output_dir=remote_work_dir,
                config=config,
            )
            tmp.write(script_content)
            local_script = Path(tmp.name)
        return SlurmJobResult(
            slurm_job_id="mock-000000",
            job_script_path=local_script,
            remote_work_dir=remote_work_dir,
            status="mock",
        )

    if not config.user:
        raise RuntimeError(
            "SLURM_USER environment variable not set. "
            "Set your University of Illinois NetID in SLURM_USER to submit jobs."
        )

    # Generate job script locally
    script_content = generate_job_script(
        job_id=job_id,
        plan_hdf=remote_plan_hdf,
        geom_ext=geom_ext,
        output_dir=remote_work_dir,
        config=config,
    )
    with tempfile.NamedTemporaryFile(
        suffix=".sh", prefix="ras_job_", delete=False, mode="w"
    ) as tmp:
        tmp.write(script_content)
        local_script = Path(tmp.name)

    try:
        # Step 1: Create remote work dir and logs dir
        mkdir_cmd = (
            f"mkdir -p {remote_work_dir} {config.taiga_base}/logs"
        )
        result = _run_ssh(mkdir_cmd, config)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to create remote work dir {remote_work_dir}: {result.stderr}"
            )
        logger.debug("[slurm] Created remote dir %s", remote_work_dir)

        # Step 2: rsync project files to remote work dir
        rsync_args = ["rsync", "-az", "--exclude=*.tmp.hdf"]
        if config.ssh_key:
            rsync_args += ["-e", f"ssh -i {config.ssh_key} -o BatchMode=yes"]
        else:
            rsync_args += ["-e", "ssh -o BatchMode=yes -o StrictHostKeyChecking=no"]
        target = f"{config.user}@{config.host}" if config.user else config.host
        rsync_args += [f"{project_dir}/", f"{target}:{remote_work_dir}/"]
        rsync_result = subprocess.run(rsync_args, capture_output=True, text=True, timeout=300)
        if rsync_result.returncode != 0:
            raise RuntimeError(
                f"rsync to cluster failed: {rsync_result.stderr}"
            )
        logger.info("[slurm] Synced project files to %s:%s", config.host, remote_work_dir)

        # Step 3: scp job script to remote
        remote_script = f"{remote_work_dir}/{script_name}"
        scp_args = ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no"]
        if config.ssh_key:
            scp_args += ["-i", config.ssh_key]
        scp_args += [str(local_script), f"{target}:{remote_script}"]
        scp_result = subprocess.run(scp_args, capture_output=True, text=True, timeout=60)
        if scp_result.returncode != 0:
            raise RuntimeError(f"scp job script failed: {scp_result.stderr}")
        logger.debug("[slurm] Copied job script to %s:%s", config.host, remote_script)

        # Step 4: sbatch
        sbatch_cmd = f"sbatch {remote_script}"
        sbatch_result = _run_ssh(sbatch_cmd, config, timeout=60)
        if sbatch_result.returncode != 0:
            return SlurmJobResult(
                slurm_job_id="",
                job_script_path=local_script,
                remote_work_dir=remote_work_dir,
                status="failed",
                stderr=sbatch_result.stderr,
            )

        # Parse "Submitted batch job 12345"
        match = re.search(r"Submitted batch job (\d+)", sbatch_result.stdout)
        if not match:
            raise RuntimeError(
                f"Could not parse sbatch output: {sbatch_result.stdout!r}"
            )
        slurm_job_id = match.group(1)
        logger.info(
            "[slurm] Submitted SLURM job %s for RAS job %s (partition=%s)",
            slurm_job_id, job_id, config.partition,
        )
        return SlurmJobResult(
            slurm_job_id=slurm_job_id,
            job_script_path=local_script,
            remote_work_dir=remote_work_dir,
            status="submitted",
        )

    except Exception:
        raise


# ── Job Status ────────────────────────────────────────────────────────────────

def check_slurm_status(slurm_job_id: str, config: SlurmConfig) -> str:
    """Query SLURM job status via squeue over SSH.

    Args:
        slurm_job_id: SLURM job ID (numeric string, e.g. "12345").
        config:       SlurmConfig with connection parameters.

    Returns:
        One of: "PENDING", "RUNNING", "COMPLETED", "FAILED",
        "CANCELLED", "TIMEOUT", or "UNKNOWN".
    """
    cmd = f"squeue -j {slurm_job_id} -h -o '%T' 2>/dev/null || sacct -j {slurm_job_id} -n -o State --parsable2 2>/dev/null | head -1"
    try:
        result = _run_ssh(cmd, config, timeout=30)
    except (subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
        logger.warning("[slurm] Status check failed for job %s: %s", slurm_job_id, exc)
        return "UNKNOWN"

    output = result.stdout.strip().upper()
    if not output:
        # Job not in squeue (completed or not found) — try sacct
        sacct_cmd = f"sacct -j {slurm_job_id} -n -o State --parsable2 2>/dev/null | head -1"
        try:
            sacct = _run_ssh(sacct_cmd, config, timeout=30)
            output = sacct.stdout.strip().upper()
        except (subprocess.TimeoutExpired, subprocess.SubprocessError):
            return "UNKNOWN"

    # Normalize sacct state (may include + suffix like "COMPLETED+")
    output = output.split("+")[0].strip()

    valid_states = {"PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}
    if output in valid_states:
        return output
    # squeue state abbreviations
    state_map = {
        "PD": "PENDING",
        "R": "RUNNING",
        "CG": "RUNNING",   # completing
        "CD": "COMPLETED",
        "F": "FAILED",
        "CA": "CANCELLED",
        "TO": "TIMEOUT",
    }
    return state_map.get(output, "UNKNOWN")


def wait_for_slurm_job(
    slurm_job_id: str,
    config: SlurmConfig,
    poll_interval_sec: int = 60,
    timeout_sec: int = 14400,
) -> str:
    """Poll squeue/sacct until a SLURM job reaches a terminal state.

    Args:
        slurm_job_id:      SLURM job ID to monitor.
        config:            SlurmConfig with connection parameters.
        poll_interval_sec: Seconds between status polls (default: 60).
        timeout_sec:       Maximum wait time in seconds (default: 4 hours).

    Returns:
        Final SLURM state: "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
        or "UNKNOWN" if monitoring timed out.
    """
    terminal_states = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"}
    deadline = time.monotonic() + timeout_sec
    logger.info(
        "[slurm] Waiting for SLURM job %s (poll every %ds, timeout %ds)",
        slurm_job_id, poll_interval_sec, timeout_sec,
    )

    while time.monotonic() < deadline:
        status = check_slurm_status(slurm_job_id, config)
        logger.debug("[slurm] Job %s status: %s", slurm_job_id, status)
        if status in terminal_states:
            logger.info("[slurm] Job %s reached terminal state: %s", slurm_job_id, status)
            return status
        time.sleep(poll_interval_sec)

    logger.error(
        "[slurm] Monitoring timeout (%ds) for SLURM job %s — last status: UNKNOWN",
        timeout_sec, slurm_job_id,
    )
    return "UNKNOWN"


# ── Results Fetch ─────────────────────────────────────────────────────────────

def fetch_results(
    job_id: str,
    remote_work_dir: str,
    local_output_dir: Path,
    config: SlurmConfig,
) -> list[Path]:
    """rsync results back from the cluster to a local output directory.

    Args:
        job_id:           RAS Agent job UUID (for logging).
        remote_work_dir:  Working directory path on the cluster.
        local_output_dir: Local directory to receive downloaded files.
        config:           SlurmConfig with connection parameters.

    Returns:
        List of local paths that were downloaded.

    Raises:
        RuntimeError: If rsync fails.
    """
    local_output_dir = Path(local_output_dir)
    local_output_dir.mkdir(parents=True, exist_ok=True)

    target = f"{config.user}@{config.host}" if config.user else config.host
    rsync_args = ["rsync", "-az"]
    if config.ssh_key:
        rsync_args += ["-e", f"ssh -i {config.ssh_key} -o BatchMode=yes"]
    else:
        rsync_args += ["-e", "ssh -o BatchMode=yes -o StrictHostKeyChecking=no"]
    rsync_args += [
        "--include=*.hdf",
        "--include=*.log",
        "--exclude=*.tmp.hdf",
        f"{target}:{remote_work_dir}/",
        f"{local_output_dir}/",
    ]

    result = subprocess.run(rsync_args, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"rsync results fetch failed for job {job_id}: {result.stderr}"
        )

    fetched = list(local_output_dir.iterdir())
    logger.info(
        "[slurm] Fetched %d files from %s:%s → %s",
        len(fetched), config.host, remote_work_dir, local_output_dir,
    )
    return fetched


# ── Convenience ───────────────────────────────────────────────────────────────

def slurm_config_from_env() -> Optional[SlurmConfig]:
    """Return a SlurmConfig loaded from environment variables, or None if SLURM_USER is not set.

    Returns:
        SlurmConfig if SLURM_USER is set in the environment, else None.
    """
    if not os.environ.get("SLURM_USER"):
        return None
    return SlurmConfig.from_env()
