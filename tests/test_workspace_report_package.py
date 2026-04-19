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
            "downloads": {"terrain_clipped": str(tmp_path / "04_terrain" / "dem.tif")},
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
        "dem_path": tmp_path / "04_terrain" / "spring_creek_basin_dem_5070.tif",
        "nlcd_path": tmp_path / "05_landcover_nlcd" / "nlcd_2021_watershed.tif",
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
    assert any(gap["id"] == "streamstats-service-transition" for gap in gap_json["gaps"])
    assert any(gap["issue_url"] for gap in gap_json["gaps"])
