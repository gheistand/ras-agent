"""
orchestrator.py — End-to-end RAS Agent pipeline runner

Chains all pipeline phases into a single run_watershed() call:
  Stage 1: Terrain acquisition (terrain.py)
  Stage 2: Watershed delineation (watershed.py)
  Stage 3: Peak flow estimation (streamstats.py)
  Stage 4: Hydrograph generation (hydrograph.py)
  Stage 5: HEC-RAS model build (model_builder.py)
  Stage 6: Job queue + execution (runner.py)
  Stage 7: Results export (results.py)

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from types import SimpleNamespace

try:
    from loguru import logger
except ImportError:  # pragma: no cover - fallback for lean test environments
    import logging
    logger = logging.getLogger(__name__)

import numpy as np

try:
    import runner as _runner
except ImportError:
    _runner = None

try:
    import hecras_readiness as _hecras_readiness
except ImportError:
    _hecras_readiness = None

try:
    import terrain as _terrain
except ImportError:
    _terrain = None

try:
    import watershed as _watershed
    from watershed import WatershedResult, BasinCharacteristics
except ImportError:
    _watershed = None
    WatershedResult = Any
    BasinCharacteristics = Any

try:
    import streamstats as _streamstats
    from streamstats import PeakFlowEstimates
except ImportError:
    _streamstats = None
    PeakFlowEstimates = Any

try:
    import hydrograph as _hydrograph
    from hydrograph import HydrographSet
except ImportError:
    _hydrograph = None
    HydrographSet = Any

try:
    import model_builder as _model_builder
    from model_builder import HecRasProject
except ImportError:
    _model_builder = None
    HecRasProject = Any

try:
    import results as _results
except ImportError:
    _results = None

# Optional: precipitation stage
try:
    import precipitation as _precipitation
    _has_precip = True
except ImportError:
    _has_precip = False


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class TerrainResult:
    """Wraps the DEM path returned by terrain.get_terrain()."""
    dem_path: Path


@dataclass
class OrchestratorResult:
    """Full provenance record for a completed pipeline run."""
    name: str
    pour_point: tuple            # (lon, lat)
    output_dir: Path
    terrain: Optional[TerrainResult]
    watershed: Optional[WatershedResult]
    peak_flows: Optional[PeakFlowEstimates]
    hydro_set: Optional[HydrographSet]
    project: Optional[HecRasProject]
    job_ids: list
    results: dict                # {return_period_yr: {name: path}}
    duration_sec: float
    status: str                  # "complete" | "partial" | "failed"
    errors: list                 # non-fatal errors encountered
    archive_dir: Optional[Path] = None  # ras2cng GeoParquet archive (Stage 7b)
    pre_run_readiness: Optional[list[dict]] = None
    water_source: dict = field(default_factory=dict)
    precip_result: Optional[dict] = None  # {rp: PrecipitationResult} from Stage 4.5


class OrchestratorError(RuntimeError):
    """Raised on fatal (unrecoverable) pipeline failures."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bbox_from_pour_point(
    lon: float,
    lat: float,
    buffer_deg: float = 0.15,
) -> tuple:
    """
    Compute a WGS84 bounding box around a pour point.

    Args:
        lon:        Pour point longitude (decimal degrees)
        lat:        Pour point latitude (decimal degrees)
        buffer_deg: Half-width of bounding box in degrees (~15 km at IL latitudes)

    Returns:
        (west, south, east, north) bounding box in WGS84
    """
    return (
        lon - buffer_deg,
        lat - buffer_deg,
        lon + buffer_deg,
        lat + buffer_deg,
    )


def _plan_hdf_for_rp(project: HecRasProject, rp_index: int) -> Path:
    """
    Derive the plan HDF path for return-period index rp_index (1-based).

    model_builder names plan files as {project_base}.p01.hdf, .p02.hdf, …
    project.plan_hdf points to the primary (.p01.hdf) file.
    """
    hdf_name = project.plan_hdf.name          # e.g. "MyProject.p01.hdf"
    dot_p_idx = hdf_name.rfind(".p")
    if dot_p_idx == -1:
        # Fallback: use primary plan_hdf for all
        return project.plan_hdf
    prj_base = hdf_name[:dot_p_idx]           # e.g. "MyProject"
    return project.project_dir / f"{prj_base}.p{rp_index:02d}.hdf"


