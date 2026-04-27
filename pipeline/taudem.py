"""
taudem.py — direct TauDEM CLI wrapper

Provides a thin Python interface over the TauDEM 5.x command-line tools used
by the Illinois-first watershed delineation workflow.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TauDemError(RuntimeError):
    """Raised when TauDEM is unavailable or a command fails."""


@dataclass
class TauDemCommandResult:
    """Structured result for one TauDEM command invocation."""

    executable: str
    command: list[str]
    outputs: dict[str, Path]
    returncode: int
    stdout: str
    stderr: str


class TauDem:
    """Static wrapper for TauDEM 5.x executables."""

    EXECUTABLES = {
        "pit_remove": "PitRemove",
        "d8_flow_dir": "D8FlowDir",
        "dinf_flow_dir": "DinfFlowDir",
        "area_d8": "AreaD8",
        "area_dinf": "AreaDinf",
        "grid_net": "Gridnet",
        "threshold": "Threshold",
        "move_outlets_to_streams": "MoveOutletsToStreams",
        "stream_net": "StreamNet",
        "gage_watershed": "GageWatershed",
    }

    @staticmethod
    def detect_installation(executable_dir: Optional[Path] = None) -> dict:
        """Return installation details for the TauDEM toolchain."""
        resolved_dir = Path(executable_dir) if executable_dir else None
        executables = {}
        missing = []

        for key, exe_name in TauDem.EXECUTABLES.items():
            exe_path = TauDem._resolve_executable(exe_name, resolved_dir, required=False)
            if exe_path is None:
                missing.append(exe_name)
            else:
                executables[exe_name] = exe_path

        mpiexec = TauDem._resolve_mpiexec(resolved_dir)
        install_dir = None
        if executables:
            install_dir = next(iter(executables.values())).parent

        return {
            "installed": len(missing) == 0,
            "executable_dir": install_dir,
            "executables": executables,
            "missing": missing,
            "mpiexec": mpiexec,
        }

    @staticmethod
    def validate_environment(
        required: Optional[list[str]] = None,
        executable_dir: Optional[Path] = None,
    ) -> dict:
        """Validate that the required TauDEM executables are available."""
        info = TauDem.detect_installation(executable_dir)
        required = required or list(TauDem.EXECUTABLES.values())
        missing = [name for name in required if name not in info["executables"]]
        if missing:
            raise TauDemError(
                "TauDEM executables not found: "
                + ", ".join(sorted(missing))
                + ". Install TauDEM 5.x and ensure the executables are on PATH "
                + "or pass executable_dir explicitly."
            )
        return info

    @staticmethod
    def pit_remove(
        dem_path: Path,
        filled_dem_path: Path,
        depmask_path: Optional[Path] = None,
        four_way: bool = False,
        executable_dir: Optional[Path] = None,
        processes: int = 1,
    ) -> TauDemCommandResult:
        args = ["-z", str(dem_path), "-fel", str(filled_dem_path)]
        if four_way:
            args.append("-4way")
        if depmask_path is not None:
            args.extend(["-depmask", str(depmask_path)])
        return TauDem._run(
            "PitRemove",
            args,
            {"fel": Path(filled_dem_path)},
            executable_dir=executable_dir,
            processes=processes,
        )

    @staticmethod
    def d8_flow_dir(
        filled_dem_path: Path,
        pfile: Path,
        sd8file: Path,
        executable_dir: Optional[Path] = None,
        processes: int = 1,
    ) -> TauDemCommandResult:
        return TauDem._run(
            "D8FlowDir",
            ["-fel", str(filled_dem_path), "-p", str(pfile), "-sd8", str(sd8file)],
            {"p": Path(pfile), "sd8": Path(sd8file)},
            executable_dir=executable_dir,
            processes=processes,
        )

    @staticmethod
    def dinf_flow_dir(
        filled_dem_path: Path,
        angfile: Path,
        slpfile: Path,
        executable_dir: Optional[Path] = None,
        processes: int = 1,
    ) -> TauDemCommandResult:
        return TauDem._run(
            "DinfFlowDir",
            ["-fel", str(filled_dem_path), "-ang", str(angfile), "-slp", str(slpfile)],
            {"ang": Path(angfile), "slp": Path(slpfile)},
            executable_dir=executable_dir,
            processes=processes,
        )

    @staticmethod
    def area_d8(
        pfile: Path,
        ad8file: Path,
        outletfile: Optional[Path] = None,
        wgfile: Optional[Path] = None,
        edge_contamination: bool = True,
        executable_dir: Optional[Path] = None,
        processes: int = 1,
    ) -> TauDemCommandResult:
        args = ["-p", str(pfile), "-ad8", str(ad8file)]
        if outletfile is not None:
            args.extend(["-o", str(outletfile)])
        if wgfile is not None:
            args.extend(["-wg", str(wgfile)])
        if not edge_contamination:
            args.append("-nc")
        return TauDem._run(
            "AreaD8",
            args,
            {"ad8": Path(ad8file)},
            executable_dir=executable_dir,
            processes=processes,
        )

    @staticmethod
    def area_dinf(
        angfile: Path,
        scafile: Path,
        outletfile: Optional[Path] = None,
        wgfile: Optional[Path] = None,
        edge_contamination: bool = True,
        executable_dir: Optional[Path] = None,
        processes: int = 1,
    ) -> TauDemCommandResult:
        args = ["-ang", str(angfile), "-sca", str(scafile)]
        if outletfile is not None:
            args.extend(["-o", str(outletfile)])
        if wgfile is not None:
            args.extend(["-wg", str(wgfile)])
        if not edge_contamination:
            args.append("-nc")
        return TauDem._run(
            "AreaDinf",
            args,
            {"sca": Path(scafile)},
            executable_dir=executable_dir,
            processes=processes,
        )

    @staticmethod
    def grid_net(
        pfile: Path,
        plenfile: Path,
        tlenfile: Path,
        gordfile: Path,
        outletfile: Optional[Path] = None,
        maskfile: Optional[Path] = None,
        threshold: Optional[float] = None,
        executable_dir: Optional[Path] = None,
        processes: int = 1,
    ) -> TauDemCommandResult:
        args = [
            "-p",
            str(pfile),
            "-plen",
            str(plenfile),
            "-tlen",
            str(tlenfile),
            "-gord",
            str(gordfile),
        ]
        if outletfile is not None:
            args.extend(["-o", str(outletfile)])
        if maskfile is not None:
            args.extend(["-mask", str(maskfile)])
            if threshold is not None:
                args.extend(["-thresh", str(threshold)])
        return TauDem._run(
            "Gridnet",
            args,
            {
                "plen": Path(plenfile),
                "tlen": Path(tlenfile),
                "gord": Path(gordfile),
            },
            executable_dir=executable_dir,
            processes=processes,
        )

    @staticmethod
    def threshold(
        ssafile: Path,
        srcfile: Path,
        thresh: float,
        maskfile: Optional[Path] = None,
        executable_dir: Optional[Path] = None,
        processes: int = 1,
    ) -> TauDemCommandResult:
        args = ["-ssa", str(ssafile), "-src", str(srcfile), "-thresh", str(thresh)]
        if maskfile is not None:
            args.extend(["-mask", str(maskfile)])
        return TauDem._run(
            "Threshold",
            args,
            {"src": Path(srcfile)},
            executable_dir=executable_dir,
            processes=processes,
        )

    @staticmethod
    def move_outlets_to_streams(
        pfile: Path,
        srcfile: Path,
        outletfile: Path,
        moved_outletfile: Path,
        maxdist: Optional[int] = None,
        executable_dir: Optional[Path] = None,
        processes: int = 1,
    ) -> TauDemCommandResult:
        args = [
            "-p",
            str(pfile),
            "-src",
            str(srcfile),
            "-o",
            str(outletfile),
            "-om",
            str(moved_outletfile),
        ]
        if maxdist is not None:
            args.extend(["-md", str(maxdist)])
        return TauDem._run(
            "MoveOutletsToStreams",
            args,
            {"outlets": Path(moved_outletfile)},
            executable_dir=executable_dir,
            processes=processes,
        )

    @staticmethod
    def stream_net(
        filled_dem_path: Path,
        pfile: Path,
        ad8file: Path,
        srcfile: Path,
        ordfile: Path,
        treefile: Path,
        coordfile: Path,
        netfile: Path,
        wfile: Path,
        outletfile: Optional[Path] = None,
        single_watershed: bool = False,
        executable_dir: Optional[Path] = None,
        processes: int = 1,
    ) -> TauDemCommandResult:
        args = [
            "-fel",
            str(filled_dem_path),
            "-p",
            str(pfile),
            "-ad8",
            str(ad8file),
            "-src",
            str(srcfile),
            "-ord",
            str(ordfile),
            "-tree",
            str(treefile),
            "-coord",
            str(coordfile),
            "-net",
            str(netfile),
            "-w",
            str(wfile),
        ]
        if outletfile is not None:
            args.extend(["-o", str(outletfile)])
        if single_watershed:
            args.append("-sw")
        return TauDem._run(
            "StreamNet",
            args,
            {
                "ord": Path(ordfile),
                "tree": Path(treefile),
                "coord": Path(coordfile),
                "net": Path(netfile),
                "w": Path(wfile),
            },
            executable_dir=executable_dir,
            processes=processes,
        )

    @staticmethod
    def gage_watershed(
        pfile: Path,
        gwfile: Path,
        outletfile: Optional[Path] = None,
        idfile: Optional[Path] = None,
        executable_dir: Optional[Path] = None,
        processes: int = 1,
    ) -> TauDemCommandResult:
        args = ["-p", str(pfile), "-gw", str(gwfile)]
        if outletfile is not None:
            args.extend(["-o", str(outletfile)])
        if idfile is not None:
            args.extend(["-id", str(idfile)])
        outputs = {"gw": Path(gwfile)}
        if idfile is not None:
            outputs["id"] = Path(idfile)
        return TauDem._run(
            "GageWatershed",
            args,
            outputs,
            executable_dir=executable_dir,
            processes=processes,
        )

    @staticmethod
    def _run(
        exe_name: str,
        args: list[str],
        outputs: dict[str, Path],
        executable_dir: Optional[Path] = None,
        processes: int = 1,
    ) -> TauDemCommandResult:
        exe_path = TauDem._resolve_executable(exe_name, executable_dir)
        command = TauDem._build_command(exe_path, args, executable_dir, processes)
        logger.info("TauDEM: %s", " ".join(command))
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise TauDemError(
                f"{exe_name} failed with exit code {proc.returncode}.\n"
                f"Command: {' '.join(command)}\n"
                f"STDERR: {proc.stderr.strip()}\n"
                f"STDOUT: {proc.stdout.strip()}"
            )

        missing_outputs = [str(path) for path in outputs.values() if not Path(path).exists()]
        if missing_outputs:
            raise TauDemError(
                f"{exe_name} completed but expected outputs were missing: "
                + ", ".join(missing_outputs)
            )

        return TauDemCommandResult(
            executable=exe_name,
            command=command,
            outputs={key: Path(value) for key, value in outputs.items()},
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    @staticmethod
    def _build_command(
        exe_path: Path,
        args: list[str],
        executable_dir: Optional[Path] = None,
        processes: int = 1,
    ) -> list[str]:
        mpiexec = TauDem._resolve_mpiexec(Path(executable_dir) if executable_dir else None)
        if mpiexec is not None:
            return [str(mpiexec), "-n", str(max(processes, 1)), str(exe_path), *args]
        logger.warning("TauDEM mpiexec not found; invoking %s directly", exe_path.name)
        return [str(exe_path), *args]

    @staticmethod
    def _resolve_executable(
        exe_name: str,
        executable_dir: Optional[Path] = None,
        required: bool = True,
    ) -> Optional[Path]:
        candidates = [exe_name]
        if not exe_name.lower().endswith(".exe"):
            candidates.append(f"{exe_name}.exe")

        if executable_dir is not None:
            for candidate in candidates:
                path = Path(executable_dir) / candidate
                if path.exists():
                    return path

        for candidate in candidates:
            which = shutil.which(candidate)
            if which:
                return Path(which)

        if required:
            raise TauDemError(f"TauDEM executable not found: {exe_name}")
        return None

    @staticmethod
    def _resolve_mpiexec(executable_dir: Optional[Path] = None) -> Optional[Path]:
        if executable_dir is not None:
            for candidate in ("mpiexec.exe", "mpiexec"):
                path = Path(executable_dir) / candidate
                if path.exists():
                    return path
        which = shutil.which("mpiexec")
        return Path(which) if which else None
