"""
Tests for windows_agent.py — Windows mesh generation interface

All tests run without a Windows machine or HEC-RAS installation.
Local and remote modes are expected to raise NotImplementedError (stubs).
"""

import os
import sys

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


def test_local_not_implemented(tmp_path):
    """Local mode raises NotImplementedError (stub)."""
    config = WindowsAgentConfig(mode="local")
    agent = WindowsAgent(config)
    with pytest.raises(NotImplementedError):
        agent.generate_mesh(_mock_request(tmp_path))


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
