"""
Tests for pipeline/watershed.py basin characterization helpers.

All fixtures are synthetic and avoid TauDEM, real DEMs, and network access.
"""

import os
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, box

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

from watershed import (
    BasinCharacteristics,
    WatershedResult,
    _build_breaklines,
    _build_centerlines,
    _compute_basin_characteristics,
    _polygonize_watershed_grid,
    save_watershed,
)


CRS_5070 = CRS.from_epsg(5070)


def _memory_raster(data, *, transform=None, crs=CRS_5070, nodata=None):
    """Return an open GDAL /vsimem GeoTIFF for helpers that call rasterio.open."""
    data = np.asarray(data)
    memfile = MemoryFile()
    with memfile.open(
        driver="GTiff",
        width=data.shape[1],
        height=data.shape[0],
        count=1,
        dtype=data.dtype,
        crs=crs,
        transform=transform or from_origin(0.0, float(data.shape[0]), 1.0, 1.0),
        nodata=nodata,
    ) as ds:
        ds.write(data, 1)
    return memfile


def test_polygonize_watershed_grid_dissolves_positive_cells():
    data = np.array(
        [
            [1, 1, 0],
            [1, 2, 2],
            [0, 2, 0],
        ],
        dtype=np.int16,
    )
    transform = from_origin(0.0, 30.0, 10.0, 10.0)

    with _memory_raster(data, transform=transform) as memfile:
        gdf = _polygonize_watershed_grid(memfile.name, CRS_5070)

    assert list(gdf["wsno"]) == [1, 2]
    assert list(gdf.geometry.area) == pytest.approx([300.0, 300.0])
    assert gdf.crs == CRS_5070


def test_polygonize_watershed_grid_returns_empty_for_empty_watershed():
    data = np.zeros((2, 2), dtype=np.int16)

    with _memory_raster(data) as memfile:
        gdf = _polygonize_watershed_grid(memfile.name, CRS_5070)

    assert gdf.empty
    assert list(gdf.columns) == ["wsno", "geometry"]
    assert gdf.crs == CRS_5070


def test_polygonize_watershed_grid_handles_single_cell_basin():
    data = np.array([[5]], dtype=np.int16)
    transform = from_origin(100.0, 200.0, 30.0, 30.0)

    with _memory_raster(data, transform=transform) as memfile:
        gdf = _polygonize_watershed_grid(memfile.name, CRS_5070)

    assert len(gdf) == 1
    assert gdf.loc[0, "wsno"] == 5
    assert gdf.geometry.iloc[0].area == pytest.approx(900.0)
    assert gdf.geometry.iloc[0].bounds == pytest.approx((100.0, 170.0, 130.0, 200.0))


def test_compute_basin_characteristics_uses_dem_and_longest_stream():
    basin_shape = box(500_000.0, 1_800_000.0, 502_000.0, 1_801_000.0)
    dem = np.array(
        [
            [100.0, 102.0, 104.0],
            [106.0, -9999.0, 110.0],
        ],
        dtype=np.float32,
    )
    streams = gpd.GeoDataFrame(
        {"stream_id": [1, 2], "Slope": [0.002, 0.004]},
        geometry=[
            LineString([(500_100.0, 1_800_000.0), (500_100.0, 1_801_000.0)]),
            LineString([(500_500.0, 1_800_000.0), (502_500.0, 1_800_000.0)]),
        ],
        crs=CRS_5070,
    )

    with _memory_raster(dem, nodata=-9999.0) as memfile:
        chars = _compute_basin_characteristics(
            basin_shape=basin_shape,
            basin_crs=CRS_5070,
            clipped_dem_path=memfile.name,
            streams_gdf=streams,
            pour_point=Point(501_000.0, 1_800_100.0),
            pour_point_lon=-89.5,
            pour_point_lat=40.0,
            cell_area_km2=0.0009,
            threshold_cells=20,
            source_bounds=(499_000.0, 1_799_000.0, 503_000.0, 1_802_000.0),
        )

    centroid_lon, centroid_lat = Transformer.from_crs(
        CRS_5070, "EPSG:4326", always_xy=True
    ).transform(basin_shape.centroid.x, basin_shape.centroid.y)

    assert chars.drainage_area_km2 == pytest.approx(2.0)
    assert chars.drainage_area_mi2 == pytest.approx(0.772204)
    assert chars.mean_elevation_m == pytest.approx(104.4)
    assert chars.relief_m == pytest.approx(10.0)
    assert chars.main_channel_length_km == pytest.approx(2.0)
    assert chars.main_channel_slope_m_per_m == pytest.approx(0.004)
    assert chars.centroid_lon == pytest.approx(centroid_lon)
    assert chars.centroid_lat == pytest.approx(centroid_lat)
    assert chars.pour_point_lon == -89.5
    assert chars.pour_point_lat == 40.0
    assert chars.extra["threshold_cells"] == 20
    assert chars.extra["cell_area_km2"] == pytest.approx(0.0009)
    assert chars.extra["source_bounds"] == (
        499_000.0,
        1_799_000.0,
        503_000.0,
        1_802_000.0,
    )
    assert chars.extra["snapped_pour_point_x"] == pytest.approx(501_000.0)
    assert chars.extra["snapped_pour_point_y"] == pytest.approx(1_800_100.0)


