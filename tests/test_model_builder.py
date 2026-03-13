"""
test_model_builder.py — Tests for pipeline/model_builder.py

All tests pass without RAS Commander or HEC-RAS installed.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import model_builder as mb


# ── Minimal mock objects ──────────────────────────────────────────────────────

def _make_basin_characteristics(area_mi2=150.0, slope=0.003):
    return SimpleNamespace(
        drainage_area_mi2=area_mi2,
        drainage_area_km2=area_mi2 * 2.58999,
        main_channel_slope_m_per_m=slope,
        main_channel_length_km=30.0,
        mean_elevation_m=200.0,
        relief_m=50.0,
        centroid_lat=40.5,
        centroid_lon=-89.5,
        pour_point_lat=40.4,
        pour_point_lon=-89.6,
    )


def _make_watershed(area_mi2=150.0, dem_path=None):
    return SimpleNamespace(
        characteristics=_make_basin_characteristics(area_mi2),
        basin=SimpleNamespace(geometry=SimpleNamespace(iloc=lambda i: None)),
        streams=None,
        dem_clipped=Path(dem_path or "/tmp/fake_dem.tif"),
    )


def _make_hydrograph(return_period=100, peak_flow=5000.0, n_points=80):
    times = np.arange(n_points) * 0.25
    flows = np.sin(np.linspace(0, np.pi, n_points)) * peak_flow + 10.0
    flows = np.maximum(flows, 0.0)
    return SimpleNamespace(
        return_period_yr=return_period,
        peak_flow_cfs=peak_flow,
        time_to_peak_hr=5.0,
        duration_hr=times[-1],
        time_step_hr=0.25,
        times_hr=times,
        flows_cfs=flows,
        baseflow_cfs=10.0,
        source="NRCS_DUH",
        metadata={},
    )


def _make_hydro_set(return_periods=(10, 100)):
    hydros = {rp: _make_hydrograph(return_period=rp) for rp in return_periods}
    return SimpleNamespace(
        watershed_area_mi2=150.0,
        time_of_concentration_hr=2.0,
        hydrographs=hydros,
        get=lambda rp: hydros.get(rp),
    )


def _make_template_dir(tmp_path: Path, name="test_template") -> Path:
    """Create a minimal fake template HEC-RAS project directory."""
    tpl = tmp_path / name
    tpl.mkdir()
    (tpl / "template_project.prj").write_text("HEC-RAS Version=6.60\n")
    (tpl / "template_project.g01").write_text("Geom Title=Test Geometry\n")
    return tpl


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_registry():
    """Reset TEMPLATE_REGISTRY before and after each test."""
    mb.TEMPLATE_REGISTRY.clear()
    yield
    mb.TEMPLATE_REGISTRY.clear()


# ── Tests: template registry ──────────────────────────────────────────────────

def test_register_template(tmp_path):
    tpl = _make_template_dir(tmp_path)
    mb.register_template("small", tpl, target_area_mi2=50.0, description="test")
    assert "small" in mb.TEMPLATE_REGISTRY
    assert mb.TEMPLATE_REGISTRY["small"].target_area_mi2 == 50.0
    assert mb.TEMPLATE_REGISTRY["small"].name == "small"


def test_select_template_closest(tmp_path):
    small = _make_template_dir(tmp_path, "small")
    medium = _make_template_dir(tmp_path, "medium")
    large = _make_template_dir(tmp_path, "large")
    mb.register_template("small", small, 50.0)
    mb.register_template("medium", medium, 200.0)
    mb.register_template("large", large, 800.0)

    # 60 mi² should map to "small" (closest on log scale to 50)
    result = mb.select_template(60.0)
    assert result is not None
    assert result.name == "small"

    # 250 mi² should map to "medium"
    result = mb.select_template(250.0)
    assert result.name == "medium"

    # 700 mi² should map to "large"
    result = mb.select_template(700.0)
    assert result.name == "large"


def test_select_template_empty_registry():
    result = mb.select_template(150.0)
    assert result is None


# ── Tests: Manning's n ────────────────────────────────────────────────────────

def test_get_mannings_n_known_class():
    # NLCD 82 = Cultivated Crops → 0.037
    assert mb.get_mannings_n(82) == pytest.approx(0.037)


def test_get_mannings_n_unknown_class():
    # Unknown class should return DEFAULT_MANNINGS_N
    assert mb.get_mannings_n(999) == pytest.approx(mb.DEFAULT_MANNINGS_N)


# ── Tests: file writers ───────────────────────────────────────────────────────

def test_write_unsteady_flow_file(tmp_path):
    hydro_set = _make_hydro_set(return_periods=(100,))
    flow_file = tmp_path / "test.u01"
    mb._write_unsteady_flow_file(flow_file, hydro_set, return_period=100, bc_slope=0.003)

    assert flow_file.exists()
    content = flow_file.read_text()

    # Check required headers
    assert "Flow Title=" in content
    assert "Program Version=6.60" in content
    assert "Boundary Location=" in content
    assert "Flow Hydrograph=" in content
    assert "Normal Depth=0.003000" in content

    # Check flow values block exists (non-empty lines after Flow Hydrograph=)
    lines = content.splitlines()
    hdr_idx = next(i for i, l in enumerate(lines) if "Flow Hydrograph=" in l)
    n_points = int(lines[hdr_idx].split("=")[1].strip())
    assert n_points == 80  # matches _make_hydrograph default


def test_write_plan_file(tmp_path):
    plan_file = tmp_path / "test.p01"
    mb._write_plan_file(
        plan_file,
        geom_file="g01",
        flow_file="u01",
        simulation_duration_hr=20.0,
        warm_up_hr=12.0,
    )

    assert plan_file.exists()
    content = plan_file.read_text()

    assert "Geom File=g01" in content
    assert "Flow File=u01" in content
    assert "Simulation Date=" in content
    assert "Program Version=6.60" in content
    assert "Computation Interval=30SEC" in content
    assert "Output Interval=1HOUR" in content

    # Simulation Date format: startDDMMMYYYY,hhmm,endDDMMMYYYY,hhmm
    sim_date_line = next(l for l in content.splitlines() if l.startswith("Simulation Date="))
    parts = sim_date_line.split("=")[1].split(",")
    assert len(parts) == 4


# ── Tests: build_model dispatching ───────────────────────────────────────────

def test_build_from_template_no_templates(tmp_path):
    watershed = _make_watershed()
    hydro_set = _make_hydro_set()
    with pytest.raises(RuntimeError, match="No templates registered"):
        mb.build_model(watershed, hydro_set, tmp_path, mesh_strategy="template_clone")


def test_build_hdf5_direct_raises(tmp_path):
    watershed = _make_watershed()
    hydro_set = _make_hydro_set()
    with pytest.raises(NotImplementedError):
        mb.build_model(watershed, hydro_set, tmp_path, mesh_strategy="hdf5_direct")


def test_build_ras2025_raises(tmp_path):
    watershed = _make_watershed()
    hydro_set = _make_hydro_set()
    with pytest.raises(NotImplementedError):
        mb.build_model(watershed, hydro_set, tmp_path, mesh_strategy="ras2025")
