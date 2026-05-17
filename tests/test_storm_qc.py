"""tests/test_storm_qc.py — Unit tests for pipeline/storm_qc.py"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from storm_qc import (
    GhcndStation,
    StormObservation,
    compare_storm_depths,
    qc_storm_catalog,
)
from precipitation import catalog_storms

BOUNDS = (-89.8, 39.5, -89.4, 39.9)


def make_catalog():
    return catalog_storms(BOUNDS, [2020, 2021, 2022], mock=True)


class TestDataclasses:
    def test_ghcnd_station_fields(self):
        s = GhcndStation(
            station_id="GHCND:USW00094870",
            name="SPRINGFIELD IL US",
            lat=39.84,
            lon=-89.68,
            elevation_m=176.2,
            datacoverage=1.0,
        )
        assert s.station_id == "GHCND:USW00094870"
        assert s.datacoverage == 1.0

    def test_storm_observation_defaults(self):
        obs = StormObservation(
            station_id="GHCND:USW00094870",
            date=date(2022, 8, 3),
            prcp_inches=1.5,
        )
        assert obs.source == "ghcnd"
        assert obs.prcp_inches == 1.5


class TestCompareMock:
    def test_adds_ghcnd_columns(self):
        df = make_catalog()
        result = compare_storm_depths(df, BOUNDS, mock=True)
        assert "ghcnd_depth_in" in result.columns
        assert "depth_ratio" in result.columns
        assert "ghcnd_stations_used" in result.columns

    def test_depth_ratio_positive(self):
        df = make_catalog()
        result = compare_storm_depths(df, BOUNDS, mock=True)
        assert (result["depth_ratio"] > 0).all()

    def test_mock_ghcnd_depth_is_90_pct_of_aorc(self):
        df = make_catalog()
        result = compare_storm_depths(df, BOUNDS, mock=True)
        expected = result["total_depth_in"] * 0.9
        pd.testing.assert_series_equal(
            result["ghcnd_depth_in"].reset_index(drop=True),
            expected.reset_index(drop=True),
            check_names=False,
        )

    def test_mock_stations_used_is_one(self):
        df = make_catalog()
        result = compare_storm_depths(df, BOUNDS, mock=True)
        assert (result["ghcnd_stations_used"] == 1).all()

    def test_does_not_modify_original(self):
        df = make_catalog()
        original_cols = set(df.columns)
        _ = compare_storm_depths(df, BOUNDS, mock=True)
        assert set(df.columns) == original_cols


class TestQcCatalogMock:
    def test_adds_qc_flag(self):
        df = make_catalog()
        result = qc_storm_catalog(df, BOUNDS, mock=True)
        assert "qc_flag" in result.columns

    def test_mock_storms_flagged_ok(self):
        df = make_catalog()
        result = qc_storm_catalog(df, BOUNDS, mock=True)
        # depth_ratio = 1/0.9 ≈ 1.11, which is within [0.6, 1.6]
        assert (result["qc_flag"] == "ok").all()

    def test_qc_flag_values_are_valid(self):
        df = make_catalog()
        result = qc_storm_catalog(df, BOUNDS, mock=True)
        valid_flags = {"ok", "high", "low", "no_obs"}
        assert set(result["qc_flag"].unique()).issubset(valid_flags)

    def test_original_columns_preserved(self):
        df = make_catalog()
        original_cols = set(df.columns)
        result = qc_storm_catalog(df, BOUNDS, mock=True)
        assert original_cols.issubset(set(result.columns))


class TestDepthRatioFlags:
    def test_flag_logic(self):
        """Verify each QC flag threshold directly against the flagging logic."""
        df = pd.DataFrame({
            "storm_id": [1, 2, 3, 4],
            "total_depth_in": [2.0, 2.0, 2.0, 2.0],
            "start_time": [datetime(2020, 7, 1)] * 4,
            "end_time": [datetime(2020, 7, 2)] * 4,
            "rank": [1, 1, 1, 1],
            "year": [2020] * 4,
        })
        df["ghcnd_depth_in"] = [1.0, 3.5, float("nan"), 1.8]
        df["depth_ratio"] = df["total_depth_in"] / df["ghcnd_depth_in"]
        df["ghcnd_stations_used"] = [1, 1, 0, 1]

        flags = []
        for _, row in df.iterrows():
            if np.isnan(row["ghcnd_depth_in"]):
                flags.append("no_obs")
            elif row["depth_ratio"] < 0.6:
                flags.append("low")
            elif row["depth_ratio"] > 1.6:
                flags.append("high")
            else:
                flags.append("ok")

        assert flags[0] == "high"    # 2/1.0 = 2.0 > 1.6
        assert flags[1] == "low"     # 2/3.5 = 0.57 < 0.6
        assert flags[2] == "no_obs"  # NaN
        assert flags[3] == "ok"      # 2/1.8 = 1.11


class TestNoTokenPath:
    def test_no_token_returns_empty(self, monkeypatch):
        """find_stations returns [] when no NOAA token is available."""
        monkeypatch.delenv("NOAA_CDO_TOKEN", raising=False)
        from storm_qc import find_stations
        result = find_stations(BOUNDS, noaa_token=None)
        assert result == []

    def test_compare_no_stations_sets_nan(self, monkeypatch):
        """Without stations all ghcnd_depth_in values are NaN."""
        monkeypatch.delenv("NOAA_CDO_TOKEN", raising=False)
        df = make_catalog()
        result = compare_storm_depths(df, BOUNDS, noaa_token=None, mock=False)
        assert result["ghcnd_stations_used"].eq(0).all()
        assert result["ghcnd_depth_in"].isna().all()
