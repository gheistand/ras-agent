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
import geopandas as gpd
from shapely.geometry import LineString, box

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
    basin = box(300000.0, 4400000.0, 315000.0, 4415000.0)
    basin_gdf = gpd.GeoDataFrame({"name": ["watershed"]}, geometry=[basin], crs="EPSG:5070")
    stream = LineString([(307500.0, 4414000.0), (307500.0, 4401000.0)])
    streams_gdf = gpd.GeoDataFrame({"stream_id": [1]}, geometry=[stream], crs="EPSG:5070")
    subbasins_gdf = basin_gdf.copy()
    subbasins_gdf["wsno"] = [1]
    centerlines_gdf = streams_gdf.copy()
    centerlines_gdf["centerline_id"] = [1]
    breaklines_gdf = gpd.GeoDataFrame(
        {"breakline_type": ["stream", "boundary"]},
        geometry=[stream, basin.boundary],
        crs="EPSG:5070",
    )
    return SimpleNamespace(
        characteristics=_make_basin_characteristics(area_mi2),
        basin=basin_gdf,
        streams=streams_gdf,
        subbasins=subbasins_gdf,
        centerlines=centerlines_gdf,
        breaklines=breaklines_gdf,
        dem_clipped=Path(dem_path or "/tmp/fake_dem.tif"),
        artifacts={"fel": Path("/tmp/fel.tif"), "ad8": Path("/tmp/ad8.tif")},
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


def test_build_hdf5_direct_creates_seed_project(tmp_path):
    watershed = _make_watershed()
    hydro_set = _make_hydro_set()
    project = mb.build_model(watershed, hydro_set, tmp_path, mesh_strategy="hdf5_direct")

    assert project.mesh_strategy == "hdf5_direct"
    assert project.prj_file.exists()
    assert project.geometry_file.exists()
    assert project.flow_file.exists()
    assert project.plan_file.exists()
    assert project.plan_hdf.exists()
    assert project.metadata["seed_project_only"] is True
    assert project.metadata["windows_regeneration_required"] is True
    assert project.metadata["centerline_count"] == 1
    assert project.metadata["breakline_count"] >= 2
    assert project.metadata["artifact_keys"] == ["ad8", "fel"]

    geom_text = project.geometry_file.read_text()
    assert "2D Flow Area=" in geom_text
    assert "2D Flow Area Cell Size=" in geom_text
    assert "Mann=" in geom_text


def test_build_ras2025_raises(tmp_path):
    watershed = _make_watershed()
    hydro_set = _make_hydro_set()
    with pytest.raises(NotImplementedError):
        mb.build_model(watershed, hydro_set, tmp_path, mesh_strategy="ras2025")


# ── Tests: RAS Commander wiring ───────────────────────────────────────────────

def test_check_ras_commander_returns_dict():
    result = mb.check_ras_commander()
    assert isinstance(result, dict)
    assert "installed" in result
    assert "version" in result
    assert "capabilities" in result
    assert isinstance(result["installed"], bool)
    assert isinstance(result["capabilities"], list)


def test_clone_project_shutil_fallback(tmp_path, monkeypatch):
    """When ras-commander is not importable, _clone_project falls back to shutil.copytree."""
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    (template_dir / "project.prj").write_text("HEC-RAS Version=6.60\n")
    (template_dir / "project.g01").write_text("Geom Title=Test\n")

    output_dir = tmp_path / "output"
    output_dir.mkdir()

    # Track whether shutil.copytree was called
    copied = []
    real_copytree = shutil.copytree

    def mock_copytree(src, dst, **kwargs):
        copied.append((src, dst))
        return real_copytree(src, dst, **kwargs)

    # Make ras_commander unimportable
    monkeypatch.setitem(sys.modules, "ras_commander", None)
    monkeypatch.setattr(shutil, "copytree", mock_copytree)

    result = mb._clone_project(template_dir, output_dir, "cloned_project")

    assert result == output_dir / "cloned_project"
    assert len(copied) == 1
    assert Path(copied[0][1]) == output_dir / "cloned_project"
    assert (result / "project.prj").exists()


def test_update_mannings_n_hdf5_fallback(tmp_path):
    """_update_mannings_n_hdf5 updates the Mann dataset column 1 in a geometry HDF."""
    import h5py

    geom_hdf = tmp_path / "test_project.g01.hdf"
    original_n = 0.040
    new_n = 0.060
    n_rows = 5

    # Create minimal geometry HDF with Mann dataset
    with h5py.File(geom_hdf, "w") as f:
        mann_data = np.zeros((n_rows, 3), dtype=float)
        mann_data[:, 0] = 0          # region_id
        mann_data[:, 1] = original_n  # n_value
        mann_data[:, 2] = 1.0        # calibration
        f.create_dataset("Geometry/2D Flow Areas/MainArea/Mann", data=mann_data)

    result = mb._update_mannings_n_hdf5(tmp_path, new_n)

    assert result is True
    with h5py.File(geom_hdf, "r") as f:
        mann = f["Geometry/2D Flow Areas/MainArea/Mann"][:]
    assert np.allclose(mann[:, 1], new_n)


# ── Tests: perimeter writing ──────────────────────────────────────────────────

SAMPLE_GEOM_FILE = """\
Geom Title=Test Geometry
Program Version=6.60

2D Flow Area= Perimeter 1  ,0
2D Flow Area Perimeter=  5
     300000.000,4400000.000
     300500.000,4400000.000
     300500.000,4400500.000
     300000.000,4400500.000
     300000.000,4400000.000
2D Flow Area Cell Size=  100.0
Mann= 0.040 ,0 ,0
"""


def _write_sample_geom(tmp_path: Path, content: str = SAMPLE_GEOM_FILE) -> Path:
    geom = tmp_path / "project.g01"
    geom.write_text(content)
    return geom


def test_write_perimeter_creates_backup(tmp_path):
    geom = _write_sample_geom(tmp_path)
    coords = [(300000.0, 4400000.0), (301000.0, 4400000.0), (301000.0, 4401000.0)]
    result = mb._write_perimeter_to_geometry_file(geom, "Perimeter 1", coords)
    assert result is True
    bak = tmp_path / "project.g01.bak"
    assert bak.exists()
    # Backup contains original content
    assert "300500.000,4400000.000" in bak.read_text()


def test_write_perimeter_updates_coordinate_count(tmp_path):
    geom = _write_sample_geom(tmp_path)
    # 3-point open polygon → should be closed to 4 points
    coords = [(300000.0, 4400000.0), (301000.0, 4400000.0), (301000.0, 4401000.0)]
    mb._write_perimeter_to_geometry_file(geom, "Perimeter 1", coords)
    content = geom.read_text()
    # Closed polygon has 4 points
    assert "2D Flow Area Perimeter= 4" in content


def test_write_perimeter_closes_polygon(tmp_path):
    geom = _write_sample_geom(tmp_path)
    coords = [(300000.0, 4400000.0), (301000.0, 4400000.0), (301000.0, 4401000.0)]
    mb._write_perimeter_to_geometry_file(geom, "Perimeter 1", coords)
    content = geom.read_text()
    lines = content.splitlines()
    coord_lines = [l.strip() for l in lines if "," in l and "Flow" not in l and "Mann" not in l and "Title" not in l and "Version" not in l]
    # First and last coordinate lines should be identical (closed polygon)
    assert coord_lines[0] == coord_lines[-1]


def test_write_perimeter_area_not_found(tmp_path):
    geom = _write_sample_geom(tmp_path)
    coords = [(300000.0, 4400000.0), (301000.0, 4400000.0), (301000.0, 4401000.0)]
    result = mb._write_perimeter_to_geometry_file(geom, "NonExistentArea", coords)
    assert result is False
    # No backup created when area not found
    bak = tmp_path / "project.g01.bak"
    assert not bak.exists()


def test_get_2d_area_name_parses_correctly(tmp_path):
    geom = _write_sample_geom(tmp_path)
    name = mb._get_2d_area_name_from_geometry_file(geom)
    assert name == "Perimeter 1"
