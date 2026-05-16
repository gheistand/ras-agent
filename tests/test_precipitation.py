"""tests/test_precipitation.py — Unit tests for pipeline/precipitation.py"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import tempfile

import pandas as pd
import pytest
from pathlib import Path

from precipitation import (
    catalog_storms,
    select_design_storm,
    download_storm,
    run_precipitation_stage,
    check_aorc_dependencies,
    get_design_depth,
    VALID_DEPTH_SOURCES,
    StormEvent,
    PrecipitationResult,
)

BOUNDS = (-89.8, 39.5, -89.4, 39.9)  # Springfield IL area


class TestCheckDeps:
    def test_returns_dict_with_all_key(self):
        result = check_aorc_dependencies()
        assert "all_available" in result
        assert isinstance(result["all_available"], bool)

    def test_returns_individual_package_keys(self):
        result = check_aorc_dependencies()
        for pkg in ("xarray", "zarr", "s3fs", "rioxarray"):
            assert pkg in result
            assert isinstance(result[pkg], bool)


class TestCatalogStorms:
    def test_mock_returns_dataframe(self):
        df = catalog_storms(BOUNDS, [2020, 2021, 2022], mock=True)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        assert "total_depth_in" in df.columns
        assert "storm_id" in df.columns
        assert "year" in df.columns
        assert "rank" in df.columns

    def test_mock_required_columns(self):
        df = catalog_storms(BOUNDS, [2020], mock=True)
        for col in (
            "storm_id", "start_time", "end_time", "sim_start", "sim_end",
            "total_depth_in", "peak_intensity_in_hr", "duration_hours",
            "wet_hours", "rank", "year",
        ):
            assert col in df.columns, f"Missing column: {col}"

    def test_mock_rank_1_is_largest(self):
        df = catalog_storms(BOUNDS, [2020], mock=True)
        max_depth_rank = df.loc[df["total_depth_in"].idxmax(), "rank"]
        assert max_depth_rank == 1

    def test_mock_ranks_are_consecutive(self):
        df = catalog_storms(BOUNDS, [2020, 2021, 2022], mock=True)
        assert sorted(df["rank"].tolist()) == list(range(1, len(df) + 1))

    def test_mock_depths_are_positive(self):
        df = catalog_storms(BOUNDS, [2020], mock=True)
        assert (df["total_depth_in"] > 0).all()


class TestSelectDesignStorm:
    def setup_method(self):
        self.df = pd.DataFrame({
            "storm_id": [1, 2, 3],
            "total_depth_in": [1.2, 2.4, 3.8],
            "rank": [3, 2, 1],
        })

    def test_exact_match(self):
        row = select_design_storm(self.df, 2.4, tolerance_pct=0.3)
        assert row is not None
        assert abs(row["total_depth_in"] - 2.4) < 0.01

    def test_closest_within_tolerance(self):
        row = select_design_storm(self.df, 2.5, tolerance_pct=0.3)
        assert row is not None

    def test_no_match_outside_tolerance(self):
        row = select_design_storm(self.df, 10.0, tolerance_pct=0.3)
        assert row is None

    def test_empty_catalog_returns_none(self):
        row = select_design_storm(pd.DataFrame(columns=["total_depth_in"]), 2.0)
        assert row is None

    def test_returns_series(self):
        row = select_design_storm(self.df, 1.2)
        assert isinstance(row, pd.Series)


class TestDownloadStorm:
    def test_mock_creates_file(self):
        df = catalog_storms(BOUNDS, [2020], mock=True)
        storm_row = df.iloc[0]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = download_storm(storm_row, BOUNDS, tmpdir, mock=True)
            assert path.exists()
            assert path.suffix == ".nc"

    def test_mock_file_content(self):
        df = catalog_storms(BOUNDS, [2020], mock=True)
        storm_row = df.iloc[0]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = download_storm(storm_row, BOUNDS, tmpdir, mock=True)
            assert path.read_bytes() == b"MOCK_NETCDF_AORC"

    def test_mock_creates_precipitation_subdir(self):
        df = catalog_storms(BOUNDS, [2020], mock=True)
        storm_row = df.iloc[0]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = download_storm(storm_row, BOUNDS, tmpdir, mock=True)
            assert path.parent.name == "precipitation"

    def test_mock_filename_includes_storm_id(self):
        df = catalog_storms(BOUNDS, [2020], mock=True)
        storm_row = df.iloc[0]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = download_storm(storm_row, BOUNDS, tmpdir, mock=True)
            assert str(storm_row["storm_id"]) in path.name


class TestRunPrecipitationStage:
    def test_mock_returns_dict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_precipitation_stage(
                BOUNDS, tmpdir,
                target_return_periods=[2, 100],
                years=[2020, 2021, 2022],
                mock=True,
            )
        assert isinstance(result, dict)
        for k in result:
            assert k in [2, 100]

    def test_mock_matched_results_are_precipitation_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_precipitation_stage(
                BOUNDS, tmpdir,
                target_return_periods=[2, 10, 100],
                years=[2020, 2021, 2022],
                mock=True,
            )
        for rp, val in result.items():
            if val is not None:
                assert isinstance(val, PrecipitationResult)
                assert val.mock is True
                assert val.target_rp_yr == rp

    def test_mock_netcdf_files_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_precipitation_stage(
                BOUNDS, tmpdir,
                target_return_periods=[100],
                years=[2020, 2021, 2022],
                mock=True,
            )
            for rp, val in result.items():
                if val is not None:
                    assert val.netcdf_path.exists()

    def test_default_return_periods_used_when_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_precipitation_stage(
                BOUNDS, tmpdir,
                years=[2020, 2021, 2022],
                mock=True,
            )
        assert set(result.keys()) == {2, 10, 100}

    def test_depth_source_bulletin_75_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_precipitation_stage(
                BOUNDS, tmpdir,
                target_return_periods=[100],
                years=[2020, 2021, 2022],
                mock=True,
            )
        assert isinstance(result, dict)

    def test_depth_source_atlas_14(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_precipitation_stage(
                BOUNDS, tmpdir,
                target_return_periods=[100],
                years=[2020, 2021, 2022],
                depth_source="atlas_14",
                mock=True,
            )
        assert isinstance(result, dict)

    def test_depth_source_invalid_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError, match="Invalid depth_source"):
                run_precipitation_stage(
                    BOUNDS, tmpdir,
                    target_return_periods=[100],
                    years=[2020, 2021, 2022],
                    depth_source="invalid_source",
                    mock=True,
                )


class TestGetDesignDepth:
    def test_bulletin_75_returns_float(self):
        depth = get_design_depth(100, "bulletin_75")
        assert isinstance(depth, float)
        assert depth == 6.4

    def test_atlas_14_returns_float(self):
        depth = get_design_depth(100, "atlas_14")
        assert isinstance(depth, float)
        assert depth == 6.6

    def test_default_source_is_bulletin_75(self):
        depth = get_design_depth(100)
        assert depth == 6.4

    def test_invalid_source_raises(self):
        with pytest.raises(ValueError, match="Invalid depth_source"):
            get_design_depth(100, "noaa_pfds")

    def test_invalid_return_period_raises(self):
        with pytest.raises(ValueError, match="not in bulletin_75"):
            get_design_depth(75, "bulletin_75")

    def test_all_standard_rps_available(self):
        for rp in [2, 5, 10, 25, 50, 100, 500]:
            depth = get_design_depth(rp, "bulletin_75")
            assert depth > 0

    def test_monotonic_increasing(self):
        rps = [2, 5, 10, 25, 50, 100, 500]
        for source in VALID_DEPTH_SOURCES:
            depths = [get_design_depth(rp, source) for rp in rps]
            assert depths == sorted(depths), f"{source} depths not monotonic"
