"""
windows_agent.py — Windows mesh generation interface for RAS Agent

Purpose
-------
This module defines the contract between the Linux pipeline and the Windows
workstation (CHAMP Dell Precision 5860).  The Windows agent accepts a watershed
perimeter + terrain path and returns a mesh HDF5 file path.  It is the bridge
between ras-agent's Linux pipeline and Windows-only HEC-RAS mesh tools
(RASMapper GUI and ras_commander.gui automation).

Current status
--------------
STUB — ``local`` and ``remote`` modes are NOT yet implemented.  Only ``mock``
mode is functional.  The stubs will be filled in after a call with
Bill Katzenmeyer (ras_commander maintainer / CLB Engineering) and
Ajith Sundarraj (CLB Engineering, RASMapper automation).

Depends on
----------
- ``ras_commander.gui`` — Bill Katzenmeyer / CLB Engineering
  (``feature/gui-subpackage`` branch, not yet on PyPI as of 2026-03-16)
- ``ras_commander`` ≥ 0.89 (already in requirements.txt)

Hardware target
---------------
CHAMP Dell Precision 5860
  - CPU:  Intel Xeon W5-2545 (12-core, 3.7 GHz base)
  - RAM:  128 GB DDR5 ECC
  - GPU:  NVIDIA RTX A4500 (20 GB VRAM) for RAS 2D GPU acceleration
  - OS:   Windows 11 Pro (HEC-RAS 6.6 or 2025 installed)

Fill-in point
-------------
Replace the ``NotImplementedError`` stubs in ``_generate_local`` and
``_generate_remote`` after the joint call with Bill Katzenmeyer + Ajith
Sundarraj (CLB Engineering).  See ``docs/KNOWLEDGE.md`` for full context.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

from __future__ import annotations

import argparse
import json
import logging
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
        Dispatch mode.  ``"local"`` drives RASMapper on the same Windows
        machine via ``ras_commander.gui``; ``"remote"`` POSTs to a lightweight
        HTTP agent running on a networked Windows workstation; ``"mock"``
        returns a synthetic result for testing.
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
    """

    mode: str = "local"
    host: str = "localhost"
    port: int = 8766
    hecras_version: str = "6.6"
    timeout_sec: int = 3600
    rdp_capable: bool = False


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
        Implementation strategy used: ``"gui_automation"`` | ``"api"`` |
        ``"mock"``.
    """

    success: bool
    mesh_hdf_path: str = ""
    geometry_file_path: str = ""
    cell_count: int = 0
    error: str = ""
    duration_sec: float = 0.0
    strategy: str = ""


# ── Agent class ───────────────────────────────────────────────────────────────

class WindowsAgent:
    """Interface to the Windows mesh generation node.

    Dispatches mesh generation jobs to one of three back-ends depending on
    ``config.mode``:

    - ``"local"``  — drives RASMapper directly via ``ras_commander.gui``
                     (Windows only; stub until Bill's subpackage is ready)
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

        In ``mock`` mode this always succeeds.  In ``local`` and ``remote``
        modes the check is not yet implemented — the caller should treat the
        agent as unavailable until the stubs are filled in.

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

        raise NotImplementedError(
            "health_check() is not yet implemented for local/remote modes. "
            "Will be implemented after the call with Bill Katzenmeyer + Ajith "
            "Sundarraj (CLB Engineering) to align on the ras_commander.gui API."
        )

    # ── Private back-ends ─────────────────────────────────────────────────────

    def _generate_local(self, request: MeshRequest) -> MeshResult:
        """Drive RASMapper on the local Windows machine via ras_commander.gui.

        .. note::
            **NOT IMPLEMENTED.**  This stub will be replaced with real
            ``ras_commander.gui`` automation after a call with Bill Katzenmeyer
            (ras_commander maintainer) and Ajith Sundarraj (CLB Engineering).

            Expected implementation outline:
            1. Clone the template project into ``request.output_dir``.
            2. Write the watershed perimeter to the ``.g##`` geometry file using
               :func:`model_builder._write_perimeter_to_geometry_file`.
            3. Call ``ras_commander.gui.RasMapperSession.generate_mesh()`` with
               ``cell_size=request.cell_size_m`` targeting ``request.area_name``.
            4. Wait for RASMapper to close / signal completion (poll HDF5 mtime
               or use the gui subpackage's completion callback).
            5. Return the path to the updated ``<project>.g##.hdf`` mesh file.

            Hardware target: CHAMP Dell Precision 5860 (Windows 11 Pro,
            HEC-RAS 6.6, NVIDIA RTX A4500 GPU acceleration enabled).
        """
        raise NotImplementedError(
            "_generate_local() requires ras_commander.gui (feature/gui-subpackage). "
            "Fill in after call with Bill Katzenmeyer + Ajith Sundarraj."
        )

    def _generate_remote(self, request: MeshRequest) -> MeshResult:
        """POST the mesh request to a remote Windows agent over HTTP.

        .. note::
            **NOT IMPLEMENTED.**  This stub will be replaced after the remote
            agent protocol is agreed upon with Bill Katzenmeyer / CLB
            Engineering.

            Expected implementation outline:
            1. Serialise ``request`` to JSON (``dataclasses.asdict``).
            2. POST to ``http://{config.host}:{config.port}/generate-mesh``.
            3. Poll ``GET /jobs/{job_id}`` until complete or ``timeout_sec``
               elapsed.
            4. Download the mesh HDF5 to ``request.output_dir``.
            5. Return a populated :class:`MeshResult`.

            The remote agent is a lightweight FastAPI/Flask server running on
            the CHAMP Dell Precision 5860 (or any Windows machine with
            HEC-RAS installed).
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