def _require_module(module, name: str):
    """Raise a clear error when an optional runtime dependency is unavailable."""
    if module is None:
        raise ImportError(
            f"{name} dependencies are not installed in this environment. "
            f"Install the pipeline requirements before running non-mock workflows."
        )
    return module


def _water_source_from_validation(validation: dict) -> dict:
    """Return the compact water-source metadata carried by run/batch reports."""
    if not isinstance(validation, dict):
        return {}

    water_source = {
        "schema_version": validation.get("schema_version"),
        "mode": validation.get("mode", "unknown"),
        "requested_mode": validation.get("requested_mode"),
        "contract_status": validation.get("contract_status", "not_recorded"),
        "production_ready": bool(validation.get("production_ready", False)),
        "screening_only": bool(validation.get("screening_only", False)),
        "provenance": validation.get("provenance") or {},
        "diagnostics": validation.get("diagnostics") or [],
        "warnings": validation.get("warnings") or [],
    }

    project_dir = None
    file_evidence = validation.get("file_evidence") or {}
    for key in ("plan_files", "flow_files"):
        for record in file_evidence.get(key, []) or []:
            path_value = record.get("path")
            if path_value:
                project_dir = Path(path_value).parent
                break
        if project_dir is not None:
            break
    if project_dir is not None:
        water_source["validation_path"] = str(
            project_dir / "water_source_validation.json"
        )
        water_source["metadata_path"] = str(
            project_dir / "ras_agent_model_metadata.json"
        )

    return water_source


# ── Mock Data Generators ─────────────────────────────────────────────────────

def _mock_terrain(output_dir: Path, lon: float, lat: float) -> "TerrainResult":
    """Generate a tiny synthetic DEM for mock mode (no network calls)."""
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.crs import CRS

    terrain_dir = output_dir / "terrain"
    terrain_dir.mkdir(parents=True, exist_ok=True)
    dem_path = terrain_dir / "dem_mock.tif"

    if not dem_path.exists():
        # 20x20 grid, slight slope from NW to SE, centered on pour point
        data = np.linspace(290, 270, 400, dtype=np.float32).reshape(20, 20)
        # Rough EPSG:5070 coords near pour point
        from pyproj import Transformer
        t = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
        cx, cy = t.transform(lon, lat)
        half = 5000  # 5km half-extent
        transform = from_bounds(cx - half, cy - half, cx + half, cy + half, 20, 20)
        with rasterio.open(
            dem_path, "w", driver="GTiff", height=20, width=20,
            count=1, dtype="float32", crs=CRS.from_epsg(5070),
            transform=transform, nodata=-9999,
        ) as dst:
            dst.write(data, 1)
    return TerrainResult(dem_path=dem_path)


def _mock_watershed(lon: float, lat: float) -> "WatershedResult":
    """Generate a synthetic WatershedResult for mock mode (no DEM needed)."""
    import geopandas as gpd
    from shapely.geometry import LineString, Point, box
    from pyproj import Transformer

    # ~50 mi² square basin in WGS84, converted to EPSG:5070 for GeoDataFrame
    basin_wgs84 = box(lon - 0.15, lat - 0.15, lon + 0.15, lat + 0.15)
    basin_gdf = gpd.GeoDataFrame(geometry=[basin_wgs84], crs="EPSG:4326").to_crs("EPSG:5070")
    centroid = basin_gdf.geometry.iloc[0].centroid
    stream_line = LineString([(centroid.x, centroid.y + 8000), (centroid.x, centroid.y - 8000)])
    streams_gdf = gpd.GeoDataFrame({"stream_id": [1]}, geometry=[stream_line], crs="EPSG:5070")
    subbasins_gdf = basin_gdf.copy()
    subbasins_gdf["wsno"] = [1]
    centerlines_gdf = streams_gdf.copy()
    centerlines_gdf["centerline_id"] = [1]
    breaklines_gdf = gpd.GeoDataFrame(
        {"breakline_type": ["stream", "boundary"]},
        geometry=[stream_line, basin_gdf.geometry.iloc[0].boundary],
        crs="EPSG:5070",
    )

    t = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    cx, cy = t.transform(lon, lat)
    pour_pt = Point(cx, cy)

    chars = BasinCharacteristics(
        drainage_area_km2=130.0,
        drainage_area_mi2=50.2,
        mean_elevation_m=285.0,
        relief_m=35.0,
        main_channel_length_km=22.0,
        main_channel_slope_m_per_m=0.0015,
        centroid_lon=lon,
        centroid_lat=lat,
        pour_point_lon=lon,
        pour_point_lat=lat,
    )
    return WatershedResult(
        basin=basin_gdf,
        streams=streams_gdf,
        subbasins=subbasins_gdf,
        centerlines=centerlines_gdf,
        breaklines=breaklines_gdf,
        pour_point=pour_pt,
        characteristics=chars,
        dem_clipped=None,
        artifacts={},
    )


