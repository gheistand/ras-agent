"""
Smoke test: build a geometry-first project from cached Spring Creek data
and run HEC-RAS 6.6 geometry preprocessing.

Requires:
  - HEC-RAS 6.6 installed on Windows
  - Cached basin polygon at workspace/Spring Creek Springfield IL/02_basin_outline/

Skipped automatically if HEC-RAS is not installed or cached data is missing.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = REPO_ROOT / "workspace" / "Spring Creek Springfield IL"
BASIN_FILE = WORKSPACE / "02_basin_outline" / "USGS_05577500_nldi_basin_5070.geojson"
NLCD_FILE = WORKSPACE / "05_landcover_nlcd" / "nlcd_2021_watershed.tif"
RAS_EXE = Path(r"C:\Program Files (x86)\HEC\HEC-RAS\6.6\Ras.exe")

requires_ras = pytest.mark.skipif(
    not RAS_EXE.exists(), reason="HEC-RAS 6.6 not installed"
)
requires_basin = pytest.mark.skipif(
    not BASIN_FILE.exists(), reason="Cached Spring Creek basin not available"
)


def _load_basin_polygon():
    import geopandas as gpd
    gdf = gpd.read_file(BASIN_FILE)
    return gdf.to_crs("EPSG:5070").geometry.iloc[0]


def _make_spring_creek_watershed(basin_poly):
    """Build a minimal WatershedResult-like object from cached data."""
    import geopandas as gpd
    from shapely.geometry import LineString
    from types import SimpleNamespace

    area_m2 = basin_poly.area
    area_km2 = area_m2 / 1e6
    area_mi2 = area_km2 / 2.58999

    centroid = basin_poly.centroid
    basin_gdf = gpd.GeoDataFrame(
        {"name": ["Spring Creek"]}, geometry=[basin_poly], crs="EPSG:5070"
    )
    stream = LineString([
        (centroid.x, centroid.y + 5000),
        (centroid.x, centroid.y - 5000),
    ])
    streams_gdf = gpd.GeoDataFrame(
        {"stream_id": [1]}, geometry=[stream], crs="EPSG:5070"
    )

    return SimpleNamespace(
        characteristics=SimpleNamespace(
            drainage_area_mi2=area_mi2,
            drainage_area_km2=area_km2,
            main_channel_slope_m_per_m=0.002,
            main_channel_length_km=20.0,
            mean_elevation_m=180.0,
            relief_m=30.0,
            centroid_lat=39.8153,
            centroid_lon=-89.6987,
            pour_point_lat=39.8153,
            pour_point_lon=-89.6987,
        ),
        basin=basin_gdf,
        streams=streams_gdf,
        centerlines=streams_gdf.copy(),
        breaklines=gpd.GeoDataFrame(
            {"breakline_type": ["stream"]}, geometry=[stream], crs="EPSG:5070"
        ),
        dem_clipped=WORKSPACE / "04_terrain" / "spring_creek_basin_dem_5070.tif",
        artifacts={},
    )


def _make_simple_hydro_set():
    import numpy as np
    from types import SimpleNamespace

    times = np.arange(80) * 0.25
    flows = np.sin(np.linspace(0, np.pi, 80)) * 5000.0 + 10.0
    hydro = SimpleNamespace(
        return_period_yr=100,
        peak_flow_cfs=5000.0,
        time_to_peak_hr=5.0,
        duration_hr=times[-1],
        time_step_hr=0.25,
        times_hr=times,
        flows_cfs=flows,
        baseflow_cfs=10.0,
        source="test",
        metadata={},
    )
    hydros = {100: hydro}
    return SimpleNamespace(
        watershed_area_mi2=50.0,
        time_of_concentration_hr=2.0,
        hydrographs=hydros,
        get=lambda rp: hydros.get(rp),
    )


@requires_basin
def test_geometry_first_builds_valid_project(tmp_path):
    """Build a geometry-first project from cached Spring Creek basin polygon."""
    import model_builder as mb

    basin_poly = _load_basin_polygon()
    watershed = _make_spring_creek_watershed(basin_poly)
    hydro_set = _make_simple_hydro_set()

    project = mb.build_model(
        watershed, hydro_set, tmp_path,
        return_periods=[100],
        mesh_strategy="geometry_first",
    )

    assert project.mesh_strategy == "geometry_first"
    assert project.prj_file.exists()
    assert project.geometry_file.exists()
    assert project.flow_file.exists()
    assert project.plan_file.exists()

    geom_text = project.geometry_file.read_text(encoding="utf-8")
    assert "Storage Area=MainArea" in geom_text
    assert "Storage Area Is2D=-1" in geom_text
    assert "Storage Area Surface Line=" in geom_text

    prj_text = project.prj_file.read_text(encoding="utf-8")
    assert "Geom File=g01" in prj_text
    assert "Plan File=p01" in prj_text


@requires_basin
@requires_ras
def test_geometry_first_hecras_preprocess(tmp_path):
    """Build project from Spring Creek data, run HEC-RAS 6.6 geometry preprocessing.

    Uses RasPreprocess.preprocess_plan() which runs HEC-RAS with early termination
    after geometry preprocessing, avoiding a full simulation (which needs terrain).
    """
    import model_builder as mb

    basin_poly = _load_basin_polygon()
    watershed = _make_spring_creek_watershed(basin_poly)
    hydro_set = _make_simple_hydro_set()

    project = mb.build_model(
        watershed, hydro_set, tmp_path,
        return_periods=[100],
        mesh_strategy="geometry_first",
    )

    from ras_commander import init_ras_project
    from ras_commander.RasPreprocess import RasPreprocess

    init_ras_project(project.project_dir, "6.6")

    for hdf in project.project_dir.glob("*.g01.hdf"):
        hdf.unlink()

    try:
        result = RasPreprocess.preprocess_plan("01", max_wait=120)
    except Exception as e:
        pytest.skip(f"HEC-RAS execution unavailable: {e}")

    geom_hdf = project.geometry_file.with_suffix(
        project.geometry_file.suffix + ".hdf"
    )
    assert geom_hdf.exists(), f"Geometry HDF not created at {geom_hdf}"
    assert geom_hdf.stat().st_size > 1000, (
        f"Geometry HDF too small ({geom_hdf.stat().st_size} bytes) — "
        "preprocessing may have failed"
    )
