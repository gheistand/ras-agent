"""
Tests for conservative LiDAR pilot-channel proposal generation.
"""

import csv
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from shapely.geometry import LineString, box

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import pilot_channel as pc


def _write_dem(path: Path, data: np.ndarray) -> Path:
    height, width = data.shape
    transform = from_bounds(0.0, 0.0, 100.0, 100.0, width, height)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs=CRS.from_epsg(5070),
        transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(data.astype("float32"), 1)
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _watershed(dem_path: Path, centerlines: gpd.GeoDataFrame) -> SimpleNamespace:
    basin = gpd.GeoDataFrame(
        {"name": ["test"]},
        geometry=[box(0.0, 0.0, 100.0, 100.0)],
        crs="EPSG:5070",
    )
    return SimpleNamespace(
        basin=basin,
        streams=centerlines.copy(),
        centerlines=centerlines,
        artifacts={},
        dem_clipped=dem_path,
    )


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as file_obj:
        return list(csv.DictReader(file_obj))


def test_flat_profile_writes_proposal_without_mutating_dem(tmp_path):
    dem_path = _write_dem(tmp_path / "flat_dem.tif", np.full((11, 11), 100.0))
    before_hash = _sha256(dem_path)
    line = LineString([(50.0, 95.0), (50.0, 5.0)])
    centerlines = gpd.GeoDataFrame(
        {
            "stream_id": [7],
            "strmOrder": [2],
            "drainage_area_km2": [4.0],
        },
        geometry=[line],
        crs="EPSG:5070",
    )

    result = pc.build_pilot_channel_proposals(
        _watershed(dem_path, centerlines),
        tmp_path / "proposal",
        config=pc.PilotChannelConfig(write_figures=False, sample_spacing_m=20.0),
    )

    assert _sha256(dem_path) == before_hash
    assert result.production_terrain_mutated is False
    assert result.hitl_required is True
    assert result.proposed_segment_count == 1
    assert result.proposal_json.exists()
    assert result.profile_csv.exists()
    assert result.segment_summary_csv.exists()
    assert result.reviewer_flags_csv.exists()
    assert result.report_html.exists()

    payload = json.loads(result.proposal_json.read_text(encoding="utf-8"))
    assert payload["production_terrain_mutated"] is False
    assert payload["hitl_required"] is True
    assert payload["references"]["hec_commander_lidar_terrain_mod_method_note"] == pc.HEC_COMMANDER_METHOD_NOTE_URL
    assert payload["references"]["ras_agent_roadmap_section"] == pc.ROADMAP_SECTION
    assert payload["ras_commander_handoff"]["status"].startswith("not_applied")

    summary = _read_csv(result.segment_summary_csv)
    assert summary[0]["proposal_status"] == "proposed_requires_human_review"
    assert summary[0]["positive_profile_slope_check_passed"] == "True"
    assert float(summary[0]["min_proposed_slope_m_per_m"]) > 0.0
    assert "flat_slope" in summary[0]["review_flags"]

    profiles = _read_csv(result.profile_csv)
    slopes = [
        float(row["proposed_slope_to_next_m_per_m"])
        for row in profiles
        if row["proposed_slope_to_next_m_per_m"]
    ]
    assert slopes
    assert min(slopes) > 0.0


def test_excessive_cut_flag_is_reported_when_slope_floor_forces_deep_cut(tmp_path):
    dem_path = _write_dem(tmp_path / "flat_dem.tif", np.full((11, 11), 100.0))
    line = LineString([(50.0, 95.0), (50.0, 5.0)])
    centerlines = gpd.GeoDataFrame(
        {
            "stream_id": [1],
            "strmOrder": [3],
            "drainage_area_km2": [8.0],
        },
        geometry=[line],
        crs="EPSG:5070",
    )

    result = pc.build_pilot_channel_proposals(
        _watershed(dem_path, centerlines),
        tmp_path / "proposal",
        config=pc.PilotChannelConfig(
            write_figures=False,
            sample_spacing_m=20.0,
            min_positive_slope_m_per_m=0.02,
            excessive_cut_threshold_m=0.75,
        ),
    )

    flags = _read_csv(result.reviewer_flags_csv)
    assert any(row["flag_code"] == "excessive_cut" for row in flags)
    summary = _read_csv(result.segment_summary_csv)
    assert float(summary[0]["max_cut_m"]) > 0.75


def test_abrupt_drop_flag_is_reported_from_sampled_profile(tmp_path):
    dem_data = np.full((11, 11), 100.0, dtype="float32")
    dem_data[5:, :] = 99.0
    dem_path = _write_dem(tmp_path / "drop_dem.tif", dem_data)
    line = LineString([(50.0, 95.0), (50.0, 5.0)])
    centerlines = gpd.GeoDataFrame(
        {
            "stream_id": [1],
            "strmOrder": [2],
            "drainage_area_km2": [4.0],
        },
        geometry=[line],
        crs="EPSG:5070",
    )

    result = pc.build_pilot_channel_proposals(
        _watershed(dem_path, centerlines),
        tmp_path / "proposal",
        config=pc.PilotChannelConfig(write_figures=False, sample_spacing_m=10.0),
    )

    flags = _read_csv(result.reviewer_flags_csv)
    assert any(row["flag_code"] == "abrupt_drop" for row in flags)
    summary = _read_csv(result.segment_summary_csv)
    assert summary[0]["proposal_status"] == "proposed_requires_human_review"


def test_uncertain_channel_evidence_is_flagged_when_order_and_area_are_missing(tmp_path):
    dem_data = np.linspace(110.0, 100.0, 121, dtype="float32").reshape(11, 11)
    dem_path = _write_dem(tmp_path / "sloped_dem.tif", dem_data)
    line = LineString([(50.0, 95.0), (50.0, 5.0)])
    centerlines = gpd.GeoDataFrame({"stream_id": [1]}, geometry=[line], crs="EPSG:5070")

    result = pc.build_pilot_channel_proposals(
        _watershed(dem_path, centerlines),
        tmp_path / "proposal",
        config=pc.PilotChannelConfig(write_figures=False, sample_spacing_m=20.0),
    )

    flags = _read_csv(result.reviewer_flags_csv)
    assert any(row["flag_code"] == "uncertain_channel_evidence" for row in flags)
    summary = _read_csv(result.segment_summary_csv)
    assert summary[0]["proposal_status"] in {
        "not_proposed_below_candidate_threshold",
        "not_proposed_no_low_flow_profile_issue",
        "proposed_requires_human_review",
    }
