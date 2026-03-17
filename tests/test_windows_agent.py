"""
Tests for windows_agent.py — Windows mesh generation interface

All tests run without a Windows machine or HEC-RAS installation.
- Mock mode: fully functional, no external deps.
- Local mode: returns MeshResult(success=False) on non-Windows platforms.
- Remote mode: still raises NotImplementedError (stub).
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import pytest
from windows_agent import (
    MeshRequest,
    MeshResult,
    WindowsAgent,
    WindowsAgentConfig,
    generate_mesh,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_request(tmp_path) -> MeshRequest:
    return MeshRequest(
        watershed_id="test_ws",
        perimeter_coords=[(0, 0), (1000, 0), (1000, 1000), (0, 1000), (0, 0)],
        terrain_path="/fake/terrain.tif",
        template_project_path="",
        cell_size_m=50.0,
        output_dir=str(tmp_path),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_mock_mesh_generation(tmp_path):
    """Mock mode returns success=True and strategy='mock'."""
    config = WindowsAgentConfig(mode="mock")
    agent = WindowsAgent(config)
    result = agent.generate_mesh(_mock_request(tmp_path))

    assert isinstance(result, MeshResult)
    assert result.success is True
    assert result.strategy == "mock"
    assert result.cell_count == 1000
    assert result.error == ""
    assert result.duration_sec >= 0.0


def test_mock_health_check():
    """health_check() returns status='ok' in mock mode."""
    config = WindowsAgentConfig(mode="mock")
    agent = WindowsAgent(config)
    status = agent.health_check()

    assert status["status"] == "ok"
    assert status["mode"] == "mock"
    assert "hecras_version" in status
    assert "strategy" in status


def test_local_fails_on_non_windows(tmp_path):
    """Local mode on non-Windows returns success=False (not raises)."""
    config = WindowsAgentConfig(mode="local")
    agent = WindowsAgent(config)
    # On Linux/macOS this should catch the platform error and return failure
    with patch("windows_agent.platform.system", return_value="Linux"):
        result = agent.generate_mesh(_mock_request(tmp_path))

    assert isinstance(result, MeshResult)
    assert result.success is False
    assert result.strategy == "ras_preprocess"
    assert "Windows" in result.error


def test_remote_not_implemented(tmp_path):
    """Remote mode raises NotImplementedError (stub)."""
    config = WindowsAgentConfig(mode="remote")
    agent = WindowsAgent(config)
    with pytest.raises(NotImplementedError):
        agent.generate_mesh(_mock_request(tmp_path))


def test_convenience_function(tmp_path):
    """Module-level generate_mesh() with no config defaults to mock mode."""
    request = _mock_request(tmp_path)
    result = generate_mesh(request)  # config=None → mock

    assert result.success is True
    assert result.strategy == "mock"


def test_local_windows_check(tmp_path):
    """On non-Windows, local mode returns MeshResult(success=False) with platform error."""
    config = WindowsAgentConfig(mode="local")
    agent = WindowsAgent(config)
    with patch("windows_agent.platform.system", return_value="Darwin"):
        result = agent.generate_mesh(_mock_request(tmp_path))

    assert result.success is False
    assert "Windows" in result.error
    assert result.strategy == "ras_preprocess"


def test_local_health_check_non_windows():
    """health_check() in local mode on non-Windows returns status='unavailable'."""
    config = WindowsAgentConfig(mode="local")
    agent = WindowsAgent(config)
    with patch("windows_agent.platform.system", return_value="Linux"):
        status = agent.health_check()

    assert status["status"] == "unavailable"
    assert status["mode"] == "local"
    assert "reason" in status
    assert "Windows" in status["reason"]


def test_ras_preprocess_import_error(tmp_path):
    """Missing ras_commander import on Windows returns graceful failure."""
    config = WindowsAgentConfig(mode="local")
    agent = WindowsAgent(config)
    with patch("windows_agent.platform.system", return_value="Windows"), \
         patch("builtins.__import__", side_effect=_make_import_error("ras_commander")):
        result = agent.generate_mesh(_mock_request(tmp_path))

    assert result.success is False
    assert result.strategy == "ras_preprocess"
    assert result.error  # non-empty error message


def _make_import_error(blocked_module: str):
    """Return a side_effect for builtins.__import__ that raises ImportError for one module."""
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def _import(name, *args, **kwargs):
        if name == blocked_module or name.startswith(blocked_module + "."):
            raise ImportError(f"No module named '{blocked_module}'")
        return real_import(name, *args, **kwargs)

    return _import
