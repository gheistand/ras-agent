"""
End-to-end integration test: Spring Creek Springfield IL

Assembles a WatershedResult from existing TauDEM artifacts and workspace data,
runs the geometry_first pipeline to produce a HEC-RAS project with 2D BC Lines,
and optionally generates a mesh (if .g01.hdf is present from prior HEC-RAS run).

Run:  pytest tests/test_spring_creek_e2e.py -v -s
"""

import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform

logger = logging.getLogger(__name__)

# ── Workspace paths ──────────────────────────────────────────────────────────

WS = Path("G:/GH/ras-agent/workspace/Spring Creek Springfield IL")
BASIN_JSON = WS / "02_basin_outline/USGS_05577500_nldi_basin.json"
NET_SHP = WS / "04_terrain/taudem_basin2/net.shp"
OUTLET_SHP = WS / "04_terrain/taudem_basin2/outlet_snapped.shp"
AD8_TIF = WS / "04_terrain/taudem_basin2/ad8.tif"
DEM_TIF = WS / "04_terrain/spring_creek_basin_dem_5070.tif"
NLCD_TIF = WS / "05_landcover_nlcd/nlcd_2021_watershed.tif"

SKIP_REASON = "Spring Creek workspace not present"


def _workspace_available():
    return all(p.exists() for p in [BASIN_JSON, NET_SHP, OUTLET_SHP, DEM_TIF])


# ── Synthetic WatershedResult from existing artifacts ────────────────────────


def _load_basin_5070():
    with open(BASIN_JSON) as f:
        gj = json.load(f)
    geom_4326 = shape(gj["features"][0]["geometry"])
    t = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    return transform(t.transform, geom_4326)


def _load_streams_gdf():
    net = gpd.read_file(NET_SHP)
    return net.set_crs("EPSG:5070", allow_override=True)


def _load_pour_point():
    gdf = gpd.read_file(OUTLET_SHP).set_crs("EPSG:5070", allow_override=True)
    return gdf.geometry.iloc[0]


def _build_watershed_result():
    """Assemble a WatershedResult from on-disk TauDEM outputs."""
    from dataclasses import dataclass as _dc, field as _field

    @_dc
    class BasinCharacteristics:
        drainage_area_km2: float
        drainage_area_mi2: float
        mean_elevation_m: float
        relief_m: float
        main_channel_length_km: float
        main_channel_slope_m_per_m: float
        centroid_lat: float
        centroid_lon: float
        pour_point_lat: float
        pour_point_lon: float
        extra: dict = _field(default_factory=dict)

    @_dc
    class WatershedResult:
        basin: object
        streams: object
        subbasins: object
        centerlines: object
        breaklines: object
        pour_point: object
        characteristics: object
        dem_clipped: object
        artifacts: dict = _field(default_factory=dict)

    basin_5070 = _load_basin_5070()
    area_km2 = basin_5070.area / 1e6
    pp = _load_pour_point()
    streams_gdf = _load_streams_gdf()

    t_4326 = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)
    cx, cy = basin_5070.centroid.x, basin_5070.centroid.y
    clon, clat = t_4326.transform(cx, cy)
    plon, plat = t_4326.transform(pp.x, pp.y)

    chars = BasinCharacteristics(
        drainage_area_km2=area_km2,
        drainage_area_mi2=area_km2 * 0.386102,
        mean_elevation_m=200.0,
        relief_m=50.0,
        main_channel_length_km=25.0,
        main_channel_slope_m_per_m=0.002,
        centroid_lat=clat,
        centroid_lon=clon,
        pour_point_lat=plat,
        pour_point_lon=plon,
    )

    basin_gdf = gpd.GeoDataFrame(geometry=[basin_5070], crs="EPSG:5070")

    artifacts = {}
    if AD8_TIF.exists():
        artifacts["ad8"] = str(AD8_TIF)

    return WatershedResult(
        basin=basin_gdf,
        streams=streams_gdf,
        subbasins=gpd.GeoDataFrame(),
        centerlines=gpd.GeoDataFrame(),
        breaklines=gpd.GeoDataFrame(),
        pour_point=pp,
        characteristics=chars,
        dem_clipped=DEM_TIF,
        artifacts=artifacts,
    )


