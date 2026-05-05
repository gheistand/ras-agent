"""
Tests for pipeline/calibration_report.py.

All tests use mocked or inline modeled data and do not require HEC-RAS.
"""

import math
import os
import sys
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import calibration_report


pytest.importorskip("bokeh")


class _ExternalReferenceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.external_refs = []

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        if tag == "script" and str(attr_map.get("src", "")).startswith(("http://", "https://")):
            self.external_refs.append(("script", attr_map["src"]))
        if tag == "link" and str(attr_map.get("href", "")).startswith(("http://", "https://")):
            self.external_refs.append(("link", attr_map["href"]))


def _series(values, start="2026-04-01 00:00", freq="h"):
    return pd.Series(
        values,
        index=pd.date_range(start, periods=len(values), freq=freq),
    )


def test_calculate_stats_normalizes_stage_rmse_by_mean():
    observed = _series([4.0, 4.0, 4.0, 4.0, 10.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0])
    modeled = observed + 1.0

    stats = calibration_report.calculate_stats(observed, modeled, variable="stage")

    assert stats["rmse"] == pytest.approx(1.0)
    expected_mean = np.mean(observed.values)
    assert stats["rmse_pct"] == pytest.approx(100.0 / expected_mean)


def test_calculate_stats_normalizes_flow_rmse_by_peak():
    observed = _series(np.linspace(20.0, 100.0, 12))
    modeled = observed + 10.0

    stats = calibration_report.calculate_stats(observed, modeled, variable="flow")

    assert stats["rmse"] == pytest.approx(10.0)
    assert stats["rmse_pct"] == pytest.approx(10.0)


def test_calculate_stats_perfect_fit():
    observed = _series(np.linspace(9.5, 10.5, 24))
    stats = calibration_report.calculate_stats(observed, observed.copy(), variable="stage")

    assert stats["rmse"] == pytest.approx(0.0, abs=1e-12)
    assert stats["rmse_pct"] == pytest.approx(0.0, abs=1e-9)
    assert stats["pbias"] == pytest.approx(0.0, abs=1e-9)
    assert stats["nse"] == pytest.approx(1.0)
    assert stats["pearson_r"] == pytest.approx(1.0)
    assert stats["kge"] == pytest.approx(1.0)


def test_calculate_stats_known_pbias():
    observed = _series([100.0] * 10)
    modeled = _series([110.0] * 10)
    stats = calibration_report.calculate_stats(observed, modeled, variable="flow")

    assert stats["pbias"] == pytest.approx(10.0)


def test_calculate_stats_constant_offset_has_r_1():
    observed = _series(np.linspace(5.0, 15.0, 24))
    modeled = observed + 2.0
    stats = calibration_report.calculate_stats(observed, modeled, variable="stage")

    assert stats["pearson_r"] == pytest.approx(1.0)


def test_calculate_stats_raises_on_non_overlapping():
    obs = _series([1.0, 2.0, 3.0], start="2026-01-01")
    mod = _series([1.0, 2.0, 3.0], start="2026-06-01")
    with pytest.raises(ValueError, match="no overlapping"):
        calibration_report.calculate_stats(obs, mod)


def test_calculate_stats_single_point_returns_nan_for_correlation():
    obs = _series([5.0])
    mod = _series([5.5])
    stats = calibration_report.calculate_stats(obs, mod)

    assert not math.isfinite(stats["pearson_r"])
    assert not math.isfinite(stats["nse"])


def test_calculate_stats_constant_observed_returns_nan_kge():
    obs = _series([10.0] * 12)
    mod = _series([11.0] * 12)
    stats = calibration_report.calculate_stats(obs, mod, variable="stage")

    assert not math.isfinite(stats["kge"])


def test_generate_calibration_report_self_contained_with_bokeh(tmp_path):
    observed = _series(10.0 + np.sin(np.linspace(0, np.pi, 24)) * 3.0)
    good_modeled = observed * 1.02
    bad_modeled = observed * 0.55
    output_path = tmp_path / "calibration_report.html"

    path = calibration_report.generate_calibration_report(
        plan_hdfs=[],
        observed_data={
            "Spring Creek Stage": {
                "observed": observed,
                "modeled": good_modeled,
                "variable": "stage",
                "units": "ft",
            },
            "Spring Creek Flow": {
                "observed": observed * 100.0,
                "modeled": bad_modeled * 100.0,
                "variable": "flow",
                "units": "cfs",
            },
        },
        output_path=output_path,
    )

    html = path.read_text(encoding="utf-8")
    assert path == output_path
    assert "<html" in html
    assert "Bokeh" in html
    assert "Global Summary" in html
    assert "Gauge Statistics" in html
    assert "KGE" in html
    assert "metric-pass" in html
    assert "metric-fail" in html
    parser = _ExternalReferenceParser()
    parser.feed(html)
    assert parser.external_refs == []


def test_generate_calibration_report_uses_mocked_hdf_extraction(tmp_path, monkeypatch):
    observed = _series(5.0 + np.sin(np.linspace(0, np.pi, 24)))
    modeled = observed * 0.98
    plan_hdf = tmp_path / "mock.p01.hdf"
    calls = []

    def fake_extract(raw_plan_hdf: Path, gauge):
        calls.append((Path(raw_plan_hdf), gauge.name, gauge.x, gauge.y, gauge.variable))
        return modeled

    monkeypatch.setattr(calibration_report, "_extract_modeled_from_plan", fake_extract)

    path = calibration_report.generate_calibration_report(
        plan_hdfs={"Mock Plan": plan_hdf},
        observed_data=[
            {
                "name": "Mock Gauge",
                "observed": observed,
                "variable": "stage",
                "units": "ft",
                "x": 500000.0,
                "y": 4400000.0,
            }
        ],
        output_path=tmp_path / "mock_report.html",
    )

    html = path.read_text(encoding="utf-8")
    assert path.exists()
    assert calls == [(plan_hdf, "Mock Gauge", 500000.0, 4400000.0, "stage")]
    assert "Mock Gauge" in html
    assert "Mock Plan" in html


def test_generate_report_with_project_context(tmp_path):
    folium = pytest.importorskip("folium")
    observed = _series(10.0 + np.sin(np.linspace(0, np.pi, 24)) * 3.0)
    modeled = observed * 1.02

    ctx = calibration_report.ProjectContext(
        title="Test Calibration Project",
        description="A test project for verifying ProjectContext rendering.",
        data_sources=[
            {"name": "Test Observed", "type": "Observed", "source": "Unit Test"},
            {"name": "Test Modeled", "type": "Modeled", "source": "Synthetic"},
        ],
        boundary_conditions=[
            {"name": "Upstream", "location": "RM 100", "type": "Flow Hydrograph"},
        ],
        gauge_locations=[
            {"name": "Test Gauge", "lat": 37.0, "lon": -89.0},
        ],
    )

    path = calibration_report.generate_calibration_report(
        plan_hdfs=[],
        observed_data={
            "Test Gauge": {
                "observed": observed,
                "modeled": modeled,
                "variable": "stage",
                "units": "ft",
            },
        },
        output_path=tmp_path / "context_report.html",
        project_context=ctx,
    )

    html = path.read_text(encoding="utf-8")
    assert "Test Calibration Project" in html
    assert "Data Sources" in html
    assert "Boundary Conditions" in html
    assert "srcdoc" in html
    assert "leaflet" in html.lower() or "L.map" in html
    assert "Test Gauge" in html
    assert "Unit Test" in html
    assert "Upstream" in html
