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
import model_builder as mb
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
        workflow_config={"schema_version": "ras-agent-rog-workflow-config/v1"},
    )
    assert r.name == "test_run"
    assert r.pour_point == (-89.5, 40.5)
    assert r.terrain.dem_path == Path("/tmp/dem.tif")
    assert r.job_ids == ["abc", "def"]
    assert r.results[100]["depth_grid"] == Path("/tmp/depth.tif")
    assert r.duration_sec == pytest.approx(42.7)
    assert r.status == "complete"
    assert r.errors == []
    assert r.workflow_config["schema_version"] == "ras-agent-rog-workflow-config/v1"


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
                     return_period_yr=None, db_path=None,
                     preprocess_mode="linux", execution_mode="local",
                     slurm_config=None):
        idx = enqueue_counter["n"]
        enqueue_counter["n"] += 1
        return fake_job_ids[idx]

    def fake_run_queue(ras_exe_dir, max_parallel=2, mock=False,
                       db_path=None, logs_dir=None,
                       preprocess_mode=None, slurm_config=None):
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
            ras_exe_dir=None,   # mock mode
            name="test_mock_run",
            workflow_config={
                "aep_years": [10, 50, 100],
                "durations_hours": [24],
                "mock": True,
            },
        )

    assert result.status == "complete", f"errors: {result.errors}"
    assert result.name == "test_mock_run"
    assert result.pour_point == (-89.5, 40.5)
    assert result.terrain is not None
    assert result.watershed is not None
    assert result.hand is not None
    assert result.hand.hand_path.exists()
    assert result.hand.mean_hand_m > 0
    assert result.peak_flows is not None
    assert result.hydro_set is not None
    assert result.project is not None
    assert len(result.job_ids) == 3
    assert result.duration_sec > 0
    assert result.errors == []
    assert result.workflow_config["schema_version"] == "ras-agent-rog-workflow-config/v1"
    assert result.workflow_config["plan_count"] == 3
    assert result.workflow_config["mock"] is True
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


def test_run_watershed_preserves_water_source_validation_on_block(tmp_path):
    terrain_result = _make_terrain_result(tmp_path)
    ws_result = _make_watershed_result(tmp_path)
    peak_flows = _make_peak_flows()
    hydro_set = _make_hydro_set()
    project_dir = tmp_path / "model" / "ras_agent_50mi2"
    validation = {
        "schema_version": "ras-agent-water-source/v1",
        "mode": "none",
        "requested_mode": "auto",
        "contract_status": "invalid",
        "production_ready": False,
        "screening_only": False,
        "provenance": {"source": "generated_design_hydrograph"},
        "diagnostics": ["No defensible water source was found."],
        "warnings": [],
        "file_evidence": {
            "plan_files": [{"path": str(project_dir / "ras_agent_50mi2.p01")}],
            "flow_files": [{"path": str(project_dir / "ras_agent_50mi2.u01")}],
        },
    }

    with patch("orchestrator._terrain.get_terrain", return_value=terrain_result.dem_path), \
         patch("orchestrator._watershed.delineate_watershed", return_value=ws_result), \
         patch("orchestrator._streamstats.get_peak_flows", return_value=peak_flows), \
         patch("orchestrator._hydrograph.generate_hydrograph_set", return_value=hydro_set), \
         patch(
             "orchestrator._model_builder.build_model",
             side_effect=mb.WaterSourceContractError(
                 "Generated model is not production-ready",
                 validation=validation,
             ),
         ):

        result = run_watershed(
            pour_point_lon=-89.5,
            pour_point_lat=40.5,
            output_dir=tmp_path,
            return_periods=[100],
            ras_exe_dir=None,
        )

    assert result.status == "partial"
    assert result.project is None
    assert result.water_source["mode"] == "none"
    assert result.water_source["contract_status"] == "invalid"
    assert result.water_source["production_ready"] is False
    assert result.water_source["validation_path"].endswith(
        "water_source_validation.json"
    )


