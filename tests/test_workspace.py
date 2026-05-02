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
