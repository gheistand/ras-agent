"""
test_orchestrator.py — Tests for pipeline/orchestrator.py

All tests pass without HEC-RAS installed.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
import geopandas as gpd
from shapely.geometry import LineString, Point, box

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import orchestrator as orch
from orchestrator import (
    OrchestratorError,
    OrchestratorResult,
    TerrainResult,
    run_watershed,
)

if orch._terrain is None:
    orch._terrain = SimpleNamespace(get_terrain=lambda *args, **kwargs: None)
if orch._watershed is None:
    orch._watershed = SimpleNamespace(delineate_watershed=lambda *args, **kwargs: None)
if orch._streamstats is None:
    orch._streamstats = SimpleNamespace(get_peak_flows=lambda *args, **kwargs: None)
if orch._hydrograph is None:
    orch._hydrograph = SimpleNamespace(generate_hydrograph_set=lambda *args, **kwargs: None)
if orch._runner is None:
    orch._runner = SimpleNamespace(
        enqueue_job=lambda *args, **kwargs: None,
        run_queue=lambda *args, **kwargs: None,
        get_job=lambda *args, **kwargs: None,
    )
if orch._results is None:
    orch._results = SimpleNamespace(export_results=lambda *args, **kwargs: {})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_terrain_result(tmp_path):
    dem = tmp_path / "terrain" / "dem_mosaic.tif"
    dem.parent.mkdir(parents=True, exist_ok=True)
    dem.write_bytes(b"\x00" * 100)   # minimal placeholder
    return TerrainResult(dem_path=dem)


def _make_basin_chars(area_mi2=50.0):
    return SimpleNamespace(
        drainage_area_km2=area_mi2 * 2.59,
        drainage_area_mi2=area_mi2,
        main_channel_length_km=20.0,
        main_channel_slope_m_per_m=0.003,
        mean_elevation_m=180.0,
        relief_m=40.0,
        centroid_lat=40.5,
        centroid_lon=-89.5,
        pour_point_lat=40.4,
        pour_point_lon=-89.6,
    )


def _make_watershed_result(tmp_path):
    dem_clipped = tmp_path / "terrain" / "dem_watershed.tif"
    dem_clipped.parent.mkdir(parents=True, exist_ok=True)
    dem_clipped.write_bytes(b"\x00" * 100)
    basin = box(300000.0, 4400000.0, 315000.0, 4415000.0)
    basin_gdf = gpd.GeoDataFrame({"name": ["watershed"]}, geometry=[basin], crs="EPSG:5070")
    stream = LineString([(307500.0, 4414000.0), (307500.0, 4401000.0)])
    streams_gdf = gpd.GeoDataFrame({"stream_id": [1]}, geometry=[stream], crs="EPSG:5070")
    subbasins_gdf = basin_gdf.copy()
    subbasins_gdf["wsno"] = [1]
    centerlines_gdf = streams_gdf.copy()
    centerlines_gdf["centerline_id"] = [1]
    breaklines_gdf = gpd.GeoDataFrame(
        {"breakline_type": ["stream", "boundary"]},
        geometry=[stream, basin.boundary],
        crs="EPSG:5070",
    )
    return SimpleNamespace(
        basin=basin_gdf,
        streams=streams_gdf,
        subbasins=subbasins_gdf,
        centerlines=centerlines_gdf,
        breaklines=breaklines_gdf,
        pour_point=Point(307500.0, 4400500.0),
        characteristics=_make_basin_chars(),
        dem_clipped=dem_clipped,
        artifacts={"fel": tmp_path / "terrain" / "fel.tif"},
    )


def _make_peak_flows():
    pf = SimpleNamespace(
        pour_point_lon=-89.5,
        pour_point_lat=40.5,
        drainage_area_mi2=50.0,
        source="regression_central",
        workspace_id=None,
        Q2=500.0,
        Q10=1200.0,
        Q50=2500.0,
        Q100=3200.0,
    )
    return pf


def _make_hydro_set():
    times = np.linspace(0, 24, 97)
    flows = np.maximum(np.sin(np.linspace(0, np.pi, 97)) * 1000 + 10, 0)
    h10 = SimpleNamespace(
        return_period_yr=10, peak_flow_cfs=1200.0,
        time_to_peak_hr=4.0, duration_hr=24.0, time_step_hr=0.25,
        times_hr=times, flows_cfs=flows, baseflow_cfs=10.0, source="NRCS_DUH",
    )
    h50 = SimpleNamespace(
        return_period_yr=50, peak_flow_cfs=2500.0,
        time_to_peak_hr=4.0, duration_hr=24.0, time_step_hr=0.25,
        times_hr=times, flows_cfs=flows * 2.1, baseflow_cfs=10.0, source="NRCS_DUH",
    )
    h100 = SimpleNamespace(
        return_period_yr=100, peak_flow_cfs=3200.0,
        time_to_peak_hr=4.0, duration_hr=24.0, time_step_hr=0.25,
        times_hr=times, flows_cfs=flows * 2.7, baseflow_cfs=10.0, source="NRCS_DUH",
    )
    return SimpleNamespace(
        watershed_area_mi2=50.0,
        time_of_concentration_hr=3.5,
        hydrographs={10: h10, 50: h50, 100: h100},
        get=lambda rp: {10: h10, 50: h50, 100: h100}.get(rp),
    )


def _make_project(tmp_path):
    from model_builder import HecRasProject
    proj_dir = tmp_path / "model" / "ras_agent_50mi2"
    proj_dir.mkdir(parents=True, exist_ok=True)
    plan_hdf = proj_dir / "template.p01.hdf"
    plan_hdf.write_bytes(b"\x00")
    return HecRasProject(
        project_dir=proj_dir,
        project_name="ras_agent_50mi2",
        prj_file=proj_dir / "template.prj",
        geometry_file=proj_dir / "template.g01",
        flow_file=proj_dir / "template.u01",
        plan_file=proj_dir / "template.p01",
        plan_hdf=plan_hdf,
        geom_ext="g01",
        mesh_strategy="hdf5_direct",
        return_periods=[10, 50, 100],
        metadata={},
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_orchestrator_result_dataclass():
    """OrchestratorResult can be constructed and fields are accessible."""
    r = OrchestratorResult(
        name="test_run",
        pour_point=(-89.5, 40.5),
        output_dir=Path("/tmp/test"),
        terrain=TerrainResult(dem_path=Path("/tmp/dem.tif")),
        watershed=None,
        peak_flows=None,
        hydro_set=None,
        project=None,
        job_ids=["abc", "def"],
        results={100: {"depth_grid": Path("/tmp/depth.tif")}},
        duration_sec=42.7,
        status="complete",
        errors=[],
    )
    assert r.name == "test_run"
    assert r.pour_point == (-89.5, 40.5)
    assert r.terrain.dem_path == Path("/tmp/dem.tif")
    assert r.job_ids == ["abc", "def"]
    assert r.results[100]["depth_grid"] == Path("/tmp/depth.tif")
    assert r.duration_sec == pytest.approx(42.7)
    assert r.status == "complete"
    assert r.errors == []


def test_run_watershed_mock_mode(tmp_path):
    """Full pipeline in mock mode returns status='complete' with all fields set."""
    terrain_result = _make_terrain_result(tmp_path)
    ws_result = _make_watershed_result(tmp_path)
    peak_flows = _make_peak_flows()
    hydro_set = _make_hydro_set()
    project = _make_project(tmp_path)

    # Fake job ids and runner behaviour
    fake_job_ids = ["job-10", "job-50", "job-100"]
    enqueue_counter = {"n": 0}

    def fake_enqueue(name, project_dir, plan_hdf, geom_ext="g01",
                     return_period_yr=None, db_path=None):
        idx = enqueue_counter["n"]
        enqueue_counter["n"] += 1
        return fake_job_ids[idx]

    def fake_run_queue(ras_exe_dir, max_parallel=2, mock=False,
                       db_path=None, logs_dir=None):
        pass   # jobs are handled by fake_get_job

    def fake_get_job(job_id, db_path=None):
        # Map each fake job id to the corresponding plan_hdf
        idx = fake_job_ids.index(job_id)
        rp = [10, 50, 100][idx]
        hdf = project.project_dir / f"template.p{idx+1:02d}.hdf"
        hdf.write_bytes(b"\x00")
        return {"id": job_id, "status": "complete", "plan_hdf": str(hdf)}

    def fake_export_results(hdf_path, output_dir, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        return {"depth_grid": output_dir / "depth_grid.tif"}

    with patch("orchestrator._terrain.get_terrain", return_value=terrain_result.dem_path), \
         patch("orchestrator._watershed.delineate_watershed", return_value=ws_result), \
         patch("orchestrator._streamstats.get_peak_flows", return_value=peak_flows), \
         patch("orchestrator._hydrograph.generate_hydrograph_set", return_value=hydro_set), \
         patch("orchestrator._model_builder.build_model", return_value=project) as mock_build, \
         patch("orchestrator._runner.enqueue_job", side_effect=fake_enqueue), \
         patch("orchestrator._runner.run_queue", side_effect=fake_run_queue), \
         patch("orchestrator._runner.get_job", side_effect=fake_get_job), \
         patch("orchestrator._results.export_results", side_effect=fake_export_results):

        result = run_watershed(
            pour_point_lon=-89.5,
            pour_point_lat=40.5,
            output_dir=tmp_path,
            return_periods=[10, 50, 100],
            ras_exe_dir=None,   # mock mode
            name="test_mock_run",
        )

    assert result.status == "complete", f"errors: {result.errors}"
    assert result.name == "test_mock_run"
    assert result.pour_point == (-89.5, 40.5)
    assert result.terrain is not None
    assert result.watershed is not None
    assert result.peak_flows is not None
    assert result.hydro_set is not None
    assert result.project is not None
    assert len(result.job_ids) == 3
    assert result.duration_sec > 0
    assert result.errors == []
    assert mock_build.call_args.kwargs["boundary_condition_mode"] == "headwater"


def test_run_watershed_stage1_failure(tmp_path):
    """Stage 1 failure (terrain) raises OrchestratorError (non-mock mode)."""
    with patch(
        "orchestrator._terrain.get_terrain",
        side_effect=RuntimeError("tile download failed"),
    ):
        with pytest.raises(OrchestratorError, match="Stage 1"):
            run_watershed(
                pour_point_lon=-89.5,
                pour_point_lat=40.5,
                output_dir=tmp_path,
                return_periods=[100],
                ras_exe_dir=Path("/fake/ras/bin"),  # non-mock so terrain is called
            )


def test_run_watershed_stage2_failure(tmp_path):
    """Stage 2 failure (watershed) raises OrchestratorError."""
    terrain_result = _make_terrain_result(tmp_path)

    with patch("orchestrator._terrain.get_terrain", return_value=terrain_result.dem_path), \
         patch(
             "orchestrator._watershed.delineate_watershed",
             side_effect=RuntimeError("no pour point"),
         ):
        with pytest.raises(OrchestratorError, match="Stage 2"):
            run_watershed(
                pour_point_lon=-89.5,
                pour_point_lat=40.5,
                output_dir=tmp_path,
                return_periods=[100],
                ras_exe_dir=Path("/fake/ras/bin"),  # non-mock so real stages 1+2 called
            )


def test_run_watershed_stage5_partial(tmp_path):
    """Stage 5 failure (model build) yields status='partial' with error message."""
    terrain_result = _make_terrain_result(tmp_path)
    ws_result = _make_watershed_result(tmp_path)
    peak_flows = _make_peak_flows()
    hydro_set = _make_hydro_set()

    with patch("orchestrator._terrain.get_terrain", return_value=terrain_result.dem_path), \
         patch("orchestrator._watershed.delineate_watershed", return_value=ws_result), \
         patch("orchestrator._streamstats.get_peak_flows", return_value=peak_flows), \
         patch("orchestrator._hydrograph.generate_hydrograph_set", return_value=hydro_set), \
         patch(
             "orchestrator._model_builder.build_model",
             side_effect=RuntimeError("no templates registered"),
         ):

        result = run_watershed(
            pour_point_lon=-89.5,
            pour_point_lat=40.5,
            output_dir=tmp_path,
            return_periods=[10, 50, 100],
            ras_exe_dir=None,
        )

    assert result.status == "partial"
    assert len(result.errors) == 1
    assert "no templates" in result.errors[0]
    # Stages before 5 should be populated
    assert result.terrain is not None
    assert result.watershed is not None
    assert result.peak_flows is not None
    assert result.hydro_set is not None
    # Stage 5+ not populated
    assert result.project is None
    assert result.job_ids == []
    assert result.results == {}


def test_cli_help():
    """CLI --help exits with code 0."""
    orchestrator_path = Path(__file__).parent.parent / "pipeline" / "orchestrator.py"
    proc = subprocess.run(
        [sys.executable, str(orchestrator_path), "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"--help exited {proc.returncode}:\n{proc.stderr}"
    assert "pour point" in proc.stdout.lower() or "lon" in proc.stdout.lower()