def test_cli_help():
    """CLI --help exits with code 0."""
    orchestrator_path = Path(__file__).parent.parent / "pipeline" / "orchestrator.py"
    env = os.environ.copy()
    for key in ("GDAL_DATA", "PROJ_LIB", "PROJ_DATA"):
        env.pop(key, None)
    proc = subprocess.run(
        [sys.executable, str(orchestrator_path), "--help"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"--help exited {proc.returncode}:\n{proc.stderr}"
    assert "pour point" in proc.stdout.lower() or "lon" in proc.stdout.lower()


def test_orchestrator_result_new_fields():
    """New Stage 8-10 fields default to None."""
    r = OrchestratorResult(
        name="test", pour_point=(-89.5, 40.5), output_dir=Path("/tmp"),
        terrain=None, watershed=None, peak_flows=None, hydro_set=None,
        project=None, job_ids=[], results={}, duration_sec=0, status="complete",
        errors=[],
    )
    assert r.storm_qc_result is None
    assert r.report_path is None
    assert r.workspace_report is None
    assert r.precip_result is None


def test_run_watershed_stage8_precipitation_mock(tmp_path):
    """Stage 8 populates precip_result when precip_mode='aorc' in mock mode."""
    import pandas as pd
    terrain_result = _make_terrain_result(tmp_path)
    ws_result = _make_watershed_result(tmp_path)
    peak_flows = _make_peak_flows()
    hydro_set = _make_hydro_set()
    project = _make_project(tmp_path)

    fake_job_ids = ["job-10", "job-50", "job-100"]
    counter = {"n": 0}

    def fake_enqueue(**kwargs):
        idx = counter["n"]; counter["n"] += 1
        return fake_job_ids[idx]

    def fake_run_queue(**kwargs):
        pass

    def fake_get_job(job_id, db_path=None):
        idx = fake_job_ids.index(job_id)
        hdf = project.project_dir / f"template.p{idx+1:02d}.hdf"
        hdf.write_bytes(b"\x00")
        return {"id": job_id, "status": "complete", "plan_hdf": str(hdf)}

    mock_catalog = pd.DataFrame([{
        "storm_id": 1001, "total_depth_in": 3.8, "rank": 1, "year": 2022,
        "start_time": pd.Timestamp("2022-08-03 06:00"),
        "end_time": pd.Timestamp("2022-08-03 18:00"),
        "sim_start": pd.Timestamp("2022-08-03 00:00"),
        "sim_end": pd.Timestamp("2022-08-04 00:00"),
        "peak_intensity_in_hr": 1.2, "duration_hours": 12, "wet_hours": 10,
    }])
    mock_precip_result = {10: "mock_result_10", 100: "mock_result_100"}

    with patch("orchestrator._terrain.get_terrain", return_value=terrain_result.dem_path), \
         patch("orchestrator._watershed.delineate_watershed", return_value=ws_result), \
         patch("orchestrator._streamstats.get_peak_flows", return_value=peak_flows), \
         patch("orchestrator._hydrograph.generate_hydrograph_set", return_value=hydro_set), \
         patch("orchestrator._model_builder.build_model", return_value=project), \
         patch("orchestrator._runner.enqueue_job", side_effect=fake_enqueue), \
         patch("orchestrator._runner.run_queue", side_effect=fake_run_queue), \
         patch("orchestrator._runner.get_job", side_effect=fake_get_job), \
         patch("orchestrator._results.export_results", return_value={}), \
         patch("orchestrator._precipitation.catalog_storms", return_value=mock_catalog), \
         patch("orchestrator._precipitation.run_precipitation_stage", return_value=mock_precip_result):

        result = run_watershed(
            pour_point_lon=-89.5, pour_point_lat=40.5,
            output_dir=tmp_path, ras_exe_dir=None,
            precip_mode="aorc",
            workflow_config={"aep_years": [10, 50, 100], "mock": True},
        )

    assert result.precip_result is not None
    assert result.precip_result == mock_precip_result


def test_run_watershed_stage8_skip_default(tmp_path):
    """Stage 8 is skipped when precip_mode='skip' (default)."""
    terrain_result = _make_terrain_result(tmp_path)
    ws_result = _make_watershed_result(tmp_path)
    peak_flows = _make_peak_flows()
    hydro_set = _make_hydro_set()
    project = _make_project(tmp_path)

    fake_job_ids = ["job-10", "job-50", "job-100"]
    counter = {"n": 0}

    def fake_enqueue(**kwargs):
        idx = counter["n"]; counter["n"] += 1
        return fake_job_ids[idx]

    def fake_run_queue(**kwargs):
        pass

    def fake_get_job(job_id, db_path=None):
        idx = fake_job_ids.index(job_id)
        hdf = project.project_dir / f"template.p{idx+1:02d}.hdf"
        hdf.write_bytes(b"\x00")
        return {"id": job_id, "status": "complete", "plan_hdf": str(hdf)}

    with patch("orchestrator._terrain.get_terrain", return_value=terrain_result.dem_path), \
         patch("orchestrator._watershed.delineate_watershed", return_value=ws_result), \
         patch("orchestrator._streamstats.get_peak_flows", return_value=peak_flows), \
         patch("orchestrator._hydrograph.generate_hydrograph_set", return_value=hydro_set), \
         patch("orchestrator._model_builder.build_model", return_value=project), \
         patch("orchestrator._runner.enqueue_job", side_effect=fake_enqueue), \
         patch("orchestrator._runner.run_queue", side_effect=fake_run_queue), \
         patch("orchestrator._runner.get_job", side_effect=fake_get_job), \
         patch("orchestrator._results.export_results", return_value={}):

        result = run_watershed(
            pour_point_lon=-89.5, pour_point_lat=40.5,
            output_dir=tmp_path, ras_exe_dir=None,
            workflow_config={"aep_years": [10, 50, 100], "mock": True},
        )

    assert result.precip_result is None


def test_run_watershed_stage9_storm_qc_mock(tmp_path):
    """Stage 9 populates storm_qc_result when enabled with mock catalog."""
    import pandas as pd
    terrain_result = _make_terrain_result(tmp_path)
    ws_result = _make_watershed_result(tmp_path)
    peak_flows = _make_peak_flows()
    hydro_set = _make_hydro_set()
    project = _make_project(tmp_path)

    fake_job_ids = ["job-10", "job-50", "job-100"]
    counter = {"n": 0}

    def fake_enqueue(**kwargs):
        idx = counter["n"]; counter["n"] += 1
        return fake_job_ids[idx]

    def fake_run_queue(**kwargs):
        pass

    def fake_get_job(job_id, db_path=None):
        idx = fake_job_ids.index(job_id)
        hdf = project.project_dir / f"template.p{idx+1:02d}.hdf"
        hdf.write_bytes(b"\x00")
        return {"id": job_id, "status": "complete", "plan_hdf": str(hdf)}

    mock_catalog = pd.DataFrame([{
        "storm_id": 1001, "total_depth_in": 3.8, "rank": 1, "year": 2022,
        "start_time": pd.Timestamp("2022-08-03 06:00"),
        "end_time": pd.Timestamp("2022-08-03 18:00"),
        "sim_start": pd.Timestamp("2022-08-03 00:00"),
        "sim_end": pd.Timestamp("2022-08-04 00:00"),
        "peak_intensity_in_hr": 1.2, "duration_hours": 12, "wet_hours": 10,
    }])

    compared_df = mock_catalog.copy()
    compared_df["ghcnd_depth_in"] = [3.4]
    compared_df["ghcnd_stations_used"] = [2]
    compared_df["depth_ratio"] = compared_df["total_depth_in"] / compared_df["ghcnd_depth_in"]

    with patch("orchestrator._terrain.get_terrain", return_value=terrain_result.dem_path), \
         patch("orchestrator._watershed.delineate_watershed", return_value=ws_result), \
         patch("orchestrator._streamstats.get_peak_flows", return_value=peak_flows), \
         patch("orchestrator._hydrograph.generate_hydrograph_set", return_value=hydro_set), \
         patch("orchestrator._model_builder.build_model", return_value=project), \
         patch("orchestrator._runner.enqueue_job", side_effect=fake_enqueue), \
         patch("orchestrator._runner.run_queue", side_effect=fake_run_queue), \
         patch("orchestrator._runner.get_job", side_effect=fake_get_job), \
         patch("orchestrator._results.export_results", return_value={}), \
         patch("orchestrator._precipitation.catalog_storms", return_value=mock_catalog), \
         patch("orchestrator._precipitation.run_precipitation_stage", return_value={}), \
         patch("orchestrator._storm_qc.compare_storm_depths", return_value=compared_df):

        result = run_watershed(
            pour_point_lon=-89.5, pour_point_lat=40.5,
            output_dir=tmp_path, ras_exe_dir=None,
            precip_mode="aorc",
            storm_qc_enabled=True,
            workflow_config={"aep_years": [10, 50, 100], "mock": True},
        )

    assert result.storm_qc_result is not None
    assert len(result.storm_qc_result) == 1
    assert result.storm_qc_result[0]["qc_flag"] == "ok"
    assert result.storm_qc_result[0]["storm_id"] == 1001


def test_run_watershed_stage9_skipped_without_catalog(tmp_path):
    """Stage 9 is skipped when precip_mode='skip' even if storm_qc_enabled=True."""
    terrain_result = _make_terrain_result(tmp_path)
    ws_result = _make_watershed_result(tmp_path)
    peak_flows = _make_peak_flows()
    hydro_set = _make_hydro_set()
    project = _make_project(tmp_path)

    fake_job_ids = ["job-10", "job-50", "job-100"]
    counter = {"n": 0}

    def fake_enqueue(**kwargs):
        idx = counter["n"]; counter["n"] += 1
        return fake_job_ids[idx]

    def fake_run_queue(**kwargs):
        pass

    def fake_get_job(job_id, db_path=None):
        idx = fake_job_ids.index(job_id)
        hdf = project.project_dir / f"template.p{idx+1:02d}.hdf"
        hdf.write_bytes(b"\x00")
        return {"id": job_id, "status": "complete", "plan_hdf": str(hdf)}

    with patch("orchestrator._terrain.get_terrain", return_value=terrain_result.dem_path), \
         patch("orchestrator._watershed.delineate_watershed", return_value=ws_result), \
         patch("orchestrator._streamstats.get_peak_flows", return_value=peak_flows), \
         patch("orchestrator._hydrograph.generate_hydrograph_set", return_value=hydro_set), \
         patch("orchestrator._model_builder.build_model", return_value=project), \
         patch("orchestrator._runner.enqueue_job", side_effect=fake_enqueue), \
         patch("orchestrator._runner.run_queue", side_effect=fake_run_queue), \
         patch("orchestrator._runner.get_job", side_effect=fake_get_job), \
         patch("orchestrator._results.export_results", return_value={}):

        result = run_watershed(
            pour_point_lon=-89.5, pour_point_lat=40.5,
            output_dir=tmp_path, ras_exe_dir=None,
            storm_qc_enabled=True,
            workflow_config={"aep_years": [10, 50, 100], "mock": True},
        )

    assert result.storm_qc_result is None


def test_run_watershed_stage8_failure_non_fatal(tmp_path):
    """Stage 8 failure is non-fatal — pipeline continues to Stage 9/10."""
    terrain_result = _make_terrain_result(tmp_path)
    ws_result = _make_watershed_result(tmp_path)
    peak_flows = _make_peak_flows()
    hydro_set = _make_hydro_set()
    project = _make_project(tmp_path)

    fake_job_ids = ["job-10", "job-50", "job-100"]
    counter = {"n": 0}

    def fake_enqueue(**kwargs):
        idx = counter["n"]; counter["n"] += 1
        return fake_job_ids[idx]

    def fake_run_queue(**kwargs):
        pass

    def fake_get_job(job_id, db_path=None):
        idx = fake_job_ids.index(job_id)
        hdf = project.project_dir / f"template.p{idx+1:02d}.hdf"
        hdf.write_bytes(b"\x00")
        return {"id": job_id, "status": "complete", "plan_hdf": str(hdf)}

    with patch("orchestrator._terrain.get_terrain", return_value=terrain_result.dem_path), \
         patch("orchestrator._watershed.delineate_watershed", return_value=ws_result), \
         patch("orchestrator._streamstats.get_peak_flows", return_value=peak_flows), \
         patch("orchestrator._hydrograph.generate_hydrograph_set", return_value=hydro_set), \
         patch("orchestrator._model_builder.build_model", return_value=project), \
         patch("orchestrator._runner.enqueue_job", side_effect=fake_enqueue), \
         patch("orchestrator._runner.run_queue", side_effect=fake_run_queue), \
         patch("orchestrator._runner.get_job", side_effect=fake_get_job), \
         patch("orchestrator._results.export_results", return_value={}), \
         patch("orchestrator._precipitation.catalog_storms", side_effect=RuntimeError("S3 timeout")):

        result = run_watershed(
            pour_point_lon=-89.5, pour_point_lat=40.5,
            output_dir=tmp_path, ras_exe_dir=None,
            precip_mode="aorc",
            workflow_config={"aep_years": [10, 50, 100], "mock": True},
        )

    assert result.status == "partial"
    assert any("Stage 8" in e for e in result.errors)
    assert result.precip_result is None
    assert result.storm_qc_result is None


def test_run_watershed_stage10_report_path(tmp_path):
    """Stage 10 sets report_path when report generation succeeds."""
    terrain_result = _make_terrain_result(tmp_path)
    ws_result = _make_watershed_result(tmp_path)
    peak_flows = _make_peak_flows()
    hydro_set = _make_hydro_set()
    project = _make_project(tmp_path)

    fake_job_ids = ["job-10", "job-50", "job-100"]
    counter = {"n": 0}

    def fake_enqueue(**kwargs):
        idx = counter["n"]; counter["n"] += 1
        return fake_job_ids[idx]

    def fake_run_queue(**kwargs):
        pass

    def fake_get_job(job_id, db_path=None):
        idx = fake_job_ids.index(job_id)
        hdf = project.project_dir / f"template.p{idx+1:02d}.hdf"
        hdf.write_bytes(b"\x00")
        return {"id": job_id, "status": "complete", "plan_hdf": str(hdf)}

    expected_report = tmp_path / "report.html"

    with patch("orchestrator._terrain.get_terrain", return_value=terrain_result.dem_path), \
         patch("orchestrator._watershed.delineate_watershed", return_value=ws_result), \
         patch("orchestrator._streamstats.get_peak_flows", return_value=peak_flows), \
         patch("orchestrator._hydrograph.generate_hydrograph_set", return_value=hydro_set), \
         patch("orchestrator._model_builder.build_model", return_value=project), \
         patch("orchestrator._runner.enqueue_job", side_effect=fake_enqueue), \
         patch("orchestrator._runner.run_queue", side_effect=fake_run_queue), \
         patch("orchestrator._runner.get_job", side_effect=fake_get_job), \
         patch("orchestrator._results.export_results", return_value={}), \
         patch.dict("sys.modules", {"report": SimpleNamespace(generate_report=lambda r: expected_report)}):

        result = run_watershed(
            pour_point_lon=-89.5, pour_point_lat=40.5,
            output_dir=tmp_path, ras_exe_dir=None,
            write_report=True,
            workflow_config={"aep_years": [10, 50, 100], "mock": True},
        )

    assert result.report_path == expected_report


def test_cli_new_args():
    """CLI --help includes the new Stage 8-10 arguments."""
    orchestrator_path = Path(__file__).parent.parent / "pipeline" / "orchestrator.py"
    env = os.environ.copy()
    for key in ("GDAL_DATA", "PROJ_LIB", "PROJ_DATA"):
        env.pop(key, None)
    proc = subprocess.run(
        [sys.executable, str(orchestrator_path), "--help"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert "--precip-mode" in proc.stdout
    assert "--storm-qc" in proc.stdout
    assert "--workspace-dir" in proc.stdout
