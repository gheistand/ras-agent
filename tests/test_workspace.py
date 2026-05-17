"""
Tests for pipeline/workspace.py.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import workspace


def test_create_workspace_structure(tmp_path):
    workspace_dir = workspace.create_workspace_structure("My Study", workspace_root=tmp_path)

    assert workspace_dir.exists()
    for subdir in workspace.DEFAULT_WORKSPACE_SUBDIRS:
        assert (workspace_dir / subdir).exists()


def test_build_report_package_uses_default_issue_urls(tmp_path, monkeypatch):
    captured = {}

    def _fake_write(workspace_dir, include_map=True, issue_urls=None):
        captured["issue_urls"] = issue_urls
        return {"report_html": tmp_path / "report.html"}

    monkeypatch.setattr(workspace.report, "write_workspace_report_package", _fake_write)
    outputs = workspace.build_report_package(tmp_path)

    assert outputs["report_html"] == tmp_path / "report.html"
    assert captured["issue_urls"]["hms_commander_gauge_study"].endswith("/issues/2")
    assert captured["issue_urls"]["ras_commander_geometry_builder"].endswith("/issues/38")


def test_validate_workspace_completeness_passes_through(monkeypatch):
    monkeypatch.setattr(workspace.report, "validate_workspace", lambda workspace_dir: {"status": "partial"})
    assert workspace.validate_workspace_completeness("fake")["status"] == "partial"


def test_refresh_context_layers_delegates_to_context_helper(tmp_path, monkeypatch):
    captured = {}

    def _fake_refresh(workspace_dir, buffer_m=500.0, nlcd_year=2021):
        captured["workspace_dir"] = workspace_dir
        captured["buffer_m"] = buffer_m
        captured["nlcd_year"] = nlcd_year
        return {"analysis_extent_summary": tmp_path / "analysis_extent_summary.json"}

    monkeypatch.setattr(workspace.context_layers, "refresh_workspace_context_layers", _fake_refresh)

    result = workspace.refresh_context_layers(tmp_path, buffer_m=750.0, nlcd_year=2019)

    assert captured["workspace_dir"] == tmp_path
    assert captured["buffer_m"] == 750.0
    assert captured["nlcd_year"] == 2019
    assert result["analysis_extent_summary"] == tmp_path / "analysis_extent_summary.json"


def test_build_2d_geometry_delegates_to_spring_creek_helper(tmp_path, monkeypatch):
    captured = {}

    def _fake_build(workspace_dir, **kwargs):
        captured["workspace_dir"] = workspace_dir
        captured.update(kwargs)
        return {"artifacts": {"mesh_quality_report_json": tmp_path / "mesh_quality_report.json"}}

    monkeypatch.setattr(
        workspace.spring_creek_geometry,
        "build_spring_creek_2d_geometry",
        _fake_build,
    )

    result = workspace.build_2d_geometry(
        tmp_path,
        cell_size_m=125.0,
        major_channel_min_length_m=2500.0,
        gauge_refinement_radius_m=500.0,
        gauge_cell_size_m=60.0,
        try_generate_mesh=True,
        mesh_max_wait=120,
    )

    assert captured["workspace_dir"] == tmp_path
    assert captured["cell_size_m"] == 125.0
    assert captured["major_channel_min_length_m"] == 2500.0
    assert captured["gauge_refinement_radius_m"] == 500.0
    assert captured["gauge_cell_size_m"] == 60.0
    assert captured["try_generate_mesh"] is True
    assert captured["mesh_max_wait"] == 120
    assert result["artifacts"]["mesh_quality_report_json"].name == "mesh_quality_report.json"


def test_write_station_precip_qaqc_artifacts_delegates_to_precip_helper(tmp_path, monkeypatch):
    captured = {}

    def _fake_build(**kwargs):
        captured.update(kwargs)
        return {"artifacts": {"station_qaqc_json": tmp_path / "08_report" / "station_precip_qaqc.json"}}

    monkeypatch.setattr(workspace.precip_qaqc, "build_station_precip_qaqc", _fake_build)

    result = workspace.write_station_precip_qaqc_artifacts(
        tmp_path,
        stations=[{"station_id": "GHCND:A", "observed_depth_in": 4.0}],
        event_start="2024-07-14T00:00",
        event_end="2024-07-15T00:00",
        gridded_source="AORC",
        gridded_depth_in=3.8,
        noaa_token_available=True,
        search_radius_mi=25.0,
    )

    assert captured["output_dir"] == tmp_path / "08_report"
    assert captured["gridded_source"] == "AORC"
    assert captured["stations"][0]["station_id"] == "GHCND:A"
    assert result["artifacts"]["station_qaqc_json"].name == "station_precip_qaqc.json"


def test_gather_base_data_delegates_to_hms_builder(tmp_path, monkeypatch):
    captured = {}

    def _fake_builder(site_id=None, workspace_root=None, **kwargs):
        captured["site_id"] = site_id
        captured["workspace_root"] = workspace_root
        return {"manifest": {"site_id": site_id}, "workspace_root": workspace_root}

    monkeypatch.setattr(workspace, "_resolve_hms_gauge_study_builder", lambda: _fake_builder)

    result = workspace.gather_base_data("05577500", "Spring Creek", workspace_root=tmp_path)

    assert captured["site_id"] == "05577500"
    assert str(captured["workspace_root"]).endswith("Spring Creek")
    assert result["site_id"] == "05577500"