def test_compute_basin_characteristics_handles_empty_dem_and_no_streams():
    basin_shape = box(500_000.0, 1_800_000.0, 500_010.0, 1_800_010.0)
    empty_streams = gpd.GeoDataFrame({"stream_id": []}, geometry=[], crs=CRS_5070)
    dem = np.array([[-9999.0]], dtype=np.float32)

    with _memory_raster(dem, nodata=-9999.0) as memfile:
        chars = _compute_basin_characteristics(
            basin_shape=basin_shape,
            basin_crs=CRS_5070,
            clipped_dem_path=memfile.name,
            streams_gdf=empty_streams,
            pour_point=Point(500_005.0, 1_800_005.0),
            pour_point_lon=-89.0,
            pour_point_lat=40.0,
            cell_area_km2=0.0001,
            threshold_cells=1,
            source_bounds=(500_000.0, 1_800_000.0, 500_010.0, 1_800_010.0),
        )

    assert chars.drainage_area_km2 == pytest.approx(0.0001)
    assert chars.mean_elevation_m == 0.0
    assert chars.relief_m == 0.0
    assert chars.main_channel_length_km == pytest.approx(0.01)
    assert chars.main_channel_slope_m_per_m == pytest.approx(0.001)


def test_compute_basin_characteristics_uses_drop_over_length_when_slope_missing():
    basin_shape = box(500_000.0, 1_800_000.0, 501_000.0, 1_801_000.0)
    streams = gpd.GeoDataFrame(
        {"stream_id": [1], "strmDrop": [8.0], "Length": [2_000.0]},
        geometry=[LineString([(500_000.0, 1_800_000.0), (501_000.0, 1_800_000.0)])],
        crs=CRS_5070,
    )
    dem = np.array([[10.0, 14.0]], dtype=np.float32)

    with _memory_raster(dem) as memfile:
        chars = _compute_basin_characteristics(
            basin_shape=basin_shape,
            basin_crs=CRS_5070,
            clipped_dem_path=memfile.name,
            streams_gdf=streams,
            pour_point=Point(501_000.0, 1_800_000.0),
            pour_point_lon=-89.0,
            pour_point_lat=40.0,
            cell_area_km2=0.0001,
            threshold_cells=1,
            source_bounds=(500_000.0, 1_800_000.0, 501_000.0, 1_801_000.0),
        )

    assert chars.main_channel_length_km == pytest.approx(1.0)
    assert chars.main_channel_slope_m_per_m == pytest.approx(0.004)


def test_build_centerlines_adds_ids_without_mutating_streams():
    streams = gpd.GeoDataFrame(
        {"stream_id": [10, 11]},
        geometry=[
            LineString([(0.0, 0.0), (10.0, 10.0)]),
            LineString([(20.0, 0.0), (20.0, 10.0)]),
        ],
        crs=CRS_5070,
    )

    centerlines = _build_centerlines(streams)

    assert list(centerlines["stream_id"]) == [10, 11]
    assert list(centerlines["centerline_id"]) == [1, 2]
    assert "centerline_id" not in streams.columns


def test_build_centerlines_preserves_existing_ids():
    streams = gpd.GeoDataFrame(
        {"stream_id": [10], "centerline_id": [42]},
        geometry=[LineString([(0.0, 0.0), (10.0, 10.0)])],
        crs=CRS_5070,
    )

    centerlines = _build_centerlines(streams)

    assert list(centerlines["centerline_id"]) == [42]


