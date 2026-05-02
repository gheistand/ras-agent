"""
test_batch.py — Tests for pipeline/batch.py

All tests pass without HEC-RAS installed.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import batch as _batch
from batch import (
    BatchResult,
    WatershedSpec,
    load_watershed_specs,
    run_batch,
    write_summary_csv,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _make_mock_orchestrator_result(name: str, lon: float, lat: float, ws_dir: Path):
    """Return a MagicMock shaped like OrchestratorResult."""
    ws_dir.mkdir(parents=True, exist_ok=True)
    mock = MagicMock()
    mock.name = name
    mock.pour_point = (lon, lat)
    mock.status = "complete"
    mock.duration_sec = 1.5
    mock.output_dir = ws_dir
    mock.watershed = None
    mock.peak_flows = None
    mock.results = {}
    mock.errors = []
    mock.project = None
    mock.water_source = {
        "mode": "mock_screening",
        "contract_status": "screening_only",
        "production_ready": False,
    }
    return mock


def _run_watershed_factory(output_dir: Path):
    """Returns a side_effect function for patching run_watershed."""
    def _side_effect(pour_point_lon, pour_point_lat, output_dir=output_dir,
                     name=None, **kwargs):
        ws_dir = Path(output_dir)
        return _make_mock_orchestrator_result(name, pour_point_lon, pour_point_lat,
                                              ws_dir)
    return _side_effect


# ── Tests: load_watershed_specs ────────────────────────────────────────────────

def test_load_watershed_specs_csv(tmp_path):
    """Load a CSV file and verify WatershedSpec fields."""
    csv_file = tmp_path / "watersheds.csv"
    _write_csv(
        csv_file,
        rows=[
            {"name": "Alpha", "lon": "-88.578", "lat": "40.021",
             "return_periods": "10,50,100", "notes": "test area"},
            {"name": "Beta",  "lon": "-87.944", "lat": "40.034",
             "return_periods": "100", "notes": ""},
        ],
        fieldnames=["name", "lon", "lat", "return_periods", "notes"],
    )

    specs = load_watershed_specs(csv_file)

    assert len(specs) == 2
    alpha = specs[0]
    assert alpha.name == "Alpha"
    assert alpha.lon == pytest.approx(-88.578)
    assert alpha.lat == pytest.approx(40.021)
    assert alpha.return_periods == [10, 50, 100]
    assert alpha.notes == "test area"

    beta = specs[1]
    assert beta.name == "Beta"
    assert beta.return_periods == [100]
    assert beta.notes == ""


def test_load_watershed_specs_json(tmp_path):
    """Load a JSON file and verify WatershedSpec fields."""
    json_file = tmp_path / "watersheds.json"
    json_file.write_text(json.dumps([
        {"name": "Sangamon_Monticello", "lon": -88.578, "lat": 40.021,
         "return_periods": [10, 50, 100], "notes": "FEMA restudy"},
        {"name": "Salt_Fork_Homer", "lon": -87.944, "lat": 40.034,
         "return_periods": [100]},
    ]))

    specs = load_watershed_specs(json_file)

    assert len(specs) == 2
    s = specs[0]
    assert s.name == "Sangamon_Monticello"
    assert s.lon == pytest.approx(-88.578)
    assert s.lat == pytest.approx(40.021)
    assert s.return_periods == [10, 50, 100]
    assert s.notes == "FEMA restudy"

    s2 = specs[1]
    assert s2.return_periods == [100]
    assert s2.notes == ""


def test_load_watershed_specs_return_periods_string(tmp_path):
    """CSV with return_periods as string '10,50,100' parses to list[int]."""
    csv_file = tmp_path / "ws.csv"
    _write_csv(
        csv_file,
        rows=[{"name": "X", "lon": "-89.0", "lat": "40.0",
               "return_periods": "10,50,100", "notes": ""}],
        fieldnames=["name", "lon", "lat", "return_periods", "notes"],
    )

    specs = load_watershed_specs(csv_file)

    assert len(specs) == 1
    assert specs[0].return_periods == [10, 50, 100]
    assert all(isinstance(rp, int) for rp in specs[0].return_periods)


# ── Tests: run_batch ───────────────────────────────────────────────────────────

def test_run_batch_mock_mode(tmp_path):
    """Patch run_watershed to return mock results; BatchResult.completed == n_watersheds."""
    csv_file = tmp_path / "ws.csv"
    _write_csv(
        csv_file,
        rows=[
            {"name": "Alpha", "lon": "-88.578", "lat": "40.021",
             "return_periods": "100", "notes": ""},
            {"name": "Beta",  "lon": "-87.944", "lat": "40.034",
             "return_periods": "100", "notes": ""},
            {"name": "Gamma", "lon": "-89.099", "lat": "38.961",
             "return_periods": "100", "notes": ""},
        ],
        fieldnames=["name", "lon", "lat", "return_periods", "notes"],
    )
    output_dir = tmp_path / "out"

    def _fake_run_watershed(pour_point_lon, pour_point_lat, output_dir,
                             name=None, **kwargs):
        ws_dir = Path(output_dir)
        return _make_mock_orchestrator_result(name, pour_point_lon,
                                              pour_point_lat, ws_dir)

    with patch("batch.run_watershed", side_effect=_fake_run_watershed):
        result = run_batch(csv_file, output_dir, max_workers=2)

    assert result.total == 3
    assert result.completed == 3
    assert result.failed == 0
    assert result.skipped == 0
    assert len(result.results) == 3
    assert result.errors == {}
    assert result.summary_csv.exists()


def test_run_batch_resume_skips_completed(tmp_path):
    """Pre-existing run_metadata.json with status='complete' counts as skipped."""
    csv_file = tmp_path / "ws.csv"
    _write_csv(
        csv_file,
        rows=[
            {"name": "Alpha", "lon": "-88.578", "lat": "40.021",
             "return_periods": "100", "notes": ""},
            {"name": "Beta",  "lon": "-87.944", "lat": "40.034",
             "return_periods": "100", "notes": ""},
        ],
        fieldnames=["name", "lon", "lat", "return_periods", "notes"],
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Pre-create completed metadata for Alpha
    alpha_dir = output_dir / "Alpha"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    (alpha_dir / "run_metadata.json").write_text(
        json.dumps({"name": "Alpha", "status": "complete"})
    )

    def _fake_run_watershed(pour_point_lon, pour_point_lat, output_dir,
                             name=None, **kwargs):
        ws_dir = Path(output_dir)
        return _make_mock_orchestrator_result(name, pour_point_lon,
                                              pour_point_lat, ws_dir)

    with patch("batch.run_watershed", side_effect=_fake_run_watershed) as mock_rw:
        result = run_batch(csv_file, output_dir, max_workers=2, resume=True)

    assert result.total == 2
    assert result.skipped == 1
    assert result.completed == 1
    assert result.failed == 0
    # run_watershed should only be called for Beta, not Alpha
    assert mock_rw.call_count == 1
    called_name = mock_rw.call_args.kwargs.get("name") or mock_rw.call_args[1].get("name")
    assert called_name == "Beta"


def test_run_batch_one_failure_continues(tmp_path):
    """One watershed failure should not stop the batch; failed=1, completed=rest."""
    csv_file = tmp_path / "ws.csv"
    _write_csv(
        csv_file,
        rows=[
            {"name": "Alpha", "lon": "-88.578", "lat": "40.021",
             "return_periods": "100", "notes": ""},
            {"name": "Beta",  "lon": "-87.944", "lat": "40.034",
             "return_periods": "100", "notes": ""},
            {"name": "Gamma", "lon": "-89.099", "lat": "38.961",
             "return_periods": "100", "notes": ""},
        ],
        fieldnames=["name", "lon", "lat", "return_periods", "notes"],
    )
    output_dir = tmp_path / "out"

    call_count = {"n": 0}

    def _fake_run_watershed(pour_point_lon, pour_point_lat, output_dir,
                             name=None, **kwargs):
        call_count["n"] += 1
        if name == "Beta":
            raise RuntimeError("simulated terrain failure")
        ws_dir = Path(output_dir)
        return _make_mock_orchestrator_result(name, pour_point_lon,
                                              pour_point_lat, ws_dir)

    with patch("batch.run_watershed", side_effect=_fake_run_watershed):
        result = run_batch(csv_file, output_dir, max_workers=1)

    assert result.total == 3
    assert result.completed == 2
    assert result.failed == 1
    assert "Beta" in result.errors
    assert "simulated terrain failure" in result.errors["Beta"]


def test_run_batch_forwards_boundary_condition_mode(tmp_path):
    csv_file = tmp_path / "ws.csv"
    _write_csv(
        csv_file,
        rows=[
            {"name": "Alpha", "lon": "-88.578", "lat": "40.021",
             "return_periods": "100", "notes": ""},
        ],
        fieldnames=["name", "lon", "lat", "return_periods", "notes"],
    )
    output_dir = tmp_path / "out"

    def _fake_run_watershed(pour_point_lon, pour_point_lat, output_dir,
                             name=None, **kwargs):
        ws_dir = Path(output_dir)
        return _make_mock_orchestrator_result(name, pour_point_lon,
                                              pour_point_lat, ws_dir)

    with patch("batch.run_watershed", side_effect=_fake_run_watershed) as mock_rw:
        result = run_batch(
            csv_file,
            output_dir,
            max_workers=1,
            boundary_condition_mode="downstream",
        )

    assert result.completed == 1
    assert mock_rw.call_args.kwargs["boundary_condition_mode"] == "downstream"
    run_metadata = json.loads((output_dir / "Alpha" / "run_metadata.json").read_text())
    assert run_metadata["boundary_condition_mode"] == "downstream"
    assert run_metadata["water_source"]["mode"] == "mock_screening"


# ── Tests: write_summary_csv ───────────────────────────────────────────────────

def test_write_summary_csv(tmp_path):
    """Output CSV has correct headers and one row per completed+failed watershed."""
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    # Build two mock completed results
    ws_dir_a = output_dir / "Alpha"
    ws_dir_b = output_dir / "Beta"
    result_a = _make_mock_orchestrator_result("Alpha", -88.578, 40.021, ws_dir_a)
    result_b = _make_mock_orchestrator_result("Beta", -87.944, 40.034, ws_dir_b)

    batch_result = BatchResult(
        input_file=tmp_path / "ws.csv",
        output_dir=output_dir,
        total=3,
        completed=2,
        failed=1,
        skipped=0,
        results=[result_a, result_b],
        errors={"Gamma": "terrain download failed"},
        duration_sec=10.0,
        summary_csv=output_dir / "batch_summary.csv",
    )

    csv_path = output_dir / "summary.csv"
    returned = write_summary_csv(batch_result, csv_path)

    assert returned == csv_path
    assert csv_path.exists()

    with csv_path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    expected_headers = {
        "name", "lon", "lat", "status", "duration_sec",
        "drainage_area_mi2", "q100_cfs", "flood_extent_km2", "error_msg",
    }
    assert expected_headers == set(reader.fieldnames)
    # 2 completed + 1 failed
    assert len(rows) == 3

    names = {r["name"] for r in rows}
    assert names == {"Alpha", "Beta", "Gamma"}

    gamma_row = next(r for r in rows if r["name"] == "Gamma")
    assert gamma_row["status"] == "failed"
    assert "terrain download failed" in gamma_row["error_msg"]


# ── Tests: dry_run ─────────────────────────────────────────────────────────────

def test_dry_run_no_execution(tmp_path):
    """dry_run=True should not call run_watershed at all."""
    csv_file = tmp_path / "ws.csv"
    _write_csv(
        csv_file,
        rows=[
            {"name": "Alpha", "lon": "-88.578", "lat": "40.021",
             "return_periods": "100", "notes": ""},
            {"name": "Beta",  "lon": "-87.944", "lat": "40.034",
             "return_periods": "10,50,100", "notes": ""},
        ],
        fieldnames=["name", "lon", "lat", "return_periods", "notes"],
    )
    output_dir = tmp_path / "out"

    with patch("batch.run_watershed") as mock_rw:
        result = run_batch(csv_file, output_dir, dry_run=True)

    mock_rw.assert_not_called()
    assert result.total == 2
    assert result.completed == 0
    assert result.failed == 0
    assert result.skipped == 0
    assert result.results == []
    assert result.errors == {}