def _mock_peak_flows(lon: float, lat: float) -> "PeakFlowEstimates":
    """Generate synthetic peak flows for mock mode (central IL agricultural)."""
    return SimpleNamespace(
        pour_point_lon=lon,
        pour_point_lat=lat,
        drainage_area_mi2=50.2,
        source="mock",
        workspace_id=None,
        Q2=1800,
        Q5=3100,
        Q10=4200,
        Q25=6000,
        Q50=7600,
        Q100=9400,
        Q500=13800,
    )


# ── Core Orchestrator ─────────────────────────────────────────────────────────

def run_watershed(
    pour_point_lon: float,
    pour_point_lat: float,
    output_dir: Path,
    return_periods: Optional[list] = None,
    resolution_m: float = 3.0,
    mesh_strategy: str = "geometry_first",
    boundary_condition_mode: str = "headwater",
    nlcd_raster_path: Optional[Path] = None,
    water_source_mode: Optional[str] = "auto",
    water_source_provenance: Optional[dict] = None,
    allow_low_detail_screening: bool = False,
    ras_exe_dir: Optional[Path] = None,
    max_parallel: int = 2,
    name: Optional[str] = None,
    precip_mode: str = "skip",
    write_report: bool = True,
    notify_config=None,        # Optional[NotifyConfig] — see pipeline/notify.py
    cloud_native: bool = True,  # If True, export GeoParquet archive via ras2cng (Stage 7b)
    r2_config=None,             # Optional R2Config for cloud upload
    slurm_config=None,      # Optional[SlurmConfig] — see pipeline/slurm.py
) -> OrchestratorResult:
    """
    Run the full RAS Agent pipeline for a pour point.

    Args:
        pour_point_lon:    Outlet longitude (WGS84 decimal degrees)
        pour_point_lat:    Outlet latitude (WGS84 decimal degrees)
        output_dir:        Root directory for all pipeline outputs
        return_periods:    Return periods to model (default: [10, 50, 100])
        resolution_m:      DEM resolution in meters (default: 3.0)
        mesh_strategy:     HEC-RAS mesh build strategy (default: "geometry_first")
                           uses ras-commander GeomStorage to write .g## and
                           lets HEC-RAS regenerate HDF artifacts
        boundary_condition_mode:
                           "headwater" | "downstream". Downstream is scaffolded
                           through this API but intentionally fails fast in the
                           builder until chained-basin implementation resumes.
        nlcd_raster_path:  Optional NLCD 2019 GeoTIFF for Manning's n
        water_source_mode: "auto" | "rain_on_grid" | "external_hydrograph" |
                           "mock_screening" | "none"; non-mock execution
                           requires production-ready water-source validation.
        water_source_provenance:
                           Optional source/provenance payload for the selected
                           water-source mode.
        allow_low_detail_screening:
                           Allow explicit screening output, but do not execute
                           it as a production model.
        ras_exe_dir:       Path to RasUnsteady binary dir; None = mock mode
        max_parallel:      Maximum simultaneous HEC-RAS jobs
        name:              Run name; defaults to "watershed_{lon}_{lat}"
        write_report:      If True and status != "failed", generate HTML report
        notify_config:     Optional NotifyConfig for webhook/email on completion
        cloud_native:      If True, run Stage 7b GeoParquet export via ras2cng
        r2_config:         Optional R2Config for uploading results + archive to R2
        slurm_config:      Optional SlurmConfig for submitting HEC-RAS jobs to the
                           NCSA Illinois Computes Campus Cluster via SLURM.
                           If None, jobs run locally (default).

    Returns:
        OrchestratorResult with full provenance and output paths
    """
    if return_periods is None:
        return_periods = [10, 50, 100]

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if name is None:
        name = f"watershed_{pour_point_lon:.4f}_{pour_point_lat:.4f}"

    mock = (ras_exe_dir is None)
    db_path = output_dir / "jobs.db"
    logs_dir = output_dir / "logs"

    t0 = time.monotonic()

    # Initialise result accumulator
    result = OrchestratorResult(
        name=name,
        pour_point=(pour_point_lon, pour_point_lat),
        output_dir=output_dir,
        terrain=None,
        watershed=None,
        peak_flows=None,
        hydro_set=None,
        project=None,
        job_ids=[],
        results={},
        duration_sec=0.0,
        status="partial",
        errors=[],
        pre_run_readiness=None,
    )

    # ── Stage 1: Terrain ──────────────────────────────────────────────────────
    logger.info(
        f"[Stage 1/7] {'(mock) ' if mock else ''}Fetching terrain for pour point "
        f"({pour_point_lon:.4f}, {pour_point_lat:.4f}) …"
    )
    try:
        if mock:
            result.terrain = _mock_terrain(output_dir, pour_point_lon, pour_point_lat)
            logger.info("[Stage 1/7] Terrain complete — synthetic DEM (mock mode)")
        else:
            bbox = _bbox_from_pour_point(pour_point_lon, pour_point_lat)
            terrain_dir = output_dir / "terrain"
            terrain_mod = _require_module(_terrain, "terrain")
            dem_path = terrain_mod.get_terrain(bbox, terrain_dir, resolution_m)
            result.terrain = TerrainResult(dem_path=dem_path)
            size_mb = dem_path.stat().st_size / 1e6 if dem_path.exists() else 0
            logger.info(
                f"[Stage 1/7] Terrain complete — DEM at {dem_path} "
                f"({size_mb:.1f} MB)"
            )
    except Exception as exc:
        raise OrchestratorError(
            f"Stage 1 (terrain) failed: {exc}. "
            "Check pour point coordinates and network connectivity."
        ) from exc

    # ── Stage 2: Watershed delineation ───────────────────────────────────────
    logger.info(
        f"[Stage 2/7] {'(mock) ' if mock else ''}Delineating watershed at "
        f"({pour_point_lon:.3f}, {pour_point_lat:.3f}) …"
    )
    try:
        if mock:
            result.watershed = _mock_watershed(pour_point_lon, pour_point_lat)
            chars = result.watershed.characteristics
            logger.info(
                f"[Stage 2/7] Watershed complete — "
                f"{chars.drainage_area_km2:.1f} km² (mock mode)"
            )
        else:
            watershed_mod = _require_module(_watershed, "watershed")
            ws_result = watershed_mod.delineate_watershed(
                dem_path=result.terrain.dem_path,
                pour_point_lon=pour_point_lon,
                pour_point_lat=pour_point_lat,
            )
            result.watershed = ws_result
            chars = ws_result.characteristics
            logger.info(
                f"[Stage 2/7] Watershed complete — "
                f"{chars.drainage_area_km2:.1f} km² "
                f"({chars.drainage_area_mi2:.1f} mi²)"
            )
    except Exception as exc:
        raise OrchestratorError(
            f"Stage 2 (watershed delineation) failed: {exc}. "
            "Check DEM coverage and pour point location."
        ) from exc

    # ── Stage 3: Peak flow estimation ─────────────────────────────────────────
    logger.info(
        f"[Stage 3/7] {'(mock) ' if mock else ''}Estimating peak flows …"
    )
    try:
        if mock:
            result.peak_flows = _mock_peak_flows(pour_point_lon, pour_point_lat)
            logger.info(
                f"[Stage 3/7] Peak flows complete — "
                f"Q100={result.peak_flows.Q100:.0f} cfs (mock mode)"
            )
        else:
            chars = result.watershed.characteristics
            streamstats_mod = _require_module(_streamstats, "streamstats")
            peak_flows = streamstats_mod.get_peak_flows(
                pour_point_lon=pour_point_lon,
                pour_point_lat=pour_point_lat,
                drainage_area_mi2=chars.drainage_area_mi2,
                channel_slope_m_per_m=chars.main_channel_slope_m_per_m,
            )
            result.peak_flows = peak_flows
            logger.info(
                f"[Stage 3/7] Peak flows complete — "
                f"source={peak_flows.source}, "
                f"Q100={peak_flows.Q100:.0f} cfs"
                if peak_flows.Q100 else
                f"[Stage 3/7] Peak flows complete — source={peak_flows.source}"
            )
    except Exception as exc:
        err = f"Stage 3 (peak flows) failed: {exc}"
        logger.error(err)
        result.errors.append(err)
        result.duration_sec = time.monotonic() - t0
        return result

    # ── Stage 4: Hydrograph generation ───────────────────────────────────────
    logger.info(
        f"[Stage 4/7] Generating hydrographs for return periods "
        f"{return_periods} …"
    )
    try:
        chars = result.watershed.characteristics
        hydrograph_mod = _require_module(_hydrograph, "hydrograph")
        hydro_set = hydrograph_mod.generate_hydrograph_set(
            peak_flows=result.peak_flows,
            channel_length_km=chars.main_channel_length_km,
            channel_slope_m_per_m=chars.main_channel_slope_m_per_m,
            return_periods=return_periods,
        )
        result.hydro_set = hydro_set
        logger.info(
            f"[Stage 4/7] Hydrographs complete — "
            f"{len(hydro_set.hydrographs)} generated, "
            f"Tc={hydro_set.time_of_concentration_hr:.2f} hr"
        )
    except Exception as exc:
        err = f"Stage 4 (hydrographs) failed: {exc}"
        logger.error(err)
        result.errors.append(err)
        result.duration_sec = time.monotonic() - t0
        return result

    # ── Stage 4.5: Precipitation (optional AORC rain-on-grid) ─────────────────
    precip_result = None
    if precip_mode == "aorc" and _has_precip:
        try:
            logger.info("[Stage 4.5] Downloading AORC precipitation catalog …")
            bounds_wgs84 = _bbox_from_pour_point(pour_point_lon, pour_point_lat)
            precip_result = _precipitation.run_precipitation_stage(
                bounds=bounds_wgs84,
                output_dir=output_dir,
                target_return_periods=return_periods,
                mock=mock,
            )
            logger.info(
                f"[Stage 4.5] Precipitation stage complete — "
                f"{sum(v is not None for v in precip_result.values())}/{len(precip_result)} return periods matched"
            )
        except Exception as exc:
            err = f"Stage 4.5 (precipitation) failed: {exc}"
            logger.warning(err)
            result.errors.append(err)

    result.precip_result = precip_result

    # ── Stage 5: Model build ──────────────────────────────────────────────────
    logger.info(
        f"[Stage 5/7] Building HEC-RAS model "
        f"(strategy={mesh_strategy}, bc_mode={boundary_condition_mode}) …"
    )
    try:
        model_builder_mod = _require_module(_model_builder, "model_builder")
        project = model_builder_mod.build_model(
            watershed=result.watershed,
            hydro_set=result.hydro_set,
            output_dir=output_dir / "model",
            return_periods=return_periods,
            mesh_strategy=mesh_strategy,
            boundary_condition_mode=boundary_condition_mode,
            nlcd_raster_path=nlcd_raster_path,
            water_source_mode=water_source_mode,
            water_source_provenance=water_source_provenance,
            allow_low_detail_screening=allow_low_detail_screening,
            mock=mock,
        )
        result.project = project
        result.water_source = project.metadata.get("water_source", {})
        logger.info(
            f"[Stage 5/7] Model build complete — "
            f"project at {project.project_dir}"
        )
    except Exception as exc:
        err = f"Stage 5 (model build) failed: {exc}"
        water_source_error_cls = getattr(
            _model_builder,
            "WaterSourceContractError",
            None,
        )
        if water_source_error_cls is not None and isinstance(exc, water_source_error_cls):
            result.water_source = _water_source_from_validation(
                getattr(exc, "validation", {})
            )
        logger.error(err)
        result.errors.append(err)
        result.duration_sec = time.monotonic() - t0
        return result

    # ── Stage 6: Job queue + execute ──────────────────────────────────────────
    if not mock and not result.water_source.get("production_ready", False):
        err = (
            "Stage 6 (water-source readiness) failed: generated model is "
            "not production-ready. Provide AORC/MRMS rain-on-grid or an "
            "external/generated hydrograph source before HEC-RAS execution. "
            f"Current water_source={result.water_source}"
        )
        logger.error(err)
        result.errors.append(err)
        result.duration_sec = time.monotonic() - t0
        return result

    logger.info(
        f"[Stage 6/7] Enqueueing {len(return_periods)} jobs "
        f"(mock={mock}) …"
    )
    try:
        runner_mod = _require_module(_runner, "runner")
        if not mock:
            readiness_mod = _require_module(
                _hecras_readiness,
                "hecras_readiness",
            )
            readiness_reports = []
            for i, rp in enumerate(return_periods, 1):
                rp_plan_hdf = _plan_hdf_for_rp(result.project, i)
                report = readiness_mod.check_hecras_readiness(
                    project_dir=result.project.project_dir,
                    plan_hdf=rp_plan_hdf,
                    geom_ext=result.project.geom_ext,
                    regenerate=True,
                    write_report=True,
                    report_path=(
                        result.project.project_dir
                        / f"{rp_plan_hdf.stem}_readiness.json"
                    ),
                )
                readiness_reports.append(report.to_dict())
                if not report.ready:
                    raise readiness_mod.HecRasReadinessError(report)
                logger.info(
                    f"[Stage 6/7] Pre-run readiness T={rp}yr — {report.status}"
                )
            result.pre_run_readiness = readiness_reports
            result.project.metadata["pre_run_readiness"] = readiness_reports

        for i, rp in enumerate(return_periods, 1):
            rp_plan_hdf = _plan_hdf_for_rp(result.project, i)
            job_id = runner_mod.enqueue_job(
                name=f"{name}_T{rp}yr",
                project_dir=str(result.project.project_dir),
                plan_hdf=str(rp_plan_hdf),
                geom_ext=result.project.geom_ext,
                return_period_yr=rp,
                db_path=db_path,
                execution_mode="slurm" if slurm_config is not None else "local",
            )
            result.job_ids.append(job_id)

        exe_dir = ras_exe_dir if ras_exe_dir is not None else Path("/nonexistent")
        run_queue_kwargs = {
            "ras_exe_dir": exe_dir,
            "max_parallel": max_parallel,
            "mock": mock,
            "db_path": db_path,
            "logs_dir": logs_dir,
            "slurm_config": slurm_config,
        }
        if not mock:
            run_queue_kwargs["pre_run_gate"] = False
        runner_mod.run_queue(**run_queue_kwargs)
        logger.info(
            f"[Stage 6/7] Execution complete — "
            f"{len(result.job_ids)} jobs processed"
        )
    except Exception as exc:
        err = f"Stage 6 (execution) failed: {exc}"
        logger.error(err)
        result.errors.append(err)
        result.duration_sec = time.monotonic() - t0
        return result

    # ── Stage 7: Results export ───────────────────────────────────────────────
    logger.info("[Stage 7/7] Exporting results …")
    n_files_total = 0
    try:
        runner_mod = _require_module(_runner, "runner")
        for job_id, rp in zip(result.job_ids, return_periods):
            job = runner_mod.get_job(job_id, db_path=db_path)
            if job is None or job["status"] != "complete":
                err = (
                    f"Job {job_id} (T={rp}yr) did not complete — "
                    f"status={job['status'] if job else 'not found'}; "
                    "skipping results export for this period"
                )
                logger.warning(err)
                result.errors.append(err)
                continue

            rp_output_dir = output_dir / "results" / f"{rp}yr"
            output_hdf = Path(job["plan_hdf"])

            if mock:
                # Skip real HDF export in mock mode — placeholder files have no geometry
                rp_output_dir.mkdir(parents=True, exist_ok=True)
                result.results[rp] = {"mock": rp_output_dir}
                n_files_total += 1
                logger.info(f"[mock] Results export skipped for T={rp}yr (mock mode)")
                continue

            try:
                results_mod = _require_module(_results, "results")
                exported = results_mod.export_results(
                    hdf_path=output_hdf,
                    output_dir=rp_output_dir,
                )
                result.results[rp] = exported
                n_files_total += len(exported)
            except Exception as exc:
                err = f"Results export failed for T={rp}yr: {exc}"
                logger.error(err)
                result.errors.append(err)

        logger.info(
            f"[Stage 7/7] Results exported — "
            f"{len(result.results)} return periods, "
            f"{n_files_total} output files"
        )
    except Exception as exc:
        err = f"Stage 7 (results export) failed: {exc}"
        logger.error(err)
        result.errors.append(err)

    # ── Stage 7b: Cloud-native GeoParquet export (ras2cng) ────────────────────
    if cloud_native and not mock and result.project is not None:
        logger.info("[Stage 7b] Exporting cloud-native archive via ras2cng …")
        try:
            archive_dir = _results.export_cloud_native(
                project_dir=result.project.project_dir,
                output_dir=output_dir / "archive",
                include_results=True,
                include_terrain=True,
                r2_config=r2_config,
            )
            result.archive_dir = archive_dir
            if archive_dir is not None:
                logger.info(f"[Stage 7b] Archive written to {archive_dir}")
            else:
                logger.info("[Stage 7b] Cloud-native export skipped (ras2cng unavailable)")
        except Exception as exc:
            err = f"Stage 7b (cloud-native export) failed: {exc}"
            logger.warning(err)
            result.errors.append(err)
    elif mock:
        logger.debug("[Stage 7b] Skipped — mock mode")

    # ── Finalise ──────────────────────────────────────────────────────────────
    result.duration_sec = time.monotonic() - t0
    result.status = "complete" if not result.errors else "partial"

    logger.info(
        f"Orchestrator complete in {result.duration_sec:.1f}s "
        f"[status={result.status}]"
    )
    if result.errors:
        for err in result.errors:
            logger.warning(f"  Non-fatal error: {err}")

    # ── Report generation ─────────────────────────────────────────────────────
    if write_report and result.status != "failed":
        try:
            import report as _report  # lazy import — avoids circular dependency
            _report.generate_report(result)
        except Exception as exc:
            logger.warning(f"[Report] Report generation failed (non-fatal): {exc}")

    # ── Notification ──────────────────────────────────────────────────────────
    if notify_config is not None:
        import notify as _notify  # lazy import — avoids circular dependency
        _notify.notify_run_complete(result, notify_config)

    return result


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="RAS Agent — run full watershed pipeline"
    )
    parser.add_argument("--lon",    type=float, required=True,
                        help="Pour point longitude (WGS84)")
    parser.add_argument("--lat",    type=float, required=True,
                        help="Pour point latitude (WGS84)")
    parser.add_argument("--output", type=Path,  required=True,
                        help="Output directory")
    parser.add_argument("--return-periods", type=int, nargs="+",
                        default=[10, 50, 100],
                        help="Return periods in years (default: 10 50 100)")
    parser.add_argument("--resolution", type=float, default=3.0,
                        help="DEM resolution in meters (default: 3.0)")
    parser.add_argument(
        "--strategy",
        default="geometry_first",
        help="Mesh build strategy (default: geometry_first)",
    )
    parser.add_argument(
        "--bc-mode",
        default="headwater",
        choices=["headwater", "downstream"],
        help="Boundary-condition mode scaffold (default: headwater)",
    )
    parser.add_argument(
        "--water-source-mode",
        default="auto",
        choices=["auto", "none", "rain_on_grid", "external_hydrograph", "mock_screening"],
        help="Headwater water-source contract mode (default: auto)",
    )
    parser.add_argument(
        "--water-source-provenance-json",
        default=None,
        help="JSON object describing water-source provenance",
    )
    parser.add_argument(
        "--low-detail-screening",
        action="store_true",
        help="Allow explicit low-detail screening output; not production-ready",
    )
    parser.add_argument("--ras-exe-dir", type=Path, default=None,
                        help="Path to RasUnsteady binary directory")
    parser.add_argument("--mock", action="store_true",
                        help="Run in mock mode (no HEC-RAS needed)")
    parser.add_argument("--name", default=None,
                        help="Run name (default: watershed_{lon}_{lat})")
    parser.add_argument("--webhook", default=None,
                        help="Webhook URL for completion notification")
    parser.add_argument("--notify-email", default=None,
                        help="Email address for completion notification")
    args = parser.parse_args()

    notify_config = None
    if args.webhook or args.notify_email:
        import notify as _notify
        notify_config = _notify.NotifyConfig(
            webhook_url=args.webhook,
            email_to=args.notify_email,
        )
    water_source_provenance = None
    if args.water_source_provenance_json:
        import json
        water_source_provenance = json.loads(args.water_source_provenance_json)

    result = run_watershed(
        pour_point_lon=args.lon,
        pour_point_lat=args.lat,
        output_dir=args.output,
        return_periods=args.return_periods,
        resolution_m=args.resolution,
        mesh_strategy=args.strategy,
        boundary_condition_mode=args.bc_mode,
        water_source_mode=args.water_source_mode,
        water_source_provenance=water_source_provenance,
        allow_low_detail_screening=args.low_detail_screening,
        ras_exe_dir=None if args.mock else args.ras_exe_dir,
        name=args.name,
        notify_config=notify_config,
    )
    print(f"Status:   {result.status}")
    print(f"Duration: {result.duration_sec:.1f}s")
    if result.errors:
        print(f"Errors:   {result.errors}")
