"""
Tests for workspace report package generation and validation helpers.
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import report


def _fake_workspace_context(tmp_path: Path) -> dict:
    return {
        "workspace_dir": tmp_path,
        "manifest": {
            "downloads": {
                "terrain_clipped": str(tmp_path / "04_terrain" / "dem.tif"),
                "analysis_extent_summary": str(tmp_path / "00_metadata" / "analysis_extent_summary.json"),
            },
            "sources": {
                "terrain_primary_image_server": "https://example.test/ImageServer",
                "terrain_fallback": "https://example.test/fallback",
                "nlcd_wcs": "https://example.test/nlcd",
                "soils_wfs": "https://example.test/soils",
            },
            "notes": {
                "streamstats_status": "Legacy StreamStats endpoint returned 404 during workspace preparation."
            },
        },
        "gauge_summary": {
            "continuous_last365d": [{"record_count": 10}],
            "daily_period_of_record": [{"record_count": 20}],
        },
        "huc_summary": {
            "gauge_huc12": [{"huc12": "071300080203", "huc12_name": "Archer Creek-Spring Creek", "states": "IL"}],
            "intersecting_huc12_count": 1,
            "intersecting_huc12": [{"huc12": "071300080203", "huc12_name": "Archer Creek-Spring Creek", "states": "IL"}],
        },
        "gauge_feature": {
            "features": [{
                "properties": {
                    "name": "SPRING CREEK AT SPRINGFIELD, IL",
                    "uri": "https://example.test/gauge",
                    "comid": "12345",
                    "reachcode": "67890",
                }
            }]
        },
        "peaks": pd.DataFrame({"peak_dt": pd.to_datetime(["2024-01-01"]), "peak_va": [1500.0]}),
        "analysis_extent_summary": {
            "buffer_m": 500.0,
            "bbox_wgs84": [-89.81, 39.69, -89.76, 39.72],
            "bbox_5070": [500000.0, 1860000.0, 501000.0, 1861000.0],
            "source_boundary": str(tmp_path / "02_basin_outline" / "USGS_05577500_nldi_basin_5070.geojson"),
        },
        "analysis_extent_path": tmp_path / "00_metadata" / "analysis_extent.geojson",
        "analysis_extent_5070_path": tmp_path / "00_metadata" / "analysis_extent_5070.geojson",
        "flowlines_path": tmp_path / "03_nhdplus" / "USGS_05577500_upstream_flowlines_analysis_extent.geojson",
        "soils_path": tmp_path / "06_soils" / "ssurgo_mapunitpoly_analysis_extent.geojson",
        "soils_5070_path": tmp_path / "06_soils" / "ssurgo_mapunitpoly_analysis_extent_5070.geojson",
        "dem_path": tmp_path / "04_terrain" / "spring_creek_basin_dem_5070.tif",
        "nlcd_path": tmp_path / "05_landcover_nlcd" / "nlcd_2021_analysis_extent.tif",
        "precip_station_qaqc_path": None,
        "precip_station_qaqc": None,
    }


def _fake_precip_qaqc() -> dict:
    return {
        "schema_version": "station-precip-qaqc/v1",
        "generated_at": "2026-05-01 00:00:00 UTC",
        "event": {
            "start_time": "2024-07-14T00:00",
            "end_time": "2024-07-15T00:00",
            "accumulation_window": "24 hours",
        },
        "grid": {"source": "AORC", "depth_in": 4.0, "depth_units": "in"},
        "station_network": {
            "network": "GHCND",
            "provider": "NOAA NCEI",
            "noaa_token_available": True,
            "search_radius_mi": 25.0,
            "nearby_station_count": 2,
            "station_count": 2,
            "valid_observation_count": 2,
            "missing_observation_count": 0,
        },
        "summary": {
            "assessment": "supports_grid",
            "station_count": 2,
            "nearby_station_count": 2,
            "valid_observation_count": 2,
            "missing_observation_count": 0,
            "ratio_count": 2,
            "observed_depth_median_in": 4.2,
            "gridded_depth_mean_in": 4.0,
            "station_to_grid_ratio_median": 1.05,
            "missing_data_conditions": [],
        },
        "flags": [],
        "stations": [
            {
                "station_id": "GHCND:USC00110072",
                "station_name": "Springfield 2 N",
                "distance_mi": 4.2,
                "observed_depth_in": 4.1,
                "gridded_depth_in": 4.0,
                "station_to_grid_ratio": 1.025,
                "status": "ok",
                "missing": False,
                "missing_reason": None,
                "flags": [],
            },
            {
                "station_id": "GHCND:USC00117657",
                "station_name": "Rochester",
                "distance_mi": 9.8,
                "observed_depth_in": 4.3,
                "gridded_depth_in": 4.0,
                "station_to_grid_ratio": 1.075,
                "status": "ok",
                "missing": False,
                "missing_reason": None,
                "flags": [],
            },
        ],
        "artifacts": {
            "station_qaqc_json": "station_precip_qaqc.json",
            "station_table_csv": "station_precip_qaqc_stations.csv",
            "figure_png": None,
        },
    }


def test_validate_workspace_reports_missing_artifacts(tmp_path):
    validation = report.validate_workspace(tmp_path)
    assert validation["status"] == "partial"
    assert validation["missing_required_artifacts"]


def test_write_workspace_report_package_writes_json_outputs(tmp_path, monkeypatch):
    def _fake_generate(workspace_dir, output_path=None, include_map=True):
        output_path = Path(output_path or (Path(workspace_dir) / "08_report" / "report.html"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("<html><body>stub</body></html>", encoding="utf-8")
        return output_path

    monkeypatch.setattr(report, "generate_workspace_report", _fake_generate)
    monkeypatch.setattr(report, "_load_workspace_context", lambda workspace_dir: _fake_workspace_context(tmp_path))
    monkeypatch.setattr(report, "validate_workspace", lambda workspace_dir: {
        "status": "partial",
        "workspace_dir": str(workspace_dir),
        "required_file_count": 23,
        "missing_required_artifacts": [],
        "present_optional_artifacts": {},
    })

    outputs = report.write_workspace_report_package(
        tmp_path,
        issue_urls={
            "ras_agent_streamstats": "https://github.com/gpt-cmdr/ras-agent/issues/1",
            "hms_commander_gauge_study": "https://github.com/gpt-cmdr/hms-commander/issues/1",
            "ras_commander_drainage_area": "https://github.com/gpt-cmdr/ras-commander/issues/1",
            "ras_commander_geometry_builder": "https://github.com/gpt-cmdr/ras-commander/issues/2",
        },
    )

    assert outputs["report_html"].exists()
    assert outputs["report_json"].exists()
    assert outputs["data_gap_analysis"].exists()

    report_json = json.loads(outputs["report_json"].read_text(encoding="utf-8"))
    gap_json = json.loads(outputs["data_gap_analysis"].read_text(encoding="utf-8"))

    assert report_json["schema_version"] == "base-engineering-report/v1"
    assert report_json["data_gaps"]["count"] == gap_json["gap_count"]
    assert report_json["analysis_extent"]["buffer_m"] == 500.0
    assert report_json["landcover"]["nlcd_path"].endswith("nlcd_2021_analysis_extent.tif")
    assert report_json["soils"]["soil_geojson"].endswith("ssurgo_mapunitpoly_analysis_extent.geojson")
    assert report_json["precipitation_qaqc"]["status"] == "missing"
    assert any(gap["id"] == "streamstats-service-transition" for gap in gap_json["gaps"])
    assert any(gap["id"] == "station-precip-qaqc-pending" for gap in gap_json["gaps"])
    assert report_json["water_source"]["contract_status"] == "not_recorded"
    assert any(gap["id"] == "headwater-water-source-contract-not-production-ready" for gap in gap_json["gaps"])
    assert any(gap["issue_url"] for gap in gap_json["gaps"])


def test_workspace_report_json_includes_station_precip_qaqc(tmp_path):
    ctx = _fake_workspace_context(tmp_path)
    qaqc_path = tmp_path / "08_report" / "station_precip_qaqc.json"
    ctx["precip_station_qaqc_path"] = qaqc_path
    ctx["precip_station_qaqc"] = _fake_precip_qaqc()
    validation = {
        "status": "complete",
        "missing_required_artifacts": [],
        "present_optional_artifacts": {
            "taudem_verification": "present",
            "drainage_area_comparison": "present",
            "model_handoff": "present",
            "precip_station_qaqc": str(qaqc_path),
        },
    }

    gap_analysis = report.build_workspace_gap_analysis(ctx, validation=validation)
    report_json = report.build_workspace_report_json(
        ctx,
        report_html_path=tmp_path / "08_report" / "report.html",
        gap_analysis=gap_analysis,
        validation=validation,
    )

    precip_section = report_json["precipitation_qaqc"]
    assert precip_section["status"] == "present"
    assert precip_section["summary"]["station_count"] == 2
    assert precip_section["station_comparisons"][0]["observed_depth_in"] == 4.1
    assert "station_precip_qaqc" in report_json["figures"]
    assert "station-precip-qaqc-pending" not in {
        gap["id"] for gap in gap_analysis["gaps"]
    }


def test_workspace_gap_analysis_records_precip_qaqc_data_gap_flags(tmp_path):
    ctx = _fake_workspace_context(tmp_path)
    qaqc = _fake_precip_qaqc()
    qaqc["flags"] = [
        {
            "id": "no-noaa-token",
            "category": "service",
            "severity": "high",
            "message": "NOAA token was not available.",
            "blocking_for": "model-readiness",
        },
        {
            "id": "no-nearby-stations",
            "category": "data",
            "severity": "medium",
            "message": "No nearby stations were found.",
            "blocking_for": "model-readiness",
        },
    ]
    qaqc["summary"]["assessment"] = "insufficient_station_evidence"
    ctx["precip_station_qaqc_path"] = tmp_path / "08_report" / "station_precip_qaqc.json"
    ctx["precip_station_qaqc"] = qaqc
    validation = {
        "status": "complete",
        "missing_required_artifacts": [],
        "present_optional_artifacts": {
            "taudem_verification": "present",
            "drainage_area_comparison": "present",
            "model_handoff": "present",
            "precip_station_qaqc": str(ctx["precip_station_qaqc_path"]),
        },
    }

    gap_analysis = report.build_workspace_gap_analysis(ctx, validation=validation)
    gap_ids = {gap["id"] for gap in gap_analysis["gaps"]}

    assert "station-precip-qaqc-no-noaa-token" in gap_ids
    assert "station-precip-qaqc-no-nearby-stations" in gap_ids


def test_workspace_precip_qaqc_html_section_contains_flags_and_table(tmp_path):
    ctx = _fake_workspace_context(tmp_path)
    ctx["precip_station_qaqc_path"] = tmp_path / "08_report" / "station_precip_qaqc.json"
    ctx["precip_station_qaqc"] = _fake_precip_qaqc()
    ctx["precip_station_qaqc"]["flags"] = [{
        "id": "station-grid-disagreement",
        "severity": "high",
        "message": "Median ratio outside review band.",
    }]

    html = report._section_workspace_precip_qaqc(ctx)

    assert "Station Precipitation QAQC" in html
    assert "GHCND:USC00110072" in html
    assert "station-grid-disagreement" in html
