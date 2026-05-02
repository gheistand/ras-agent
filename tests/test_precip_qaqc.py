"""
Tests for station precipitation QAQC artifacts.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import precip_qaqc


def test_station_precip_qaqc_writes_table_json_and_figure(tmp_path: Path):
    stations = [
        {
            "station_id": "GHCND:USC00110072",
            "station_name": "Springfield 2 N",
            "distance_mi": 4.2,
            "observed_depth_in": 4.2,
        },
        {
            "station_id": "GHCND:USC00117657",
            "station_name": "Rochester",
            "distance_mi": 9.8,
            "observed_depth_in": 4.6,
        },
        {
            "station_id": "GHCND:USC00118884",
            "station_name": "Missing Station",
            "distance_mi": 14.0,
            "observed_depth_in": None,
            "missing_reason": "No PRCP value for event window",
        },
    ]

    result = precip_qaqc.build_station_precip_qaqc(
        stations=stations,
        output_dir=tmp_path,
        event_start="2024-07-14T00:00",
        event_end="2024-07-15T00:00",
        gridded_source="AORC",
        gridded_depth_in=4.0,
        search_radius_mi=25.0,
        generated_at="2026-05-01 00:00:00 UTC",
    )

    assert result["summary"]["station_count"] == 3
    assert result["summary"]["valid_observation_count"] == 2
    assert result["summary"]["station_to_grid_ratio_median"] == 1.1
    assert result["summary"]["assessment"] == "supports_grid"
    assert "station-observations-missing" in {flag["id"] for flag in result["flags"]}

    json_path = Path(result["artifacts"]["station_qaqc_json"])
    csv_path = Path(result["artifacts"]["station_table_csv"])
    figure_path = Path(result["artifacts"]["figure_png"])
    assert json_path.exists()
    assert csv_path.exists()
    assert figure_path.exists()

    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == "station-precip-qaqc/v1"
    assert "station_to_grid_ratio" in csv_path.read_text(encoding="utf-8")


def test_station_precip_qaqc_records_no_token_no_station_gap():
    result = precip_qaqc.compare_station_precipitation(
        stations=[],
        event_start="2024-07-14T00:00",
        event_end="2024-07-15T00:00",
        gridded_source="MRMS",
        gridded_depth_in=3.1,
        noaa_token_available=False,
        search_radius_mi=20.0,
        generated_at="2026-05-01 00:00:00 UTC",
    )

    flag_ids = {flag["id"] for flag in result["flags"]}
    assert result["summary"]["assessment"] == "insufficient_station_evidence"
    assert result["station_network"]["station_count"] == 0
    assert "no-noaa-token" in flag_ids
    assert "no-nearby-stations" in flag_ids
    assert "no-noaa-token" in result["summary"]["missing_data_conditions"]


def test_station_precip_qaqc_records_no_observation_gap():
    result = precip_qaqc.compare_station_precipitation(
        stations=[
            {
                "station_id": "GHCND:USC00110072",
                "observed_depth_in": None,
                "missing_reason": "No daily PRCP value",
            }
        ],
        event_start="2024-07-14T00:00",
        event_end="2024-07-15T00:00",
        gridded_source="AORC",
        gridded_depth_in=4.0,
        generated_at="2026-05-01 00:00:00 UTC",
    )

    flag_ids = {flag["id"] for flag in result["flags"]}
    assert result["summary"]["assessment"] == "insufficient_station_evidence"
    assert result["summary"]["missing_observation_count"] == 1
    assert "no-valid-observations" in flag_ids
    assert result["stations"][0]["missing_reason"] == "No daily PRCP value"


def test_station_precip_qaqc_flags_grid_disagreement():
    result = precip_qaqc.compare_station_precipitation(
        stations=[
            {"station_id": "GHCND:A", "observed_depth_in": 7.0},
            {"station_id": "GHCND:B", "observed_depth_in": 6.4},
        ],
        event_start="2024-07-14T00:00",
        event_end="2024-07-15T00:00",
        gridded_source="AORC",
        gridded_depth_in=4.0,
        generated_at="2026-05-01 00:00:00 UTC",
    )

    assert result["summary"]["assessment"] == "conflicts_with_grid"
    assert result["summary"]["station_to_grid_ratio_median"] == 1.675
    assert "station-grid-disagreement" in {flag["id"] for flag in result["flags"]}
