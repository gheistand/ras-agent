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
from typing import Optional

from loguru import logger

import terrain as _terrain
import watershed as _watershed
import streamstats as _streamstats
import hydrograph as _hydrograph
import model_builder as _model_builder
import runner as _runner
import results as _results

from watershed import WatershedResult
from streamstats import PeakFlowEstimates
from hydrograph import HydrographSet
from model_builder import HecRasProject


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


# ── Core Orchestrator ─────────────────────────────────────────────────────────

def run_watershed(
    pour_point_lon: float,
    pour_point_lat: float,
    output_dir: Path,
    return_periods: Optional[list] = None,
    resolution_m: float = 3.0,
    mesh_strategy: str = "template_clone",
    nlcd_raster_path: Optional[Path] = None,
    ras_exe_dir: Optional[Path] = None,
    max_parallel: int = 2,
    name: Optional[str] = None,
    write_report: bool = True,
) -> OrchestratorResult:
    """
    Run the full RAS Agent pipeline for a pour point.

    Args:
        pour_point_lon:    Outlet longitude (WGS84 decimal degrees)
        pour_point_lat:    Outlet latitude (WGS84 decimal degrees)
        output_dir:        Root directory for all pipeline outputs
        return_periods:    Return periods to model (default: [10, 50, 100])
        resolution_m:      DEM resolution in meters (default: 3.0)
        mesh_strategy:     HEC-RAS mesh build strategy (default: "template_clone")
        nlcd_raster_path:  Optional NLCD 2019 GeoTIFF for Manning's n
        ras_exe_dir:       Path to RasUnsteady binary dir; None = mock mode
        max_parallel:      Maximum simultaneous HEC-RAS jobs
        name:              Run name; defaults to "watershed_{lon}_{lat}"
        write_report:      If True and status != "failed", generate HTML report

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
    )

    # ── Stage 1: Terrain ──────────────────────────────────────────────────────
    logger.info(
        f"[Stage 1/7] Fetching terrain for pour point "
        f"({pour_point_lon:.4f}, {pour_point_lat:.4f}) …"
    )
    try:
        bbox = _bbox_from_pour_point(pour_point_lon, pour_point_lat)
        terrain_dir = output_dir / "terrain"
        dem_path = _terrain.get_terrain(bbox, terrain_dir, resolution_m)
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
        f"[Stage 2/7] Delineating watershed at "
        f"({pour_point_lon:.3f}, {pour_point_lat:.3f}) …"
    )
    try:
        ws_result = _watershed.delineate_watershed(
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
    logger.info("[Stage 3/7] Estimating peak flows (StreamStats / regression) …")
    try:
        chars = result.watershed.characteristics
        peak_flows = _streamstats.get_peak_flows(
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
        hydro_set = _hydrograph.generate_hydrograph_set(
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

    # ── Stage 5: Model build ──────────────────────────────────────────────────
    logger.info(
        f"[Stage 5/7] Building HEC-RAS model "
        f"(strategy={mesh_strategy}) …"
    )
    try:
        project = _model_builder.build_model(
            watershed=result.watershed,
            hydro_set=result.hydro_set,
            output_dir=output_dir / "model",
            return_periods=return_periods,
            mesh_strategy=mesh_strategy,
            nlcd_raster_path=nlcd_raster_path,
        )
        result.project = project
        logger.info(
            f"[Stage 5/7] Model build complete — "
            f"project at {project.project_dir}"
        )
    except Exception as exc:
        err = f"Stage 5 (model build) failed: {exc}"
        logger.error(err)
        result.errors.append(err)
        result.duration_sec = time.monotonic() - t0
        return result

    # ── Stage 6: Job queue + execute ──────────────────────────────────────────
    logger.info(
        f"[Stage 6/7] Enqueueing {len(return_periods)} jobs "
        f"(mock={mock}) …"
    )
    try:
        for i, rp in enumerate(return_periods, 1):
            rp_plan_hdf = _plan_hdf_for_rp(result.project, i)
            job_id = _runner.enqueue_job(
                name=f"{name}_T{rp}yr",
                project_dir=str(result.project.project_dir),
                plan_hdf=str(rp_plan_hdf),
                geom_ext=result.project.geom_ext,
                return_period_yr=rp,
                db_path=db_path,
            )
            result.job_ids.append(job_id)

        exe_dir = ras_exe_dir if ras_exe_dir is not None else Path("/nonexistent")
        _runner.run_queue(
            ras_exe_dir=exe_dir,
            max_parallel=max_parallel,
            mock=mock,
            db_path=db_path,
            logs_dir=logs_dir,
        )
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
        for job_id, rp in zip(result.job_ids, return_periods):
            job = _runner.get_job(job_id, db_path=db_path)
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
            try:
                exported = _results.export_results(
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
    parser.add_argument("--strategy", default="template_clone",
                        help="Mesh build strategy (default: template_clone)")
    parser.add_argument("--ras-exe-dir", type=Path, default=None,
                        help="Path to RasUnsteady binary directory")
    parser.add_argument("--mock", action="store_true",
                        help="Run in mock mode (no HEC-RAS needed)")
    parser.add_argument("--name", default=None,
                        help="Run name (default: watershed_{lon}_{lat})")
    args = parser.parse_args()

    result = run_watershed(
        pour_point_lon=args.lon,
        pour_point_lat=args.lat,
        output_dir=args.output,
        return_periods=args.return_periods,
        resolution_m=args.resolution,
        mesh_strategy=args.strategy,
        ras_exe_dir=None if args.mock else args.ras_exe_dir,
        name=args.name,
    )
    print(f"Status:   {result.status}")
    print(f"Duration: {result.duration_sec:.1f}s")
    if result.errors:
        print(f"Errors:   {result.errors}")