def _build_hydro_set(area_mi2):
    """Create a synthetic 100-yr hydrograph for testing."""
    from pipeline.hydrograph import HydrographResult, HydrographSet

    n_points = 97
    peak_cfs = 8000.0
    dt_hr = 0.25
    times = np.arange(n_points) * dt_hr
    flows = peak_cfs * np.sin(np.pi * np.arange(n_points) / (n_points - 1))
    flows[0] = 0.0
    flows[-1] = 0.0

    hydro = HydrographResult(
        return_period_yr=100,
        peak_flow_cfs=peak_cfs,
        time_to_peak_hr=times[np.argmax(flows)],
        duration_hr=times[-1],
        time_step_hr=dt_hr,
        times_hr=times,
        flows_cfs=flows,
        baseflow_cfs=0.0,
        source="synthetic_test",
    )

    return HydrographSet(
        watershed_area_mi2=area_mi2,
        time_of_concentration_hr=6.0,
        hydrographs={100: hydro},
    )


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _workspace_available(), reason=SKIP_REASON)
class TestSpringCreekBcLines:
    """Verify BC Line generation from real TauDEM data."""

    def test_stream_boundary_crossings(self):
        from pipeline.bc_lines import _find_stream_boundary_intersections
        basin = _load_basin_5070()
        streams = list(_load_streams_gdf().geometry)
        isects = _find_stream_boundary_intersections(streams, basin)
        assert len(isects) >= 3, f"Expected 3+ crossings, got {len(isects)}"

    def test_generate_bc_lines_headwater(self):
        from pipeline.bc_lines import generate_bc_lines
        basin = _load_basin_5070()
        streams = list(_load_streams_gdf().geometry)
        pp = _load_pour_point()
        bc_set = generate_bc_lines(
            basin=basin, streams=streams, pour_point=pp,
            dem_path=DEM_TIF, ad8_path=AD8_TIF,
            channel_slope=0.002, headwater=True,
        )
        assert bc_set.outlet is not None
        assert len(bc_set.inflows) == 0, "Headwater should have no inflows"
        assert len(bc_set.bc_lines) == 2, "Headwater needs exactly 2 BCs"
        assert bc_set.bc_lines[0].name == "DSOutflow"
        assert bc_set.bc_lines[1].name == "Perimeter"
        for bc in bc_set.bc_lines:
            assert bc.bc_type == "normal_depth"

    def test_generate_bc_lines_non_headwater(self):
        from pipeline.bc_lines import generate_bc_lines
        basin = _load_basin_5070()
        streams = list(_load_streams_gdf().geometry)
        pp = _load_pour_point()
        bc_set = generate_bc_lines(
            basin=basin, streams=streams, pour_point=pp,
            dem_path=DEM_TIF, ad8_path=AD8_TIF,
            channel_slope=0.002, headwater=False,
        )
        assert bc_set.outlet is not None
        assert len(bc_set.inflows) >= 2
        weights = [bc.weight for bc in bc_set.inflows]
        assert sum(weights) == pytest.approx(1.0, abs=0.01)

    def test_bc_coords_near_boundary(self):
        """BC coords should be near (within offset distance of) the boundary."""
        from pipeline.bc_lines import generate_bc_lines, DEFAULT_OFFSET_FT
        basin = _load_basin_5070()
        streams = list(_load_streams_gdf().geometry)
        pp = _load_pour_point()
        bc_set = generate_bc_lines(
            basin=basin, streams=streams, pour_point=pp, channel_slope=0.002,
        )
        max_dist = DEFAULT_OFFSET_FT * 0.3048 * 2
        for bc in bc_set.bc_lines:
            for x, y in bc.coords:
                d = basin.boundary.distance(Point(x, y))
                assert d < max_dist, (
                    f"BC '{bc.name}' coord ({x:.0f},{y:.0f}) is {d:.0f}m from boundary"
                )


