"""
trial_spring_creek.py — End-to-end pipeline trial using pre-staged Spring Creek data.

Stages:
  1. [BYPASS] Terrain — use staged spring_creek_basin_dem_5070.tif
  2. Watershed delineation — run pysheds on the staged DEM
  3. Peak flows — LP3 from staged annual peaks RDB (gauge_lp3, not regression)
  4. Hydrograph generation
  5. Model build (mock mode — no HEC-RAS binary needed)
  6. Runner enqueue + mock execution

Reports exact failure point and full traceback so we know what to fix.
"""

import sys
import traceback
from pathlib import Path
import logging

# pysheds 0.5 uses np.in1d which was removed in NumPy 2.0 — patch it.
import numpy as _np
if not hasattr(_np, 'in1d'):
    _np.in1d = _np.isin

# ── Paths ─────────────────────────────────────────────────────────────────────

SPRING_CREEK = Path("/Users/glennheistand/Projects/ras-agent/workspace/spring_creek")
STAGED_DEM   = SPRING_CREEK / "04_terrain" / "spring_creek_basin_dem_5070.tif"
STAGED_RDB   = SPRING_CREEK / "01_gauge" / "peaks" / "USGS_05577500_annual_peaks.rdb"
OUTPUT_DIR   = SPRING_CREEK / "08_model_validation" / "ras_agent_95mi2"

# Spring Creek gauge location (WGS84)
POUR_LON = -89.7010  # 05577500
POUR_LAT =  39.7730

RETURN_PERIODS = [10, 50, 100]

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("trial")


def banner(stage: int, name: str):
    log.info("=" * 60)
    log.info(f"  Stage {stage}: {name}")
    log.info("=" * 60)


def fail(stage: int, name: str, exc: Exception):
    log.error(f"\n{'='*60}")
    log.error(f"  FAILED at Stage {stage}: {name}")
    log.error(f"  {type(exc).__name__}: {exc}")
    log.error("  Traceback:")
    traceback.print_exc()
    log.error(f"{'='*60}\n")
    sys.exit(1)


# ── Stage 0: Sanity checks ────────────────────────────────────────────────────

log.info("Pre-flight checks...")
assert STAGED_DEM.exists(), f"Missing staged DEM: {STAGED_DEM}"
assert STAGED_RDB.exists(), f"Missing staged RDB: {STAGED_RDB}"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
log.info(f"  DEM:    {STAGED_DEM}  ({STAGED_DEM.stat().st_size / 1e6:.1f} MB)")
log.info(f"  RDB:    {STAGED_RDB}")
log.info(f"  Output: {OUTPUT_DIR}")

# ── Stage 1: Terrain (bypassed) ───────────────────────────────────────────────

banner(1, "Terrain [BYPASSED — using staged DEM]")
from dataclasses import dataclass

@dataclass
class TerrainResult:
    dem_path: Path

terrain = TerrainResult(dem_path=STAGED_DEM)
log.info(f"  Using: {STAGED_DEM}")

# ── Stage 2: Watershed delineation ───────────────────────────────────────────

banner(2, "Watershed delineation (pysheds D8)")
try:
    sys.path.insert(0, str(Path(__file__).parent / "pipeline"))
    from watershed import delineate_watershed, WatershedResult
    ws = delineate_watershed(
        dem_path=terrain.dem_path,
        pour_point_lon=POUR_LON,
        pour_point_lat=POUR_LAT,
    )
    chars = ws.characteristics
    log.info(f"  Drainage area: {chars.drainage_area_km2:.1f} km²  ({chars.drainage_area_mi2:.1f} mi²)")
    log.info(f"  Channel length: {chars.main_channel_length_km:.2f} km")
    log.info(f"  Mean slope: {chars.main_channel_slope_m_per_m:.5f} m/m")
    if chars.drainage_area_mi2 < 50:
        log.warning(f"  ⚠️  Area only {chars.drainage_area_mi2:.1f} mi² — expected ~95 mi². DEM edge / pour point snapping issue.")
        log.warning("  Boundary handoff outlet at (531443, 1883487) EPSG:5070 may fix this.")
        log.warning("  Continuing run anyway to find all downstream blockers.")
    log.info("  Stage 2 PASSED ✓")
