"""
trial_spring_creek_precip.py — Dry-run for precipitation + storm QC stages.

Tests the data plumbing added in the May 1 pre-call sprint:
  - Stage 4.5a: AORC storm catalog (mock mode — no large file download)
  - Stage 4.5b: GHCND storm QC (requires NOAA_CDO_TOKEN for live; mock otherwise)
  - Stage 4.5c: Design storm selection
  - Stage 4.5d: Orchestrator field check (precip_result + pre_run_readiness)

Spring Creek pilot parameters:
  Gauge:    USGS 05577500
  Centroid: ~39.88N, 89.65W  (Sangamon County, IL)
  Area:     103.4 mi²
  Bounds:   (W=-90.0, S=39.6, E=-89.2, N=40.2)  — approximate basin bbox
"""

import os
import sys
import logging
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "pipeline"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("precip_trial")


def banner(stage: str, name: str):
    log.info("=" * 60)
    log.info(f"  Stage {stage}: {name}")
    log.info("=" * 60)


def fail(stage: str, name: str, exc: Exception):
    log.error(f"\n{'='*60}")
    log.error(f"  FAILED at Stage {stage}: {name}")
    log.error(f"  {type(exc).__name__}: {exc}")
    traceback.print_exc()
    log.error(f"{'='*60}\n")
    sys.exit(1)


# Spring Creek pilot parameters
BASIN_BOUNDS = (-90.0, 39.6, -89.2, 40.2)   # (W, S, E, N) WGS84
YEARS        = [2007, 2008, 2013, 2015]       # known high-flow years on record
TARGET_DEPTH_IN = 3.5                         # approx IL 100-yr 24-hr depth

OUTPUT_DIR = Path("/Users/glennheistand/Projects/ras-agent/workspace/spring_creek/precip_trial")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

noaa_token = os.environ.get("NOAA_CDO_TOKEN")
if noaa_token:
    log.info(f"NOAA_CDO_TOKEN found ({len(noaa_token)} chars) ✓")
else:
    log.warning("NOAA_CDO_TOKEN not set — storm QC running in mock mode")
    log.warning("Get token at: https://www.ncdc.noaa.gov/cdo-web/token")

# ── Stage 4.5a: AORC storm catalog ───────────────────────────────────────────

banner("4.5a", "AORC storm catalog (mock=True for dry-run)")
try:
    from precipitation import catalog_storms

    catalog_df = catalog_storms(
        bounds=BASIN_BOUNDS,
        years=YEARS,
        percentile_threshold=80.0,
        mock=True,   # use synthetic data — no large AORC download
    )

    log.info(f"  Storms cataloged: {len(catalog_df)}")
    for _, row in catalog_df.iterrows():
        log.info(
            f"    {row['storm_id']}  depth={row['total_depth_in']:.2f}in  "
            f"dur={row['duration_hours']}hr  rank={row['rank']}"
        )
    log.info("  Stage 4.5a PASSED ✓")
except Exception as exc:
    fail("4.5a", "AORC storm catalog", exc)

# ── Stage 4.5b: GHCND storm QC ───────────────────────────────────────────────

banner("4.5b", f"GHCND storm QC (mock={noaa_token is None})")
try:
    from storm_qc import qc_storm_catalog

    qc_df = qc_storm_catalog(
        storm_catalog_df=catalog_df,
        bounds=BASIN_BOUNDS,
        mock=(noaa_token is None),   # mock if no token; live if token present
    )

    log.info(f"  QC results: {len(qc_df)} rows")
    if "qc_flag" in qc_df.columns:
        flags = qc_df["qc_flag"].value_counts().to_dict()
        log.info(f"  Flag counts: {flags}")
        for _, row in qc_df.iterrows():
            log.info(
                f"    {row.get('storm_id', '?')}  "
                f"qc={row['qc_flag']}  "
                f"aorc={row.get('total_depth_in', '?'):.2f}in  "
                f"ghcnd={row.get('ghcnd_depth_in', 'N/A')}"
            )
    log.info("  Stage 4.5b PASSED ✓")
except Exception as exc:
    fail("4.5b", "GHCND storm QC", exc)

# ── Stage 4.5c: Design storm selection ───────────────────────────────────────

banner("4.5c", f"Design storm selection (target ~{TARGET_DEPTH_IN} in)")
try:
    from precipitation import select_design_storm

    design = select_design_storm(
        catalog_df=catalog_df,
        target_depth_in=TARGET_DEPTH_IN,
        tolerance_pct=0.5,   # wide tolerance for mock data
    )

    if design is not None:
        log.info(f"  Selected storm: {design.get('storm_id', '?')}")
        log.info(f"    Depth:        {design.get('total_depth_in', '?'):.2f} in")
        log.info(f"    Duration:     {design.get('duration_hours', '?')} hr")
        log.info(f"    Sim window:   {design.get('sim_start', '?')} → {design.get('sim_end', '?')}")
    else:
        log.info("  No storm within tolerance (expected with mock data) — OK")
    log.info("  Stage 4.5c PASSED ✓")
except Exception as exc:
    fail("4.5c", "Design storm selection", exc)

# ── Stage 4.5d: Orchestrator field check ─────────────────────────────────────

banner("4.5d", "Orchestrator: precip_result + pre_run_readiness fields")
try:
    from orchestrator import OrchestratorResult

    result_fields = list(OrchestratorResult.__dataclass_fields__.keys())
    log.info(f"  OrchestratorResult fields: {result_fields}")

    assert "precip_result" in result_fields,     "MISSING: precip_result"
    assert "pre_run_readiness" in result_fields, "MISSING: pre_run_readiness"
    log.info("  precip_result         ✓")
    log.info("  pre_run_readiness     ✓")
    log.info("  Stage 4.5d PASSED ✓")
except Exception as exc:
    fail("4.5d", "Orchestrator field check", exc)

# ── Done ──────────────────────────────────────────────────────────────────────

log.info("")
log.info("=" * 60)
log.info("  PRECIP DRY-RUN COMPLETE ✓")
log.info(f"  NOAA token: {'present — live QC ran' if noaa_token else 'MISSING — mock QC only'}")
log.info("=" * 60)
if not noaa_token:
    log.info("")
    log.info("  Next step: get NOAA CDO token for live storm QC:")
    log.info("  https://www.ncdc.noaa.gov/cdo-web/token")
    log.info("  Then re-run with NOAA_CDO_TOKEN set in environment.")
