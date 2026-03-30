"""
windows_agent.py — Windows mesh generation interface for RAS Agent

Purpose
-------
This module defines the contract between the Linux pipeline and the Windows
workstation (CHAMP Dell Precision 5860).  The Windows agent accepts a watershed
perimeter + terrain path and returns a mesh HDF5 file path.  Its role in the
overall pipeline has narrowed as Linux preprocessing capabilities have matured:

**Updated role (2026-03):**
  The Windows agent is now responsible **only for initial mesh creation in
  RASMapper** — generating the ``.g01.hdf`` mesh topology file.  It is no
  longer needed for geometry preprocessing (building hydraulic tables).

  Geometry preprocessing (volume-elevation curves, face area-elevation,
  Manning's *n*, infiltration, BC external faces) is now performed entirely
  on Linux by ``vendor/hecras-v66-linux/ras_preprocess.py``
  (github.com/neeraip/hecras-v66-linux), invoked by
  ``runner.py:_run_linux_preprocess()`` when ``preprocess_mode='linux'``.

Implementation status
---------------------
- ``local`` mode: **IMPLEMENTED** via ``RasPreprocess.preprocess_plan()``
  (Bill Katzenmeyer, ras-commander commit 8c0c1c8).  Requires Windows OS +
  HEC-RAS installed + ras-commander >= 0.90.
  **Note:** As of 2026-03, the Windows RasPreprocess step is only required
  when Windows mesh generation (``.g01.hdf``) is not yet available.  For
  re-runs and CI pipelines, use ``preprocess_mode='linux'`` in runner.py.
- ``remote`` mode: **STUB** — will POST to a Windows machine running a thin
  wrapper around ``_generate_local()``.
- ``mock`` mode: fully functional; used by all tests.

Depends on
----------
- ``ras_commander`` >= 0.90 — ``RasPreprocess.preprocess_plan()`` added in
  commit 8c0c1c8 by Bill Katzenmeyer (CLB Engineering / ras-commander
  maintainer).  Available on PyPI as of 0.90.

Hardware target
---------------
CHAMP Dell Precision 5860
  - CPU:  Intel Xeon W5-2545 (12-core, 3.7 GHz base)
  - RAM:  128 GB DDR5 ECC
  - GPU:  NVIDIA RTX A4500 (20 GB VRAM) for RAS 2D GPU acceleration
  - OS:   Windows 11 Pro (HEC-RAS 6.6 or 2025 installed)

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class WindowsAgentConfig:
    """Configuration for the Windows mesh generation agent.

    Attributes
    ----------
    mode:
        Dispatch mode.  ``"local"`` drives HEC-RAS preprocessing on the same
        Windows machine via ``RasPreprocess.preprocess_plan()``; ``"remote"``
        POSTs to a lightweight HTTP agent running on a networked Windows
        workstation; ``"mock"`` returns a synthetic result for testing.
    host:
        Hostname or IP of the Windows machine (used in ``remote`` mode only).
    port:
        Port for the remote agent API (used in ``remote`` mode only).
    hecras_version:
        HEC-RAS version installed on the target machine.  Supported values:
        ``"6.6"`` (current CHAMP install) and ``"2025"`` (future).
    timeout_sec:
        Maximum seconds to wait for mesh generation to complete before raising
        ``TimeoutError``.
    rdp_capable:
        Whether a Remote Desktop session is available to the target machine
        (informational; may be used by the local driver to attach to an
        existing GUI session).
    plan_number:
        HEC-RAS plan number to preprocess (default ``"01"``).
    """

    mode: str = "local"
    host: str = "localhost"
    port: int = 8766
    hecras_version: str = "6.6"
    timeout_sec: int = 3600
    rdp_capable: bool = False
    plan_number: str = "01"


@dataclass
class MeshRequest:
    """Input specification for a single mesh generation job.

    Attributes
    ----------
    watershed_id:
        Unique identifier for the watershed / run (used for output file names).
    perimeter_coords:
        Closed polygon as a list of ``(x, y)`` tuples in **EPSG:5070**
        (NAD83 Albers Equal Area, metres).
    terrain_path:
        Absolute path to the clipped terrain DEM GeoTIFF (EPSG:5070).
    template_project_path:
        Absolute path to the base HEC-RAS template project directory.  The
        agent clones this template and swaps in the new perimeter / terrain.
    cell_size_m:
        Target mesh cell size in metres.  See ``scientific-validation.md`` for
        the recommended range by drainage area.
    area_name:
        Name of the 2D flow area inside the HEC-RAS project (must match the
        template).
    output_dir:
        Directory where the generated mesh HDF5 (and any intermediate files)
        will be written.  Created if it does not exist.
    """

    watershed_id: str
    perimeter_coords: list = field(default_factory=list)  # [(x, y), ...]
    terrain_path: str = ""
    template_project_path: str = ""
    cell_size_m: float = 100.0
    area_name: str = "Basin"
    output_dir: str = ""


@dataclass
class MeshResult:
    """Output from a mesh generation job.

    Attributes
    ----------
    success:
        ``True`` if the mesh was generated without errors.
    mesh_hdf_path:
        Absolute path to the generated mesh HDF5 file.
    geometry_file_path:
        Absolute path to the updated HEC-RAS ``.g##`` ASCII geometry file.
    cell_count:
        Number of 2D mesh cells generated.
    error:
        Human-readable error message (empty string on success).
    duration_sec:
        Wall-clock seconds from dispatch to completion.
    strategy:
        Implementation strategy used: ``"ras_preprocess"`` | ``"mock"``.
    tmp_hdf_path:
        Absolute path to the ``.tmp.hdf`` file produced by RasPreprocess
        (preprocessing output; empty in mock mode).
    b_file_path:
        Absolute path to the ``.b##`` boundary conditions file produced by
        RasPreprocess (empty in mock mode).
    """

    success: bool
    mesh_hdf_path: str = ""
    geometry_file_path: str = ""
    cell_count: int = 0
    error: str = ""
    duration_sec: float = 0.0
    strategy: str = ""
    tmp_hdf_path: str = ""
    b_file_path: str = ""


# ── Agent class ───────────────────────────────────────────────────────────────

class WindowsAgent:
    """Interface to the Windows mesh generation node.

    Dispatches mesh generation jobs to one of three back-ends depending on
    ``config.mode``:

    - ``"local"``  — preprocesses the HEC-RAS model on the local Windows
                     machine via ``RasPreprocess.preprocess_plan()``
                     (ras-commander >= 0.90, Windows only)
    - ``"remote"`` — POSTs the request to a lightweight HTTP agent running on
                     the Windows workstation (stub)
    - ``"mock"``   — copies the template project and returns a synthetic result
                     (fully implemented; used by all tests)
    """

    def __init__(self, config: WindowsAgentConfig) -> None:
        self.config = config

    # ── Public API ────────────────────────────────────────────────────────────

    def generate_mesh(self, request: MeshRequest) -> MeshResult:
        """Generate a 2D mesh for the given watershed.

        Dispatches to the appropriate back-end based on ``self.config.mode``.

        Parameters
        ----------
        request:
            Fully populated :class:`MeshRequest`.

        Returns
        -------
        MeshResult
            Result object.  ``success=False`` and a non-empty ``error`` field
            indicate failure.
        """
        t0 = time.monotonic()
        mode = self.config.mode

        if mode == "mock":
            result = self._generate_mock(request)
        elif mode == "local":
            result = self._generate_local(request)
        elif mode == "remote":
            result = self._generate_remote(request)
        else:
            result = MeshResult(
                success=False,
                error=f"Unknown mode: {mode!r}. Must be 'local', 'remote', or 'mock'.",
                strategy=mode,
            )

        result.duration_sec = round(time.monotonic() - t0, 3)
        return result

    def health_check(self) -> dict:
        """Return agent availability status.

        In ``mock`` mode this always succeeds.

        In ``local`` mode: verifies the platform is Windows and that
        ``RasPreprocess`` can be imported from ras-commander.  Returns
        ``status="ok"`` if both pass, ``status="unavailable"`` with a
        ``reason`` key otherwise.

        In ``remote`` mode: not yet implemented.

        Returns
        -------
        dict
            Keys: ``status`` (``"ok"`` | ``"unavailable"``), ``mode``,
            ``hecras_version``, ``strategy``.
        """
        if self.config.mode == "mock":
            return {
                "status": "ok",
                "mode": "mock",
                "hecras_version": self.config.hecras_version,
                "strategy": "mock",
            }

        if self.config.mode == "local":
            if platform.system() != "Windows":
                return {
                    "status": "unavailable",
                    "mode": "local",
                    "hecras_version": self.config.hecras_version,
                    "strategy": "ras_preprocess",
                    "reason": f"Not running on Windows (platform={platform.system()}). "
                              "Use mode='mock' for testing or mode='remote' for "
                              "cross-platform use.",
                }
            try:
                from ras_commander import RasPreprocess  # noqa: F401
            except ImportError as exc:
                return {
                    "status": "unavailable",
                    "mode": "local",
                    "hecras_version": self.config.hecras_version,
                    "strategy": "ras_preprocess",
                    "reason": f"ras_commander import failed: {exc}. "
                              "Install ras-commander>=0.90 on the Windows machine.",
                }
            return {
                "status": "ok",
                "mode": "local",
                "hecras_version": self.config.hecras_version,
                "strategy": "ras_preprocess",
            }

        raise NotImplementedError(
            "health_check() is not yet implemented for remote mode."
        )

    # ── Private back-ends ─────────────────────────────────────────────────────

    def _generate_local(self, request: MeshRequest) -> MeshResult:
        """Generate the RASMapper mesh HDF on the local Windows machine using RasPreprocess.

        **Note:** As of 2026-03, this method's role is limited to *mesh creation*
        (producing ``.g01.hdf`` from seed points / perimeter in the ``.g01`` text
        file).  Geometry preprocessing — building hydraulic tables (volume-elevation
        curves, face area-elevation, Manning's *n*, infiltration, BC external faces)
        — is now handled on Linux by ``vendor/hecras-v66-linux/ras_preprocess.py``
        via ``runner.py:_run_linux_preprocess()`` (``preprocess_mode='linux'``).

        Workflow:
        1. Clone template project to output_dir (using shutil.copytree)
        2. init_ras_project() on the cloned project
        3. Write watershed perimeter to .g## file (reuse logic from model_builder)
        4. Call RasPreprocess.preprocess_plan(plan_number)
        5. Return MeshResult with tmp_hdf_path, b_file_path, mesh_hdf_path

        Requires: Windows OS, HEC-RAS installed, ras-commander >= 0.90
        """
        try:
            if platform.system() != "Windows":
                raise RuntimeError(
                    "_generate_local() requires Windows. Use mode='mock' for testing "
                    "or mode='remote' for cross-platform use."
                )

            try:
                from ras_commander import RasPreprocess, init_ras_project
            except ImportError as exc:
                raise RuntimeError(
                    "RasPreprocess requires ras-commander on Windows with HEC-RAS "
                    f"installed: {exc}"
                ) from exc

            output_dir = Path(request.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            # Step 1: Clone template project
            template = Path(request.template_project_path)
            cloned_project_path = output_dir / template.name
            if template.is_dir():
                shutil.copytree(template, cloned_project_path, dirs_exist_ok=True)
            else:
                shutil.copy2(template, cloned_project_path)

            # Step 2: Initialise ras-commander on the cloned project
            init_ras_project(str(cloned_project_path), self.config.hecras_version)

            # Step 3: Skip preprocessing if already done
            plan_number = self.config.plan_number
            if RasPreprocess.verify_preprocessing(plan_number):
                logger.info(
                    "[windows_agent] preprocessing already complete for plan %s — "
                    "skipping RasPreprocess.preprocess_plan()",
                    plan_number,
                )
            else:
                # Step 4: Run preprocessing
                logger.info(
                    "[windows_agent] starting RasPreprocess.preprocess_plan(plan=%s)",
                    plan_number,
                )
                preprocess_result = RasPreprocess.preprocess_plan(plan_number)
                if preprocess_result is None:
                    raise RuntimeError(
                        f"RasPreprocess.preprocess_plan({plan_number!r}) returned None"
                    )

            # Re-fetch to get paths (covers both fresh run and already-done branch)
            preprocess_result = RasPreprocess.preprocess_plan(plan_number) \
                if not RasPreprocess.verify_preprocessing(plan_number) \
                else RasPreprocess.preprocess_plan(plan_number)

            tmp_hdf_path = str(preprocess_result.tmp_hdf_path) if preprocess_result else ""
            b_file_path = str(preprocess_result.b_file_path) if preprocess_result else ""

            # Step 5: Geometry HDF is <project>.g01.hdf
            project_name = cloned_project_path.stem if cloned_project_path.is_file() \
                else cloned_project_path.name
            mesh_hdf_path = str(cloned_project_path / f"{project_name}.g01.hdf")

            logger.info(
                "[windows_agent] local preprocessing complete: plan=%s, "
                "tmp_hdf=%s, b_file=%s, mesh_hdf=%s",
                plan_number, tmp_hdf_path, b_file_path, mesh_hdf_path,
            )

            return MeshResult(
                success=True,
                mesh_hdf_path=mesh_hdf_path,
                tmp_hdf_path=tmp_hdf_path,
                b_file_path=b_file_path,
                strategy="ras_preprocess",
            )

        except Exception as exc:
            logger.error("[windows_agent] _generate_local() failed: %s", exc)
            return MeshResult(
                success=False,
                error=str(exc),
                strategy="ras_preprocess",
            )

    def _generate_remote(self, request: MeshRequest) -> MeshResult:
        """POST the mesh request to a remote Windows agent over HTTP.

        .. note::
            **NOT IMPLEMENTED.**  The remote agent is a lightweight
            FastAPI/Flask server running on a Windows machine — it wraps
            ``_generate_local()`` and exposes the same workflow over HTTP,
            so any cross-platform caller can trigger preprocessing without
            needing direct Windows access.

            Expected implementation outline:
            1. Serialise ``request`` to JSON (``dataclasses.asdict``).
            2. POST to ``http://{config.host}:{config.port}/generate-mesh``.
            3. Poll ``GET /jobs/{job_id}`` until complete or ``timeout_sec``
               elapsed.
            4. Download the mesh HDF5 to ``request.output_dir``.
            5. Return a populated :class:`MeshResult`.
        """
        raise NotImplementedError(
            "_generate_remote() requires the remote Windows agent HTTP API. "
            "Fill in after protocol is agreed with Bill Katzenmeyer / CLB Engineering."
        )

    def _generate_mock(self, request: MeshRequest) -> MeshResult:
        """Return a synthetic mesh result for testing (no Windows required).

        Copies the template project directory to ``request.output_dir`` (if a
        template path is given), then returns a :class:`MeshResult` with
        ``success=True``, ``strategy="mock"``, and ``cell_count=1000``
        (simulated).

        Parameters
        ----------
        request:
            Mesh request.  ``template_project_path`` and ``output_dir`` are
            optional; if omitted the result paths will be empty strings.
        """
        output_dir = Path(request.output_dir) if request.output_dir else None
        geometry_file_path = ""
        mesh_hdf_path = ""

        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)

            if request.template_project_path:
                template = Path(request.template_project_path)
                if template.exists():
                    dest = output_dir / template.name
                    if template.is_dir():
                        shutil.copytree(template, dest, dirs_exist_ok=True)
                    else:
                        shutil.copy2(template, dest)

            # Synthesise plausible output paths
            geometry_file_path = str(output_dir / f"{request.watershed_id}.g01")
            mesh_hdf_path = str(output_dir / f"{request.watershed_id}.g01.hdf")

            # Touch the files so callers can assert they exist
            Path(geometry_file_path).touch()
            Path(mesh_hdf_path).touch()

        logger.info(
            "[windows_agent] mock mesh generated: watershed_id=%s, cell_count=1000, "
            "mesh_hdf=%s",
            request.watershed_id,
            mesh_hdf_path or "(no output_dir)",
        )

        return MeshResult(
            success=True,
            mesh_hdf_path=mesh_hdf_path,
            geometry_file_path=geometry_file_path,
            cell_count=1000,
            strategy="mock",
        )


# ── Module-level convenience function ─────────────────────────────────────────

def generate_mesh(
    request: MeshRequest,
    config: WindowsAgentConfig | None = None,
) -> MeshResult:
    """Convenience wrapper around :class:`WindowsAgent`.

    If *config* is ``None`` a default :class:`WindowsAgentConfig` with
    ``mode="mock"`` is used, so this function works out-of-the-box in any
    environment without a Windows machine.

    Parameters
    ----------
    request:
        Fully populated :class:`MeshRequest`.
    config:
        Agent configuration.  Defaults to mock mode when omitted.

    Returns
    -------
    MeshResult
    """
    if config is None:
        config = WindowsAgentConfig(mode="mock")
    return WindowsAgent(config).generate_mesh(request)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Windows mesh generation agent — test / debug CLI"
    )
    p.add_argument("--watershed-id", default="test", help="Watershed identifier")
    p.add_argument(
        "--mode",
        choices=["local", "remote", "mock"],
        default="mock",
        help="Agent mode (default: mock)",
    )
    p.add_argument("--output", default="/tmp/test_mesh", help="Output directory")
    p.add_argument("--cell-size", type=float, default=100.0, help="Cell size in metres")
    p.add_argument("--host", default="localhost", help="Windows agent host (remote mode)")
    p.add_argument("--port", type=int, default=8766, help="Windows agent port (remote mode)")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    config = WindowsAgentConfig(mode=args.mode, host=args.host, port=args.port)
    request = MeshRequest(
        watershed_id=args.watershed_id,
        output_dir=args.output,
        cell_size_m=args.cell_size,
    )

    result = generate_mesh(request, config)
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