except Exception as exc:
    fail(2, "Watershed delineation", exc)

# ── Stage 3: Peak flows (LP3 from gauge RDB) ──────────────────────────────────

banner(3, "Peak flows — LP3 from USGS annual peaks RDB")
try:
    from streamstats import get_peak_flows_from_rdb, PeakFlowEstimates
    pf = get_peak_flows_from_rdb(STAGED_RDB)
    # Inject drainage area from watershed delineation
    pf = PeakFlowEstimates(
        pour_point_lon=POUR_LON,
        pour_point_lat=POUR_LAT,
        drainage_area_mi2=chars.drainage_area_mi2,
        source=pf.source,
        workspace_id=None,
        Q2=pf.Q2, Q5=pf.Q5, Q10=pf.Q10, Q25=pf.Q25,
        Q50=pf.Q50, Q100=pf.Q100, Q500=pf.Q500,
    )
    log.info(f"  Q2={pf.Q2:.0f}  Q10={pf.Q10:.0f}  Q50={pf.Q50:.0f}  Q100={pf.Q100:.0f}  Q500={pf.Q500:.0f} cfs")
    log.info(f"  Source: {pf.source}")
    log.info("  Stage 3 PASSED ✓")
except Exception as exc:
    fail(3, "Peak flows LP3", exc)

# ── Stage 4: Hydrograph generation ───────────────────────────────────────────

banner(4, "Hydrograph generation (NRCS unit hydrograph)")
try:
    from hydrograph import generate_hydrograph_set, save_hydrographs_csv
    hydro_set = generate_hydrograph_set(
        peak_flows=pf,
        channel_length_km=chars.main_channel_length_km,
        channel_slope_m_per_m=chars.main_channel_slope_m_per_m,
        return_periods=RETURN_PERIODS,
    )
    hydro_dir = OUTPUT_DIR / "hydrographs"
    paths = save_hydrographs_csv(hydro_set, hydro_dir)
    for rp, p in paths.items():
        log.info(f"  Q{rp}: {p}")
    log.info("  Stage 4 PASSED ✓")
except Exception as exc:
    fail(4, "Hydrograph generation", exc)

# ── Stage 5: Model build (mock) ───────────────────────────────────────────────

banner(5, "HEC-RAS model build (mock mode)")
try:
    from model_builder import build_model
    project = build_model(
        watershed=ws,
        hydro_set=hydro_set,
        output_dir=OUTPUT_DIR / "model",
        return_periods=RETURN_PERIODS,
        mock=True,
        nlcd_raster_path=SPRING_CREEK / "05_landcover_nlcd" / "nlcd_2021_watershed.tif",
    )
    log.info(f"  Project dir:  {project.project_dir}")
    log.info(f"  Geometry file: {project.geometry_file}")
    log.info(f"  Plan file:     {project.plan_file}")
    log.info(f"  Plan HDF:      {project.plan_hdf}")
    log.info(f"  Geom ext:      {project.geom_ext}")
    log.info("  Stage 5 PASSED ✓")
except Exception as exc:
    fail(5, "Model build", exc)

# ── Stage 6: Runner (mock enqueue + execute) ──────────────────────────────────

banner(6, "Runner — enqueue + mock execute")
try:
    import runner as _runner
    db_path = OUTPUT_DIR / "jobs.db"
    job_ids = []
    # Mock mode produces one plan file — enqueue it for the primary return period
    jid = _runner.enqueue_job(
        db_path=db_path,
        name="spring_creek_Q100",
        project_dir=project.project_dir,
        plan_hdf=project.plan_hdf,
        return_period_yr=100,
    )
    job_ids.append(jid)
    log.info(f"  Enqueued Q100: {jid}")

    # Mock execute
    _runner.run_job(db_path=db_path, job_id=jid, ras_exe_dir=Path("/dev/null"), mock=True)
    log.info(f"  Mock run complete: {jid}")

    log.info("  Stage 6 PASSED ✓")
except Exception as exc:
    fail(6, "Runner", exc)

# ── Done ──────────────────────────────────────────────────────────────────────

log.info("")
log.info("=" * 60)
log.info("  ALL STAGES PASSED ✓")
log.info(f"  Output: {OUTPUT_DIR}")
log.info("=" * 60)
