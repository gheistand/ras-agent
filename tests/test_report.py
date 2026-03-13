"""
test_report.py — Tests for pipeline/report.py

All tests pass without HEC-RAS installed.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

from hydrograph import HydrographSet, HydrographResult
from orchestrator import OrchestratorResult, TerrainResult
from streamstats import PeakFlowEstimates
import report as _report


# ── Helper ────────────────────────────────────────────────────────────────────

def make_mock_result(tmp_path: Path) -> OrchestratorResult:
    """Build a minimal OrchestratorResult with enough fields to generate a report."""
    dem = tmp_path / "terrain" / "dem_mosaic.tif"
    dem.parent.mkdir(parents=True, exist_ok=True)
    dem.write_bytes(b"\x00" * 100)

    chars = SimpleNamespace(
        drainage_area_km2=129.5,
        drainage_area_mi2=50.0,
        mean_elevation_m=180.5,
        relief_m=42.3,
        main_channel_length_km=20.0,
        main_channel_slope_m_per_m=0.003,
        centroid_lat=40.510,
        centroid_lon=-89.502,
        pour_point_lat=40.5,
        pour_point_lon=-89.6,
    )
    ws_result = SimpleNamespace(characteristics=chars)

    peak_flows = PeakFlowEstimates(
        pour_point_lon=-89.6,
        pour_point_lat=40.5,
        drainage_area_mi2=50.0,
        source="regression_central",
        workspace_id=None,
        Q2=490.0,
        Q5=860.0,
        Q10=1150.0,
        Q25=1600.0,
        Q50=2050.0,
        Q100=2530.0,
        Q500=3800.0,
    )

    times = np.linspace(0, 24, 97)
    flows_base = np.maximum(np.sin(np.linspace(0, np.pi, 97)), 0)
    hydros = {}
    for rp, qp in [(10, 1150.0), (100, 2530.0)]:
        hydros[rp] = HydrographResult(
            return_period_yr=rp,
            peak_flow_cfs=qp,
            time_to_peak_hr=4.0,
            duration_hr=24.0,
            time_step_hr=0.25,
            times_hr=times,
            flows_cfs=flows_base * qp + 10.0,
            baseflow_cfs=10.0,
            source="NRCS_DUH",
        )
    hydro_set = HydrographSet(
        watershed_area_mi2=50.0,
        time_of_concentration_hr=3.5,
        hydrographs=hydros,
    )

    return OrchestratorResult(
        name="test_watershed",
        pour_point=(-89.6, 40.5),
        output_dir=tmp_path,
        terrain=TerrainResult(dem_path=dem),
        watershed=ws_result,
        peak_flows=peak_flows,
        hydro_set=hydro_set,
        project=None,
        job_ids=[],
        results={},
        duration_sec=17.3,
        status="complete",
        errors=[],
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_generate_report_creates_file(tmp_path):
    """generate_report() creates an HTML file at the expected path."""
    result = make_mock_result(tmp_path)
    path = _report.generate_report(result, include_plots=False)
    assert path.exists()
    assert path.suffix == ".html"


def test_report_is_valid_html(tmp_path):
    """Output file contains expected HTML structure markers."""
    result = make_mock_result(tmp_path)
    path = _report.generate_report(result, include_plots=False)
    html = path.read_text(encoding="utf-8")
    assert "<html" in html
    assert "<table" in html
    assert "<h1" in html


def test_report_contains_watershed_name(tmp_path):
    """Watershed name appears in the generated HTML."""
    result = make_mock_result(tmp_path)
    path = _report.generate_report(result, include_plots=False)
    html = path.read_text(encoding="utf-8")
    assert "test_watershed" in html


def test_report_contains_peak_flows(tmp_path):
    """Q100 value appears in the generated HTML."""
    result = make_mock_result(tmp_path)
    path = _report.generate_report(result, include_plots=False)
    html = path.read_text(encoding="utf-8")
    # Q100 = 2530.0 → formatted as "2,530 cfs"
    assert "2,530" in html


def test_report_self_contained(tmp_path):
    """Report has no external file references (no http URLs in src/href attributes)."""
    result = make_mock_result(tmp_path)
    path = _report.generate_report(result, include_plots=True)
    html = path.read_text(encoding="utf-8")
    assert 'src="http' not in html
    assert "src='http" not in html
    assert 'href="http' not in html
    assert "href='http" not in html


def test_report_without_plots(tmp_path):
    """generate_report(include_plots=False) still creates valid HTML without PNG data."""
    result = make_mock_result(tmp_path)
    path = _report.generate_report(result, include_plots=False)
    html = path.read_text(encoding="utf-8")
    assert "<html" in html
    assert "test_watershed" in html
    assert "data:image/png;base64" not in html
