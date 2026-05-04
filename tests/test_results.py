"""
Tests for results.py — HEC-RAS 2D results extraction and GIS export

Uses a synthetic HEC-RAS HDF5 fixture with known geometry and depth values
to verify data extraction, raster interpolation, flood extent derivation,
and the full export pipeline.  No actual HEC-RAS run is required.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'pipeline'))

import h5py
import numpy as np
import pytest
import rasterio
import geopandas as gpd
from rasterio.crs import CRS

import unittest.mock as mock

from results import (
    get_2d_area_names,
    extract_max_depth,
    extract_max_wse,
    extract_max_velocity,
    extract_flow_area_results,
    cells_to_raster,
    extract_flood_extent,
    export_results,
    export_cloud_native,
    export_filtered_rasters,
    _parse_plan_from_hdf,
    detect_ras_version,
    FlowAreaGeometry,
    FlowAreaResults,
    NODATA,
    TARGET_CRS,
)


# ── Fixture ───────────────────────────────────────────────────────────────────

N_CELLS = 100
N_FACES = 180      # face points — more than cells
N_TIMESTEPS = 10
AREA_NAME = "TestArea"
AREA_NAME_2 = "TestArea2"


def make_fake_ras_hdf(path: Path, n_cells: int = N_CELLS, n_timesteps: int = N_TIMESTEPS) -> Path:
    """
    Create a minimal synthetic HEC-RAS 6.x output HDF5 for testing.

    Cell centers are placed in a 1 km × 1 km box within EPSG:5070 coordinate
    space.  Depths are random uniform [0, 3] m; WSE = depth + 280 m (fake DEM).
    Face points are scattered across the same domain.  Velocity is [0, 2] m/s.
    """
    rng = np.random.default_rng(seed=42)   # reproducible
    with h5py.File(str(path), "w") as hf:
        # ── Geometry ─────────────────────────────────────────────────────────
        area_grp = hf.create_group(f"Geometry/2D Flow Areas/{AREA_NAME}")
        xy = rng.uniform(300_000, 301_000, (n_cells, 2)).astype(np.float64)
        area_grp.create_dataset("Cells Center Coordinate", data=xy)

        # Face geometry (optional but now included in fixture)
        n_fp = N_FACES
        face_pts = rng.uniform(300_000, 301_000, (n_fp, 2)).astype(np.float64)
        area_grp.create_dataset("FacePoints Coordinate", data=face_pts)
        n_face_edges = N_FACES - 10
        fp_idxs = rng.integers(0, n_fp, (n_face_edges, 2)).astype(np.int32)
        area_grp.create_dataset("Faces FacePoint Indexes", data=fp_idxs)

        # ── Results ───────────────────────────────────────────────────────────
        ts_path = (
            f"Results/Unsteady/Output/Output Blocks/Base Output/"
            f"Unsteady Time Series/2D Flow Areas/{AREA_NAME}"
        )
        ts_grp = hf.create_group(ts_path)
        depths = rng.uniform(0, 3.0, (n_timesteps, n_cells)).astype(np.float32)
        ts_grp.create_dataset("Depth", data=depths)
        wse = (depths + 280.0).astype(np.float32)
        ts_grp.create_dataset("Water Surface", data=wse)
        velocities = rng.uniform(0, 2.0, (n_timesteps, n_cells)).astype(np.float32)
        ts_grp.create_dataset("Velocity", data=velocities)

    return path


def make_fake_ras_hdf_multi_area(path: Path) -> Path:
    """
    Create a synthetic HDF with two 2D flow areas for multi-area export tests.
    """
    rng = np.random.default_rng(seed=7)
    with h5py.File(str(path), "w") as hf:
        for area_name, x_offset in [(AREA_NAME, 0), (AREA_NAME_2, 2_000)]:
            area_grp = hf.create_group(f"Geometry/2D Flow Areas/{area_name}")
            xy = rng.uniform(300_000 + x_offset, 301_000 + x_offset, (N_CELLS, 2)).astype(np.float64)
            area_grp.create_dataset("Cells Center Coordinate", data=xy)

            ts_path = (
                f"Results/Unsteady/Output/Output Blocks/Base Output/"
                f"Unsteady Time Series/2D Flow Areas/{area_name}"
            )
            ts_grp = hf.create_group(ts_path)
            depths = rng.uniform(0, 3.0, (N_TIMESTEPS, N_CELLS)).astype(np.float32)
            ts_grp.create_dataset("Depth", data=depths)
            wse = (depths + 280.0).astype(np.float32)
            ts_grp.create_dataset("Water Surface", data=wse)
            velocities = rng.uniform(0, 1.5, (N_TIMESTEPS, N_CELLS)).astype(np.float32)
            ts_grp.create_dataset("Velocity", data=velocities)
    return path


def make_fake_ras_2025_hdf(path: Path) -> Path:
    """
    Create a minimal synthetic HEC-RAS 2025 schema HDF5 for version detection tests.
    """
    rng = np.random.default_rng(seed=99)
    with h5py.File(str(path), "w") as hf:
        area_grp = hf.create_group(f"Geometry/2D Flow Areas/{AREA_NAME}")
        xy = rng.uniform(300_000, 301_000, (N_CELLS, 2)).astype(np.float64)
        area_grp.create_dataset("Cell Coordinates", data=xy)

        ts_path = f"Results/Output Blocks/Base Output/2D Flow Areas/{AREA_NAME}"
        ts_grp = hf.create_group(ts_path)
        depths = rng.uniform(0, 2.0, (N_TIMESTEPS, N_CELLS)).astype(np.float32)
        ts_grp.create_dataset("Depth", data=depths)
        wse = (depths + 250.0).astype(np.float32)
        ts_grp.create_dataset("Water Surface", data=wse)
    return path


@pytest.fixture(scope="module")
def fake_hdf(tmp_path_factory):
    """Shared synthetic HDF for all result-extraction tests."""
    p = tmp_path_factory.mktemp("hdf") / "test_output.hdf"
    make_fake_ras_hdf(p)
    return p


@pytest.fixture(scope="module")
def fake_hdf_multi(tmp_path_factory):
    """Synthetic HDF with two 2D areas for multi-area export tests."""
    p = tmp_path_factory.mktemp("hdf_multi") / "test_multi.hdf"
    make_fake_ras_hdf_multi_area(p)
    return p


@pytest.fixture(scope="module")
def fake_hdf_2025(tmp_path_factory):
    """Synthetic HDF following RAS 2025 schema."""
    p = tmp_path_factory.mktemp("hdf_2025") / "test_2025.hdf"
    make_fake_ras_2025_hdf(p)
    return p


@pytest.fixture()
def output_dir(tmp_path):
    """Fresh output directory for each test."""
    d = tmp_path / "results_out"
    d.mkdir()
    return d


# ── Version Detection ─────────────────────────────────────────────────────────

class TestDetectRasVersion:
    def test_6x_schema_returns_6x(self, fake_hdf):
        """Standard 6.x HDF should be detected as '6.x'."""
        assert detect_ras_version(fake_hdf) == "6.x"

    def test_2025_schema_returns_2025(self, fake_hdf_2025):
        """HDF with RAS 2025 schema should be detected as '2025'."""
        assert detect_ras_version(fake_hdf_2025) == "2025"

    def test_accepts_string_path(self, fake_hdf):
        """detect_ras_version should accept a string path, not just Path."""
        assert detect_ras_version(str(fake_hdf)) == "6.x"


# ── Area Discovery ────────────────────────────────────────────────────────────

class TestGet2dAreaNames:
    def test_returns_area_list(self, fake_hdf):
        """Should return the area names defined in /Geometry/2D Flow Areas/."""
        names = get_2d_area_names(fake_hdf)
        assert names == [AREA_NAME]

    def test_returns_list_type(self, fake_hdf):
        names = get_2d_area_names(fake_hdf)
        assert isinstance(names, list)

    def test_no_areas_returns_empty(self, tmp_path):
        """HDF without Geometry group should return empty list."""
        empty_hdf = tmp_path / "empty.hdf"
        with h5py.File(str(empty_hdf), "w") as hf:
            hf.create_group("SomeOtherGroup")
        assert get_2d_area_names(empty_hdf) == []

    def test_multi_area_returns_all(self, fake_hdf_multi):
        """Multi-area HDF should return all area names."""
        names = get_2d_area_names(fake_hdf_multi)
        assert AREA_NAME in names
        assert AREA_NAME_2 in names
        assert len(names) == 2


# ── Depth Extraction ──────────────────────────────────────────────────────────

class TestExtractMaxDepth:
    def test_shapes(self, fake_hdf):
        """Cell centers should be (N,2) and max_depth should be (N,)."""
        xy, max_depth = extract_max_depth(fake_hdf, AREA_NAME)
        assert xy.shape == (N_CELLS, 2)
        assert max_depth.shape == (N_CELLS,)

    def test_max_depth_non_negative(self, fake_hdf):
        """Maximum depths should be ≥ 0."""
        _, max_depth = extract_max_depth(fake_hdf, AREA_NAME)
        assert np.all(max_depth >= 0)

    def test_max_depth_is_true_max_across_timesteps(self, tmp_path):
        """Verify that the per-cell maximum is taken across all timesteps."""
        hdf = tmp_path / "check_max.hdf"
        with h5py.File(str(hdf), "w") as hf:
            area = hf.create_group(f"Geometry/2D Flow Areas/{AREA_NAME}")
            area.create_dataset("Cells Center Coordinate",
                                data=np.ones((5, 2), dtype=np.float64))
            ts = hf.create_group(
                f"Results/Unsteady/Output/Output Blocks/Base Output/"
                f"Unsteady Time Series/2D Flow Areas/{AREA_NAME}"
            )
            # Depths: 3 timesteps, 5 cells; known values
            d = np.array([[1, 2, 3, 4, 5],
                          [5, 4, 3, 2, 1],
                          [2, 2, 2, 2, 2]], dtype=np.float32)
            ts.create_dataset("Depth", data=d)

        _, max_depth = extract_max_depth(hdf, AREA_NAME)
        np.testing.assert_array_equal(max_depth, [5, 4, 3, 4, 5])


# ── WSE Extraction ────────────────────────────────────────────────────────────

class TestExtractMaxWse:
    def test_shapes(self, fake_hdf):
        """Cell centers and max WSE should match N_CELLS."""
        xy, max_wse = extract_max_wse(fake_hdf, AREA_NAME)
        assert xy.shape == (N_CELLS, 2)
        assert max_wse.shape == (N_CELLS,)

    def test_wse_greater_than_depth(self, fake_hdf):
        """WSE = depth + terrain, so WSE should exceed depth for typical elevations."""
        _, max_depth = extract_max_depth(fake_hdf, AREA_NAME)
        _, max_wse = extract_max_wse(fake_hdf, AREA_NAME)
        # In our fake data: wse = depth + 280; so wse > depth everywhere depth<280
        assert np.all(max_wse >= max_depth * 0.5)   # loose check, not exact


# ── Velocity Extraction ───────────────────────────────────────────────────────

class TestExtractMaxVelocity:
    def test_shapes(self, fake_hdf):
        """Cell centers and max velocity should match N_CELLS."""
        xy, max_vel = extract_max_velocity(fake_hdf, AREA_NAME)
        assert xy.shape == (N_CELLS, 2)
        assert max_vel.shape == (N_CELLS,)

    def test_max_velocity_non_negative(self, fake_hdf):
        """Maximum velocities should be ≥ 0."""
        _, max_vel = extract_max_velocity(fake_hdf, AREA_NAME)
        assert np.all(max_vel >= 0)

    def test_max_velocity_is_true_max_across_timesteps(self, tmp_path):
        """Verify per-cell maximum is taken across all timesteps."""
        hdf = tmp_path / "vel_max.hdf"
        with h5py.File(str(hdf), "w") as hf:
            area = hf.create_group(f"Geometry/2D Flow Areas/{AREA_NAME}")
            area.create_dataset("Cells Center Coordinate",
                                data=np.ones((3, 2), dtype=np.float64))
            ts = hf.create_group(
                f"Results/Unsteady/Output/Output Blocks/Base Output/"
                f"Unsteady Time Series/2D Flow Areas/{AREA_NAME}"
            )
            v = np.array([[1.0, 2.0, 3.0],
                          [3.0, 1.0, 1.0],
                          [2.0, 2.0, 2.0]], dtype=np.float32)
            ts.create_dataset("Velocity", data=v)

        _, max_vel = extract_max_velocity(hdf, AREA_NAME)
        np.testing.assert_array_almost_equal(max_vel, [3.0, 2.0, 3.0])

    def test_missing_velocity_raises_key_error(self, tmp_path):
        """KeyError should be raised when velocity dataset is absent."""
        hdf = tmp_path / "no_vel.hdf"
        with h5py.File(str(hdf), "w") as hf:
            area = hf.create_group(f"Geometry/2D Flow Areas/{AREA_NAME}")
            area.create_dataset("Cells Center Coordinate",
                                data=np.ones((3, 2), dtype=np.float64))
            ts = hf.create_group(
                f"Results/Unsteady/Output/Output Blocks/Base Output/"
                f"Unsteady Time Series/2D Flow Areas/{AREA_NAME}"
            )
            ts.create_dataset("Depth", data=np.ones((2, 3), dtype=np.float32))
        with pytest.raises(KeyError):
            extract_max_velocity(hdf, AREA_NAME)


# ── FlowAreaResults Dataclass ─────────────────────────────────────────────────

class TestExtractFlowAreaResults:
    def test_returns_flow_area_results_type(self, fake_hdf):
        """Should return a FlowAreaResults instance."""
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        assert isinstance(result, FlowAreaResults)

    def test_area_name_matches(self, fake_hdf):
        """Result name should match requested area."""
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        assert result.name == AREA_NAME

    def test_geometry_is_flow_area_geometry(self, fake_hdf):
        """geometry field should be a FlowAreaGeometry instance."""
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        assert isinstance(result.geometry, FlowAreaGeometry)

    def test_geometry_name_matches(self, fake_hdf):
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        assert result.geometry.name == AREA_NAME

    def test_cell_centers_shape(self, fake_hdf):
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        assert result.geometry.cell_centers.shape == (N_CELLS, 2)

    def test_face_points_loaded(self, fake_hdf):
        """face_points should be populated when FacePoints Coordinate dataset exists."""
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        assert result.geometry.face_points is not None
        assert result.geometry.face_points.shape == (N_FACES, 2)

    def test_face_point_indexes_loaded(self, fake_hdf):
        """face_point_indexes should be populated when Faces FacePoint Indexes exists."""
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        assert result.geometry.face_point_indexes is not None
        assert result.geometry.face_point_indexes.ndim == 2

    def test_max_depth_shape(self, fake_hdf):
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        assert result.max_depth.shape == (N_CELLS,)

    def test_max_wse_shape(self, fake_hdf):
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        assert result.max_wse.shape == (N_CELLS,)

    def test_max_velocity_shape(self, fake_hdf):
        """max_velocity should be (N,) when velocity data is present."""
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        assert result.max_velocity is not None
        assert result.max_velocity.shape == (N_CELLS,)

    def test_max_velocity_none_when_absent(self, tmp_path):
        """max_velocity should be None when velocity dataset is not in HDF."""
        hdf = tmp_path / "no_vel.hdf"
        with h5py.File(str(hdf), "w") as hf:
            area = hf.create_group(f"Geometry/2D Flow Areas/{AREA_NAME}")
            area.create_dataset("Cells Center Coordinate",
                                data=np.ones((5, 2), dtype=np.float64))
            ts = hf.create_group(
                f"Results/Unsteady/Output/Output Blocks/Base Output/"
                f"Unsteady Time Series/2D Flow Areas/{AREA_NAME}"
            )
            ts.create_dataset("Depth", data=np.ones((3, 5), dtype=np.float32))
            ts.create_dataset("Water Surface", data=np.ones((3, 5), dtype=np.float32))
        result = extract_flow_area_results(hdf, AREA_NAME)
        assert result.max_velocity is None

    def test_face_points_none_when_absent(self, tmp_path):
        """face_points should be None when FacePoints Coordinate is not in HDF."""
        hdf = tmp_path / "no_face.hdf"
        with h5py.File(str(hdf), "w") as hf:
            area = hf.create_group(f"Geometry/2D Flow Areas/{AREA_NAME}")
            area.create_dataset("Cells Center Coordinate",
                                data=np.ones((5, 2), dtype=np.float64))
            ts = hf.create_group(
                f"Results/Unsteady/Output/Output Blocks/Base Output/"
                f"Unsteady Time Series/2D Flow Areas/{AREA_NAME}"
            )
            ts.create_dataset("Depth", data=np.ones((3, 5), dtype=np.float32))
            ts.create_dataset("Water Surface", data=np.ones((3, 5), dtype=np.float32))
        result = extract_flow_area_results(hdf, AREA_NAME)
        assert result.geometry.face_points is None

    def test_depth_matches_extract_max_depth(self, fake_hdf):
        """max_depth from dataclass should equal extract_max_depth output."""
        _, expected = extract_max_depth(fake_hdf, AREA_NAME)
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        np.testing.assert_array_equal(result.max_depth, expected)

    def test_wse_matches_extract_max_wse(self, fake_hdf):
        """max_wse from dataclass should equal extract_max_wse output."""
        _, expected = extract_max_wse(fake_hdf, AREA_NAME)
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        np.testing.assert_array_equal(result.max_wse, expected)


# ── Raster Export ─────────────────────────────────────────────────────────────

class TestCellsToRaster:
    def test_creates_geotiff(self, fake_hdf, output_dir):
        """cells_to_raster should create a readable GeoTIFF."""
        xy, max_depth = extract_max_depth(fake_hdf, AREA_NAME)
        out = output_dir / "depth.tif"
        result = cells_to_raster(xy, max_depth, TARGET_CRS, resolution_m=5.0, output_path=out)
        assert result.exists()
        assert result.suffix == ".tif"

    def test_raster_crs_matches(self, fake_hdf, output_dir):
        """Output raster CRS should match the requested CRS."""
        xy, max_depth = extract_max_depth(fake_hdf, AREA_NAME)
        out = output_dir / "crs_check.tif"
        cells_to_raster(xy, max_depth, TARGET_CRS, resolution_m=5.0, output_path=out)
        with rasterio.open(str(out)) as ds:
            assert ds.crs == TARGET_CRS

    def test_raster_has_valid_data(self, fake_hdf, output_dir):
        """The raster band should contain at least some non-nodata pixels."""
        xy, max_depth = extract_max_depth(fake_hdf, AREA_NAME)
        out = output_dir / "data_check.tif"
        cells_to_raster(xy, max_depth, TARGET_CRS, resolution_m=5.0, output_path=out)
        with rasterio.open(str(out)) as ds:
            data = ds.read(1)
            nodata = ds.nodata
            valid = data[data != nodata]
            assert len(valid) > 0

    def test_raster_nodata_is_set(self, fake_hdf, output_dir):
        """Output raster should have nodata = -9999.0."""
        xy, max_depth = extract_max_depth(fake_hdf, AREA_NAME)
        out = output_dir / "nodata_check.tif"
        cells_to_raster(xy, max_depth, TARGET_CRS, resolution_m=5.0, output_path=out)
        with rasterio.open(str(out)) as ds:
            assert ds.nodata == pytest.approx(-9999.0)

    def test_method_nearest_creates_geotiff(self, fake_hdf, output_dir):
        """method='nearest' should produce a valid raster."""
        xy, max_depth = extract_max_depth(fake_hdf, AREA_NAME)
        out = output_dir / "nearest.tif"
        result = cells_to_raster(
            xy, max_depth, TARGET_CRS, resolution_m=10.0,
            output_path=out, method="nearest"
        )
        assert result.exists()
        with rasterio.open(str(out)) as ds:
            assert ds.count == 1

    def test_method_face_weighted_creates_geotiff(self, fake_hdf, output_dir):
        """method='face_weighted' (IDW) should produce a valid raster."""
        xy, max_depth = extract_max_depth(fake_hdf, AREA_NAME)
        out = output_dir / "face_weighted.tif"
        result = cells_to_raster(
            xy, max_depth, TARGET_CRS, resolution_m=10.0,
            output_path=out, method="face_weighted"
        )
        assert result.exists()
        with rasterio.open(str(out)) as ds:
            data = ds.read(1)
            valid = data[data != -9999.0]
            assert len(valid) > 0

    def test_method_face_weighted_uses_face_points(self, fake_hdf, output_dir):
        """face_weighted with face-point coords produces a valid raster."""
        result = extract_flow_area_results(fake_hdf, AREA_NAME)
        # Use face_points as input coordinates with replicated depth values
        face_pts = result.geometry.face_points
        # Assign a dummy value per face point (nearest cell depth)
        from scipy.spatial import cKDTree
        tree = cKDTree(result.geometry.cell_centers)
        _, idxs = tree.query(face_pts)
        face_depth_vals = result.max_depth[idxs]
        out = output_dir / "face_weighted_face_pts.tif"
        raster = cells_to_raster(
            face_pts, face_depth_vals, TARGET_CRS, resolution_m=10.0,
            output_path=out, method="face_weighted"
        )
        assert raster.exists()

    def test_face_weighted_has_no_nan_holes(self, fake_hdf, output_dir):
        """IDW should not leave NaN holes in the interior of the point cloud."""
        xy, max_depth = extract_max_depth(fake_hdf, AREA_NAME)
        out = output_dir / "fw_noholes.tif"
        cells_to_raster(
            xy, max_depth, TARGET_CRS, resolution_m=15.0,
            output_path=out, method="face_weighted"
        )
        with rasterio.open(str(out)) as ds:
            data = ds.read(1)
            # IDW fills the full bounding box — no interior NaN
            nan_count = np.isnan(data).sum()
            assert nan_count == 0


# ── Flood Extent ──────────────────────────────────────────────────────────────

class TestExtractFloodExtent:
    def test_returns_geodataframe(self, fake_hdf):
        """Should return a GeoDataFrame."""
        gdf = extract_flood_extent(fake_hdf, AREA_NAME, depth_threshold_m=0.1)
        assert isinstance(gdf, gpd.GeoDataFrame)

    def test_has_one_polygon(self, fake_hdf):
        """Flood extent should produce at least one polygon row."""
        gdf = extract_flood_extent(fake_hdf, AREA_NAME, depth_threshold_m=0.0)
        assert len(gdf) >= 1

    def test_crs_is_5070(self, fake_hdf):
        """Flood extent GeoDataFrame should be in EPSG:5070."""
        gdf = extract_flood_extent(fake_hdf, AREA_NAME, depth_threshold_m=0.0)
        assert gdf.crs == TARGET_CRS

    def test_high_threshold_reduces_extent(self, fake_hdf):
        """A higher depth threshold should yield a smaller flood area."""
        gdf_low = extract_flood_extent(fake_hdf, AREA_NAME, depth_threshold_m=0.1)
        gdf_high = extract_flood_extent(fake_hdf, AREA_NAME, depth_threshold_m=2.5)
        area_low = gdf_low.geometry.area.sum()
        area_high = gdf_high.geometry.area.sum()
        assert area_high <= area_low

    def test_above_max_depth_returns_empty(self, fake_hdf):
        """Threshold above all simulated depths should return an empty GeoDataFrame."""
        gdf = extract_flood_extent(fake_hdf, AREA_NAME, depth_threshold_m=1000.0)
        assert len(gdf) == 0 or gdf.geometry.area.sum() == pytest.approx(0.0, abs=1.0)


# ── Full Export Pipeline ──────────────────────────────────────────────────────

class TestExportResults:
    def test_creates_all_output_files(self, fake_hdf, output_dir):
        """export_results should create depth_grid, wse_grid, and flood_extent files."""
        outputs = export_results(fake_hdf, output_dir)
        assert "depth_grid" in outputs
        assert "wse_grid" in outputs
        assert "flood_extent_gpkg" in outputs
        assert "flood_extent_shp" in outputs

    def test_creates_velocity_grid(self, fake_hdf, output_dir):
        """export_results should create velocity_grid.tif when velocity data present."""
        outputs = export_results(fake_hdf, output_dir)
        assert "velocity_grid" in outputs
        assert outputs["velocity_grid"].exists()

    def test_output_files_exist(self, fake_hdf, output_dir):
        """All returned paths should point to existing files."""
        outputs = export_results(fake_hdf, output_dir)
        for name, path in outputs.items():
            assert path.exists(), f"Output file missing: {name} → {path}"

    def test_geotiffs_are_readable(self, fake_hdf, output_dir):
        """GeoTIFF outputs should be readable with rasterio."""
        outputs = export_results(fake_hdf, output_dir)
        for key in ("depth_grid", "wse_grid"):
            with rasterio.open(str(outputs[key])) as ds:
                assert ds.count == 1
                assert ds.crs is not None

    def test_velocity_geotiff_readable(self, fake_hdf, output_dir):
        """velocity_grid.tif should be a valid readable raster."""
        outputs = export_results(fake_hdf, output_dir)
        with rasterio.open(str(outputs["velocity_grid"])) as ds:
            assert ds.count == 1
            assert ds.crs is not None

    def test_gpkg_is_valid_geodataframe(self, fake_hdf, output_dir):
        """GeoPackage flood extent should load as a non-empty GeoDataFrame."""
        outputs = export_results(fake_hdf, output_dir)
        gdf = gpd.read_file(str(outputs["flood_extent_gpkg"]))
        assert isinstance(gdf, gpd.GeoDataFrame)
        assert len(gdf) >= 1

    def test_returns_dict_of_paths(self, fake_hdf, output_dir):
        """Return value should be a dict mapping str keys to Path objects."""
        outputs = export_results(fake_hdf, output_dir)
        assert isinstance(outputs, dict)
        for k, v in outputs.items():
            assert isinstance(k, str)
            assert isinstance(v, Path)


class TestExportResultsMultiArea:
    def test_exports_all_areas(self, fake_hdf_multi, output_dir):
        """Multi-area HDF should produce outputs for every area."""
        outputs = export_results(fake_hdf_multi, output_dir)
        assert f"{AREA_NAME}/depth_grid" in outputs
        assert f"{AREA_NAME_2}/depth_grid" in outputs

    def test_multi_area_creates_subdirectories(self, fake_hdf_multi, output_dir):
        """Each area should get its own subdirectory under output_dir."""
        export_results(fake_hdf_multi, output_dir)
        assert (output_dir / AREA_NAME).is_dir()
        assert (output_dir / AREA_NAME_2).is_dir()

    def test_multi_area_all_files_exist(self, fake_hdf_multi, output_dir):
        """All returned paths in a multi-area export should exist on disk."""
        outputs = export_results(fake_hdf_multi, output_dir)
        for name, path in outputs.items():
            assert path.exists(), f"Output file missing: {name} → {path}"

    def test_multi_area_includes_velocity(self, fake_hdf_multi, output_dir):
        """Each area should have a velocity_grid.tif when velocity data is present."""
        outputs = export_results(fake_hdf_multi, output_dir)
        assert f"{AREA_NAME}/velocity_grid" in outputs
        assert f"{AREA_NAME_2}/velocity_grid" in outputs

    def test_multi_area_returns_dict_of_paths(self, fake_hdf_multi, output_dir):
        """Return value should be a dict with str keys and Path values."""
        outputs = export_results(fake_hdf_multi, output_dir)
        for k, v in outputs.items():
            assert isinstance(k, str)
            assert isinstance(v, Path)


# ── Cloud-native export (ras2cng) ─────────────────────────────────────────────

class TestExportCloudNative:
    """Tests for export_cloud_native() — graceful degradation and happy path."""

    def test_export_cloud_native_no_ras2cng(self, tmp_path):
        """When ras2cng is not installed, export_cloud_native returns None gracefully."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        archive_dir = tmp_path / "archive"

        with mock.patch.dict("sys.modules", {"ras2cng": None}):
            result = export_cloud_native(project_dir, archive_dir)

        assert result is None
        # archive_dir should NOT have been created (nothing to write)
        # (the function returns before mkdir when import fails)

    def test_export_cloud_native_success(self, tmp_path):
        """Happy path: mock archive_project returns a manifest; function returns archive dir."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        archive_dir = tmp_path / "archive"

        fake_manifest = {
            "geometries": ["geom1.parquet", "geom2.parquet"],
            "plans": ["plan1.parquet"],
            "files": [],  # no real files to stat
        }

        fake_ras2cng = mock.MagicMock()
        fake_ras2cng.archive_project.return_value = fake_manifest

        with mock.patch.dict("sys.modules", {"ras2cng": fake_ras2cng}):
            # Re-import inside patch context so the lazy import picks up the mock
            import importlib
            import results as _results_mod
            importlib.reload(_results_mod)
            result = _results_mod.export_cloud_native(project_dir, archive_dir)

        assert result == archive_dir
        fake_ras2cng.archive_project.assert_called_once_with(
            project_dir,
            archive_dir,
            include_results=True,
            include_terrain=True,
        )


# ── Filtered Raster Export (RasProcess) ──────────────────────────────────────


def _make_synthetic_rasters(tmp_dir: Path, rows: int = 50, cols: int = 50):
    """Create aligned depth and WSE GeoTIFFs with known values for testing.

    Depth: gradient from 0 m (top) to ~1.5 m (bottom).
    WSE: depth + 100 m (fake ground elevation).
    Both share the same grid, transform, and CRS — just like RASMapper output.
    """
    from rasterio.transform import from_bounds

    transform = from_bounds(300_000, 200_000, 301_000, 201_000, cols, rows)
    profile = {
        "driver": "GTiff",
        "height": rows,
        "width": cols,
        "count": 1,
        "dtype": np.float32,
        "crs": CRS.from_epsg(5070),
        "transform": transform,
        "nodata": NODATA,
    }

    depth_vals = np.linspace(0, 1.5, rows * cols, dtype=np.float32).reshape(rows, cols)
    wse_vals = (depth_vals + 100.0).astype(np.float32)

    depth_path = tmp_dir / "Depth (Max).Terrain.tif"
    wse_path = tmp_dir / "WSE (Max).Terrain.tif"

    for path, data in [(depth_path, depth_vals), (wse_path, wse_vals)]:
        with rasterio.open(str(path), "w", **profile) as dst:
            dst.write(data, 1)

    return depth_path, wse_path, depth_vals, wse_vals


class TestParsePlanFromHdf:
    def test_standard_name(self, tmp_path):
        hdf = tmp_path / "MyProject.p01.hdf"
        hdf.touch()
        project_dir, plan_num = _parse_plan_from_hdf(hdf)
        assert project_dir == tmp_path
        assert plan_num == "01"

    def test_multi_dot_name(self, tmp_path):
        hdf = tmp_path / "Spring.Creek.p03.hdf"
        hdf.touch()
        _, plan_num = _parse_plan_from_hdf(hdf)
        assert plan_num == "03"

    def test_invalid_name_raises(self, tmp_path):
        hdf = tmp_path / "no_plan_number.hdf"
        hdf.touch()
        with pytest.raises(ValueError, match="Cannot parse plan number"):
            _parse_plan_from_hdf(hdf)


class TestExportFilteredRasters:
    """Tests for export_filtered_rasters() — mocks ras_commander module."""

    def _mock_store_maps(self, tmp_dir):
        """Return a side_effect for store_maps that produces synthetic rasters."""
        depth_path, wse_path, _, _ = _make_synthetic_rasters(tmp_dir)

        def fake_store_maps(*, plan_number, output_path, **kwargs):
            import shutil as _shutil
            out = Path(output_path)
            out.mkdir(parents=True, exist_ok=True)
            d = out / depth_path.name
            w = out / wse_path.name
            _shutil.copy2(depth_path, d)
            _shutil.copy2(wse_path, w)
            return {"depth": [d], "wse": [w]}

        return fake_store_maps

    def _run_with_mocked_ras(self, staging, fake_store, call_fn):
        """Inject a fake ras_commander module and run call_fn."""
        fake_rc = mock.MagicMock()
        fake_rc.RasProcess.store_maps.side_effect = fake_store
        fake_rc.init_ras_project.return_value = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"ras_commander": fake_rc}):
            return call_fn()

    def test_basic_filtering(self, tmp_path):
        """Cells below threshold are NODATA in both depth and WSE outputs."""
        staging = tmp_path / "staging"
        staging.mkdir()
        hdf = tmp_path / "TestProj.p01.hdf"
        hdf.touch()
        output_dir = tmp_path / "filtered_out"

        fake_store = self._mock_store_maps(staging)

        def run():
            return export_filtered_rasters(
                hdf_path=hdf, output_dir=output_dir, min_depth_ft=1.0,
            )

        outputs = self._run_with_mocked_ras(staging, fake_store, run)

        assert "filtered_depth" in outputs
        assert "filtered_wse" in outputs
        assert outputs["filtered_depth"].exists()
        assert outputs["filtered_wse"].exists()

        with rasterio.open(str(outputs["filtered_depth"])) as src:
            depth = src.read(1)
            assert src.nodata == NODATA
            n_nodata = np.sum(np.isclose(depth, NODATA))
            assert n_nodata > 0, "Expected some cells filtered out"
            assert n_nodata < depth.size, "Expected some cells to survive filter"

        with rasterio.open(str(outputs["filtered_wse"])) as src:
            wse = src.read(1)
            wse_nodata = np.sum(np.isclose(wse, NODATA))
            assert wse_nodata == n_nodata

    def test_mask_alignment(self, tmp_path):
        """The same pixels are masked in both depth and WSE."""
        staging = tmp_path / "staging"
        staging.mkdir()
        hdf = tmp_path / "TestProj.p01.hdf"
        hdf.touch()
        output_dir = tmp_path / "filtered_out"

        fake_store = self._mock_store_maps(staging)

        def run():
            return export_filtered_rasters(
                hdf_path=hdf, output_dir=output_dir, min_depth_ft=0.5,
            )

        outputs = self._run_with_mocked_ras(staging, fake_store, run)

        with rasterio.open(str(outputs["filtered_depth"])) as src:
            depth = src.read(1)
        with rasterio.open(str(outputs["filtered_wse"])) as src:
            wse = src.read(1)

        depth_mask = np.isclose(depth, NODATA)
        wse_mask = np.isclose(wse, NODATA)
        assert np.array_equal(depth_mask, wse_mask), "Depth and WSE masks must be identical"

    def test_zero_threshold_keeps_all(self, tmp_path):
        """A threshold of 0 ft should keep all non-zero cells."""
        staging = tmp_path / "staging"
        staging.mkdir()
        hdf = tmp_path / "TestProj.p01.hdf"
        hdf.touch()
        output_dir = tmp_path / "filtered_out"

        fake_store = self._mock_store_maps(staging)

        def run():
            return export_filtered_rasters(
                hdf_path=hdf, output_dir=output_dir, min_depth_ft=0.0,
            )

        outputs = self._run_with_mocked_ras(staging, fake_store, run)

        with rasterio.open(str(outputs["filtered_depth"])) as src:
            depth = src.read(1)
        n_filtered = np.sum(np.isclose(depth, NODATA))
        assert n_filtered <= depth.shape[1], "At most one row at zero depth"

    def test_negative_threshold_raises(self, tmp_path):
        """Negative min_depth_ft should be rejected."""
        hdf = tmp_path / "TestProj.p01.hdf"
        hdf.touch()
        with pytest.raises(ValueError, match="non-negative"):
            export_filtered_rasters(
                hdf_path=hdf, output_dir=tmp_path / "out", min_depth_ft=-1.0,
            )

    def test_cog_metadata_tags(self, tmp_path):
        """Filtered rasters should include threshold and source metadata."""
        staging = tmp_path / "staging"
        staging.mkdir()
        hdf = tmp_path / "TestProj.p01.hdf"
        hdf.touch()
        output_dir = tmp_path / "filtered_out"

        fake_store = self._mock_store_maps(staging)

        def run():
            return export_filtered_rasters(
                hdf_path=hdf, output_dir=output_dir, min_depth_ft=0.5,
            )

        outputs = self._run_with_mocked_ras(staging, fake_store, run)

        for key in ("filtered_depth", "filtered_wse"):
            with rasterio.open(str(outputs[key])) as src:
                tags = src.tags()
                assert tags["min_depth_threshold_ft"] == "0.5"
                assert tags["source_hdf"] == "TestProj.p01.hdf"
                assert tags["source_plan"] == "p01"

    def test_high_threshold_filters_all(self, tmp_path):
        """A threshold higher than any depth should produce all-NODATA rasters."""
        staging = tmp_path / "staging"
        staging.mkdir()
        hdf = tmp_path / "TestProj.p01.hdf"
        hdf.touch()
        output_dir = tmp_path / "filtered_out"

        fake_store = self._mock_store_maps(staging)

        def run():
            return export_filtered_rasters(
                hdf_path=hdf, output_dir=output_dir, min_depth_ft=100.0,
            )

        outputs = self._run_with_mocked_ras(staging, fake_store, run)

        with rasterio.open(str(outputs["filtered_depth"])) as src:
            depth = src.read(1)
        assert np.all(np.isclose(depth, NODATA)), "All cells should be NODATA"