def test_build_breaklines_combines_streams_and_basin_boundary():
    basin_shape = box(0.0, 0.0, 100.0, 50.0)
    stream = LineString([(10.0, 5.0), (90.0, 45.0)])
    centerlines = gpd.GeoDataFrame(
        {"centerline_id": [1]},
        geometry=[stream],
        crs=CRS_5070,
    )

    breaklines = _build_breaklines(basin_shape, centerlines, CRS_5070)

    assert list(breaklines["breakline_type"]) == ["stream", "boundary"]
    assert breaklines.geometry.iloc[0].equals(stream)
    assert breaklines.geometry.iloc[1].equals(basin_shape.boundary)
    assert breaklines.crs == CRS_5070


def test_build_breaklines_handles_empty_centerlines():
    basin_shape = box(0.0, 0.0, 100.0, 50.0)
    centerlines = gpd.GeoDataFrame({"centerline_id": []}, geometry=[], crs=CRS_5070)

    breaklines = _build_breaklines(basin_shape, centerlines, CRS_5070)

    assert len(breaklines) == 1
    assert list(breaklines["breakline_type"]) == ["boundary"]
    assert breaklines.geometry.iloc[0].equals(basin_shape.boundary)


def _make_watershed_result(tmp_path: Path) -> WatershedResult:
    basin_shape = box(0.0, 0.0, 100.0, 100.0)
    stream = LineString([(10.0, 10.0), (90.0, 90.0)])
    basin = gpd.GeoDataFrame({"name": ["watershed"]}, geometry=[basin_shape], crs=CRS_5070)
    streams = gpd.GeoDataFrame({"stream_id": [1]}, geometry=[stream], crs=CRS_5070)
    subbasins = gpd.GeoDataFrame({"wsno": [1]}, geometry=[basin_shape], crs=CRS_5070)
    centerlines = gpd.GeoDataFrame(
        {"stream_id": [1], "centerline_id": [1]},
        geometry=[stream],
        crs=CRS_5070,
    )
    breaklines = gpd.GeoDataFrame(
        {"breakline_type": ["stream", "boundary"]},
        geometry=[stream, basin_shape.boundary],
        crs=CRS_5070,
    )
    chars = BasinCharacteristics(
        drainage_area_km2=0.01,
        drainage_area_mi2=0.00386102,
        mean_elevation_m=100.0,
        relief_m=5.0,
        main_channel_length_km=0.12,
        main_channel_slope_m_per_m=0.001,
        centroid_lat=40.0,
        centroid_lon=-89.0,
        pour_point_lat=40.0,
        pour_point_lon=-89.0,
    )
    artifact = tmp_path / "fel.tif"
    artifact.write_bytes(b"synthetic artifact")

    return WatershedResult(
        basin=basin,
        streams=streams,
        subbasins=subbasins,
        centerlines=centerlines,
        breaklines=breaklines,
        pour_point=Point(90.0, 90.0),
        characteristics=chars,
        dem_clipped=tmp_path / "dem_clipped.tif",
        artifacts={"fel": artifact},
    )


def test_save_watershed_writes_vector_outputs_and_returns_artifacts(tmp_path):
    result = _make_watershed_result(tmp_path)
    output_dir = tmp_path / "watershed_outputs"

    paths = save_watershed(result, output_dir)

    assert set(paths) == {
        "basin",
        "streams",
        "subbasins",
        "centerlines",
        "breaklines",
        "fel",
    }
    for key in ("basin", "streams", "subbasins", "centerlines", "breaklines"):
        assert paths[key].exists()
        assert paths[key].parent == output_dir

    basin = gpd.read_file(paths["basin"])
    streams = gpd.read_file(paths["streams"])
    subbasins = gpd.read_file(paths["subbasins"])
    centerlines = gpd.read_file(paths["centerlines"])
    breaklines = gpd.read_file(paths["breaklines"])

    assert list(basin["name"]) == ["watershed"]
    assert list(streams["stream_id"]) == [1]
    assert list(subbasins["wsno"]) == [1]
    assert list(centerlines["centerline_id"]) == [1]
    assert set(breaklines["breakline_type"]) == {"stream", "boundary"}
    assert paths["fel"] == result.artifacts["fel"]