@pytest.mark.skipif(not _workspace_available(), reason=SKIP_REASON)
class TestSpringCreekModelBuild:
    """Full model build from TauDEM artifacts."""

    def test_build_geometry_first_project(self, tmp_path):
        from pipeline.model_builder import build_model

        watershed = _build_watershed_result()
        hydro_set = _build_hydro_set(watershed.characteristics.drainage_area_mi2)
        nlcd = NLCD_TIF if NLCD_TIF.exists() else None

        project = build_model(
            watershed=watershed,
            hydro_set=hydro_set,
            output_dir=tmp_path,
            return_periods=[100],
            nlcd_raster_path=nlcd,
            strategy="geometry_first",
        )

        assert project.project_dir.exists()
        assert project.geometry_file.exists()
        assert project.flow_file.exists()
        assert project.plan_file.exists()

        g01_text = project.geometry_file.read_text()
        assert "BC Line Name=" in g01_text
        bc_count = g01_text.count("BC Line Name=")
        assert bc_count == 2, f"Expected 2 BC Lines (headwater), got {bc_count}"
        assert "Storage Area Is2D=-1" in g01_text

        u01_text = project.flow_file.read_text()
        assert "Boundary Location=" in u01_text
        assert "MainArea" in u01_text

    def test_geom_file_has_correct_perimeter(self, tmp_path):
        from pipeline.model_builder import build_model

        watershed = _build_watershed_result()
        hydro_set = _build_hydro_set(watershed.characteristics.drainage_area_mi2)

        project = build_model(
            watershed=watershed,
            hydro_set=hydro_set,
            output_dir=tmp_path,
            return_periods=[100],
            strategy="geometry_first",
        )

        g01_text = project.geometry_file.read_text()
        assert "Storage Area=MainArea" in g01_text
        coord_count_line = [
            l for l in g01_text.splitlines()
            if l.startswith("Storage Area Surface Line=")
        ]
        assert len(coord_count_line) == 1
        n_pts = int(coord_count_line[0].split("=")[1].strip())
        assert n_pts >= 100, f"Expected 100+ perimeter points, got {n_pts}"

    def test_terrain_registered(self, tmp_path):
        from pipeline.model_builder import build_model

        watershed = _build_watershed_result()
        hydro_set = _build_hydro_set(watershed.characteristics.drainage_area_mi2)

        project = build_model(
            watershed=watershed,
            hydro_set=hydro_set,
            output_dir=tmp_path,
            return_periods=[100],
            strategy="geometry_first",
        )

        terrain_in_project = project.project_dir / "terrain.tif"
        assert terrain_in_project.exists()
        rasmap = project.project_dir / f"{project.project_dir.name}.rasmap"
        rasmap_text = rasmap.read_text()
        assert "terrain.tif" in rasmap_text

    def test_metadata_populated(self, tmp_path):
        from pipeline.model_builder import build_model

        watershed = _build_watershed_result()
        hydro_set = _build_hydro_set(watershed.characteristics.drainage_area_mi2)

        project = build_model(
            watershed=watershed,
            hydro_set=hydro_set,
            output_dir=tmp_path,
            return_periods=[100],
            strategy="geometry_first",
        )

        assert project.metadata["watershed_area_mi2"] == pytest.approx(103.4, abs=1)
        assert project.metadata["cell_size_m"] > 0
        assert project.metadata["main_channel_slope"] == pytest.approx(0.002, abs=0.001)


@pytest.mark.skipif(not _workspace_available(), reason=SKIP_REASON)
class TestSpringCreekFlowFile:
    """Verify .u01 2D boundary locations and flow weighting."""

    def test_boundary_location_entries(self, tmp_path):
        from pipeline.model_builder import build_model

        watershed = _build_watershed_result()
        hydro_set = _build_hydro_set(watershed.characteristics.drainage_area_mi2)

        project = build_model(
            watershed=watershed,
            hydro_set=hydro_set,
            output_dir=tmp_path,
            return_periods=[100],
            strategy="geometry_first",
        )

        u01_text = project.flow_file.read_text()
        bl_count = u01_text.count("Boundary Location=")
        assert bl_count == 2, f"Expected 2 Boundary Locations (headwater), got {bl_count}"
        assert "Friction Slope=" in u01_text
        assert "DSOutflow" in u01_text
        # Headwater mode: all BCs are Normal Depth, no Flow Hydrograph
        assert "Flow Hydrograph=" not in u01_text
        assert "USInflow" not in u01_text
        # No 1D references
        assert "RAS_AGENT,MAIN" not in u01_text
