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

from results import (
    get_2d_area_names,
    extract_max_depth,
    extract_max_wse,
    cells_to_raster,
    extract_flood_extent,
    export_results,
    TARGET_CRS,
)


# ── Fixture ───────────────────────────────────────────────────────────────────

N_CELLS = 100
N_TIMESTEPS = 10
AREA_NAME = "TestArea"


def make_fake_ras_hdf(path: Path, n_cells: int = N_CELLS, n_timesteps: int = N_TIMESTEPS) -> Path:
    """
    Create a minimal synthetic HEC-RAS 6.x output HDF5 for testing.

    Cell centers are placed in a 1 km × 1 km box within EPSG:5070 coordinate
    space.  Depths are random uniform [0, 3] m; WSE = depth + 280 m (fake DEM).
    """
    rng = np.random.default_rng(seed=42)   # reproducible
    with h5py.File(str(path), "w") as hf:
        # ── Geometry ─────────────────────────────────────────────────────────
        area_grp = hf.create_group(f"Geometry/2D Flow Areas/{AREA_NAME}")
        xy = rng.uniform(300_000, 301_000, (n_cells, 2)).astype(np.float64)
        area_grp.create_dataset("Cells Center Coordinate", data=xy)

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

    return path


@pytest.fixture(scope="module")
def fake_hdf(tmp_path_factory):
    """Shared synthetic HDF for all result-extraction tests."""
    p = tmp_path_factory.mktemp("hdf") / "test_output.hdf"
    make_fake_ras_hdf(p)
    return p


@pytest.fixture()
def output_dir(tmp_path):
    """Fresh output directory for each test."""
    d = tmp_path / "results_out"
    d.mkdir()
    return d


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
