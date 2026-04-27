"""
test_taudem.py — Tests for pipeline/taudem.py

All tests pass without TauDEM installed by mocking subprocess execution and
binary discovery.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import taudem as td


def test_detect_installation_reports_available_tools(monkeypatch):
    def fake_which(name):
        if name == "mpiexec":
            return r"C:\Program Files\MPI\mpiexec.exe"
        if name.endswith(".exe"):
            return rf"C:\TauDEM\{name}"
        return None

    monkeypatch.setattr(td.shutil, "which", fake_which)

    info = td.TauDem.detect_installation()

    assert info["installed"] is True
    assert info["missing"] == []
    assert info["executable_dir"] == Path(r"C:\TauDEM")
    assert info["executables"]["PitRemove"] == Path(r"C:\TauDEM\PitRemove.exe")
    assert info["mpiexec"] == Path(r"C:\Program Files\MPI\mpiexec.exe")


def test_validate_environment_raises_when_required_missing(monkeypatch):
    monkeypatch.setattr(td.TauDem, "detect_installation", lambda executable_dir=None: {
        "installed": False,
        "executable_dir": None,
        "executables": {"PitRemove": Path(r"C:\TauDEM\PitRemove.exe")},
        "missing": ["D8FlowDir"],
        "mpiexec": None,
    })

    with pytest.raises(td.TauDemError, match="D8FlowDir"):
        td.TauDem.validate_environment(required=["PitRemove", "D8FlowDir"])


def test_build_command_prefers_mpiexec(monkeypatch):
    monkeypatch.setattr(td.TauDem, "_resolve_mpiexec", lambda executable_dir=None: Path(r"C:\MPI\mpiexec.exe"))

    command = td.TauDem._build_command(
        exe_path=Path(r"C:\TauDEM\Threshold.exe"),
        args=["-ssa", "ad8.tif", "-src", "src.tif", "-thresh", "100"],
        processes=4,
    )

    assert command == [
        r"C:\MPI\mpiexec.exe",
        "-n",
        "4",
        r"C:\TauDEM\Threshold.exe",
        "-ssa",
        "ad8.tif",
        "-src",
        "src.tif",
        "-thresh",
        "100",
    ]


def test_threshold_runs_and_returns_structured_result(tmp_path, monkeypatch):
    srcfile = tmp_path / "src.tif"
    captured = {}

    monkeypatch.setattr(td.TauDem, "_resolve_executable", lambda exe_name, executable_dir=None, required=True: Path(r"C:\TauDEM\Threshold.exe"))
    monkeypatch.setattr(td.TauDem, "_resolve_mpiexec", lambda executable_dir=None: None)

    def fake_run(command, capture_output, text, check):
        captured["command"] = command
        srcfile.write_text("fake raster")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(td.subprocess, "run", fake_run)

    result = td.TauDem.threshold(
        ssafile=tmp_path / "ad8.tif",
        srcfile=srcfile,
        thresh=125,
        processes=1,
    )

    assert captured["command"] == [
        r"C:\TauDEM\Threshold.exe",
        "-ssa",
        str(tmp_path / "ad8.tif"),
        "-src",
        str(srcfile),
        "-thresh",
        "125",
    ]
    assert result.executable == "Threshold"
    assert result.outputs["src"] == srcfile
    assert result.stdout == "ok"


def test_run_raises_when_expected_output_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(td.TauDem, "_resolve_executable", lambda exe_name, executable_dir=None, required=True: Path(r"C:\TauDEM\AreaD8.exe"))
    monkeypatch.setattr(td.TauDem, "_resolve_mpiexec", lambda executable_dir=None: None)
    monkeypatch.setattr(
        td.subprocess,
        "run",
        lambda command, capture_output, text, check: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )

    with pytest.raises(td.TauDemError, match="expected outputs were missing"):
        td.TauDem.area_d8(
            pfile=tmp_path / "p.tif",
            ad8file=tmp_path / "ad8.tif",
        )
