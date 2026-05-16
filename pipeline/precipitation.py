"""
precipitation.py — AORC rain-on-grid precipitation stage

Wraps ras-commander PrecipAorc to catalog, select, and download AORC
storm events for use as rain-on-grid boundary conditions in HEC-RAS 2D.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger


# ── Depth-Frequency Sources ───────────────────────────────────────────────────

VALID_DEPTH_SOURCES = ("bulletin_75", "atlas_14")

# ISWS Bulletin 75 (Huff & Angel, 1992) — point precipitation frequency for Illinois.
# Depths in inches for standard return periods (statewide median values).
# For production, look up by lat/lon from the Bulletin 75 isopluvial maps;
# these statewide medians are suitable for central IL (40°N) and serve as
# the default when site-specific lookup is not yet implemented.
_BULLETIN_75_DEPTHS_IN: dict[int, float] = {
    2:   2.9,
    5:   3.6,
    10:  4.2,
    25:  5.0,
    50:  5.7,
    100: 6.4,
    500: 8.1,
}

# NOAA Atlas 14 Vol. 2 (Bonnin et al., 2006) — 24-hour point depths for central IL.
# Used as a selectable alternative. Same caveat: site-specific PFDS lookup
# should replace these representative values in production.
_ATLAS_14_DEPTHS_IN: dict[int, float] = {
    2:   2.8,
    5:   3.5,
    10:  4.1,
    25:  5.1,
    50:  5.8,
    100: 6.6,
    500: 8.5,
}

_DEPTH_TABLES: dict[str, dict[int, float]] = {
    "bulletin_75": _BULLETIN_75_DEPTHS_IN,
    "atlas_14": _ATLAS_14_DEPTHS_IN,
}


def get_design_depth(
    return_period_yr: int,
    depth_source: str = "bulletin_75",
) -> float:
    """Look up 24-hour design depth for a return period from the given source.

    Args:
        return_period_yr: Return period in years (e.g. 2, 10, 100).
        depth_source:     One of "bulletin_75" (default) or "atlas_14".

    Returns:
        Design depth in inches.

    Raises:
        ValueError: If depth_source is invalid or return_period_yr is not in table.
    """
    if depth_source not in VALID_DEPTH_SOURCES:
        raise ValueError(
            f"Invalid depth_source={depth_source!r}. "
            f"Valid options: {VALID_DEPTH_SOURCES}"
        )
    table = _DEPTH_TABLES[depth_source]
    if return_period_yr not in table:
        available = sorted(table.keys())
        raise ValueError(
            f"Return period {return_period_yr}yr not in {depth_source} table. "
            f"Available: {available}"
        )
    return table[return_period_yr]


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class StormEvent:
    """A single AORC storm event from the catalog.

    Attributes:
        storm_id:             Unique storm identifier.
        start_time:           Observed storm start (UTC).
        end_time:             Observed storm end (UTC).
        sim_start:            Recommended simulation start (padded).
        sim_end:              Recommended simulation end (padded).
        total_depth_in:       Total storm precipitation depth in inches.
        peak_intensity_in_hr: Peak hourly intensity in inches per hour.
        duration_hours:       Total storm duration in hours.
        wet_hours:            Number of hours with measurable precipitation.
        rank:                 Rank by total depth (1 = largest).
        year:                 Calendar year of the storm.
        netcdf_path:          Path to downloaded NetCDF file, if available.
    """
    storm_id: int
    start_time: datetime
    end_time: datetime
    sim_start: datetime
    sim_end: datetime
    total_depth_in: float
    peak_intensity_in_hr: float
    duration_hours: int
    wet_hours: int
    rank: int
    year: int
    netcdf_path: Optional[Path] = None


@dataclass
class PrecipitationResult:
    """Result of the precipitation stage for one return period.

    Attributes:
        storm:          Selected StormEvent metadata.
        netcdf_path:    Path to the downloaded AORC NetCDF file.
        bounds:         (west, south, east, north) bounding box in WGS84.
        target_rp_yr:   Target return period in years (or None if unmatched).
        mock:           True if produced in mock mode.
    """
    storm: StormEvent
    netcdf_path: Path
    bounds: tuple          # (west, south, east, north) WGS84
    target_rp_yr: Optional[int]
    mock: bool = False


# ── Dependency Check ──────────────────────────────────────────────────────────

def check_aorc_dependencies() -> dict:
    """Check whether all AORC optional dependencies are importable.

    Returns:
        Dict with keys "xarray", "zarr", "s3fs", "rioxarray", "all_available".
        Each key maps to True if the package can be imported, False otherwise.
    """
    results = {}
    for pkg in ("xarray", "zarr", "s3fs", "rioxarray"):
        try:
            __import__(pkg)
            results[pkg] = True
        except ImportError:
            results[pkg] = False
    results["all_available"] = all(results.values())
    return results


# ── Storm Catalog ─────────────────────────────────────────────────────────────

def catalog_storms(
    bounds: tuple,
    years: list,
    percentile_threshold: float = 80.0,
    mock: bool = False,
) -> pd.DataFrame:
    """Catalog AORC storm events for the given bounding box and years.

    Args:
        bounds:               (west, south, east, north) in WGS84.
        years:                List of integer years to search.
        percentile_threshold: Minimum storm percentile to include (default 80.0).
        mock:                 If True, return a synthetic 3-row DataFrame.

    Returns:
        DataFrame with columns: storm_id, start_time, end_time, sim_start,
        sim_end, total_depth_in, peak_intensity_in_hr, duration_hours,
        wet_hours, rank, year. Sorted by rank (1 = largest depth).
    """
    if mock:
        df = pd.DataFrame([
            {
                "storm_id": 1001,
                "start_time": datetime(2022, 8, 3, 6),
                "end_time": datetime(2022, 8, 3, 18),
                "sim_start": datetime(2022, 8, 3, 0),
                "sim_end": datetime(2022, 8, 4, 0),
                "total_depth_in": 3.8,
                "peak_intensity_in_hr": 1.2,
                "duration_hours": 12,
                "wet_hours": 10,
                "rank": 1,
                "year": 2022,
            },
            {
                "storm_id": 1002,
                "start_time": datetime(2021, 5, 22, 12),
                "end_time": datetime(2021, 5, 23, 0),
                "sim_start": datetime(2021, 5, 22, 6),
                "sim_end": datetime(2021, 5, 23, 6),
                "total_depth_in": 2.4,
                "peak_intensity_in_hr": 0.8,
                "duration_hours": 12,
                "wet_hours": 9,
                "rank": 2,
                "year": 2021,
            },
            {
                "storm_id": 1003,
                "start_time": datetime(2020, 7, 15, 8),
                "end_time": datetime(2020, 7, 15, 20),
                "sim_start": datetime(2020, 7, 15, 0),
                "sim_end": datetime(2020, 7, 16, 0),
                "total_depth_in": 1.2,
                "peak_intensity_in_hr": 0.4,
                "duration_hours": 12,
                "wet_hours": 7,
                "rank": 3,
                "year": 2020,
            },
        ])
        logger.info(f"Storm catalog: {len(df)} storms across {len(years)} years (mock mode)")
        return df

    from ras_commander.precip import PrecipAorc

    frames = []
    for year in years:
        try:
            year_df = PrecipAorc.get_storm_catalog(
                bounds, year, percentile_threshold=percentile_threshold
            )
            if year_df is not None and len(year_df) > 0:
                year_df = year_df.copy()
                year_df["year"] = year
                frames.append(year_df)
        except Exception as exc:
            logger.warning(f"Storm catalog failed for year {year}: {exc}")

    if not frames:
        cols = [
            "storm_id", "start_time", "end_time", "sim_start", "sim_end",
            "total_depth_in", "peak_intensity_in_hr", "duration_hours",
            "wet_hours", "rank", "year",
        ]
        return pd.DataFrame(columns=cols)

    df = pd.concat(frames, ignore_index=True)

    # Re-rank globally by total_depth_in descending (rank 1 = largest)
    df = df.sort_values("total_depth_in", ascending=False).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)

    logger.info(f"Storm catalog: {len(df)} storms across {len(years)} years")
    return df


# ── Design Storm Selection ─────────────────────────────────────────────────────

def select_design_storm(
    catalog_df: pd.DataFrame,
    target_depth_in: float,
    tolerance_pct: float = 0.3,
) -> Optional[pd.Series]:
    """Select the catalog storm whose depth is closest to target_depth_in.

    Args:
        catalog_df:     DataFrame from catalog_storms().
        target_depth_in: Target storm depth in inches.
        tolerance_pct:  Maximum allowed fractional deviation from target
                        (default 0.3 = 30%).

    Returns:
        pd.Series of the best-matching row, or None if no match within
        tolerance.
    """
    if catalog_df.empty:
        logger.info("select_design_storm: catalog is empty — no match")
        return None

    idx = (catalog_df["total_depth_in"] - target_depth_in).abs().idxmin()
    best = catalog_df.loc[idx]
    deviation = abs(best["total_depth_in"] - target_depth_in) / target_depth_in

    if deviation <= tolerance_pct:
        logger.info(
            f"select_design_storm: matched storm_id={best['storm_id']} "
            f"depth={best['total_depth_in']:.2f}in "
            f"(target={target_depth_in:.2f}in, dev={deviation:.1%})"
        )
        return best

    logger.info(
        f"select_design_storm: no match within {tolerance_pct:.0%} tolerance "
        f"(closest={best['total_depth_in']:.2f}in, target={target_depth_in:.2f}in, "
        f"dev={deviation:.1%})"
    )
    return None


# ── Storm Download ────────────────────────────────────────────────────────────

def download_storm(
    storm_row: pd.Series,
    bounds: tuple,
    output_dir,
    mock: bool = False,
) -> Path:
    """Download AORC NetCDF for the given storm event.

    Args:
        storm_row:   pd.Series with sim_start, sim_end, storm_id, start_time.
        bounds:      (west, south, east, north) in WGS84.
        output_dir:  Root output directory; file written to
                     {output_dir}/precipitation/.
        mock:        If True, write a placeholder file instead of downloading.

    Returns:
        Path to the downloaded (or mock) NetCDF file.
    """
    output_dir = Path(output_dir)
    precip_dir = output_dir / "precipitation"
    precip_dir.mkdir(parents=True, exist_ok=True)

    start_str = storm_row["start_time"].strftime("%Y%m%d")
    output_path = precip_dir / f"aorc_{storm_row['storm_id']}_{start_str}.nc"

    if mock:
        output_path.write_bytes(b"MOCK_NETCDF_AORC")
        logger.info(f"download_storm: mock file written to {output_path}")
        return output_path

    from ras_commander.precip import PrecipAorc

    logger.info(
        f"download_storm: downloading storm_id={storm_row['storm_id']} "
        f"({storm_row['sim_start']} → {storm_row['sim_end']}) …"
    )
    PrecipAorc.download(
        bounds,
        storm_row["sim_start"],
        storm_row["sim_end"],
        output_path,
    )
    logger.info(f"download_storm: saved to {output_path}")
    return output_path


# ── Stage Runner ──────────────────────────────────────────────────────────────

def run_precipitation_stage(
    bounds: tuple,
    output_dir,
    target_return_periods: Optional[list] = None,
    years: Optional[list] = None,
    depth_source: str = "bulletin_75",
    mock: bool = False,
) -> dict:
    """Run the full precipitation stage for a set of return periods.

    Catalogs AORC storms, selects design events whose total depth best
    matches the design depth for each return period from the given source.

    Args:
        bounds:                (west, south, east, north) in WGS84.
        output_dir:            Root output directory.
        target_return_periods: Return periods in years (default: [2, 10, 100]).
        years:                 Calendar years to search (default: last 5 years).
        depth_source:          Precipitation frequency source for design depths.
                               "bulletin_75" (default, ISWS standard) or "atlas_14".
        mock:                  If True, run without network or S3 access.

    Returns:
        Dict mapping each return period (int) to a PrecipitationResult, or
        None if no matching storm was found for that period.
    """
    if depth_source not in VALID_DEPTH_SOURCES:
        raise ValueError(
            f"Invalid depth_source={depth_source!r}. "
            f"Valid options: {VALID_DEPTH_SOURCES}"
        )
    if target_return_periods is None:
        target_return_periods = [2, 10, 100]
    if years is None:
        now = datetime.now()
        years = list(range(now.year - 5, now.year))

    logger.info(
        f"[CALC] Precipitation depth source: {depth_source} "
        f"(RPs: {target_return_periods})"
    )

    catalog_df = catalog_storms(bounds, years, mock=mock)

    output_dir = Path(output_dir)
    results: dict = {}

    for rp in target_return_periods:
        target_depth = get_design_depth(rp, depth_source)
        logger.info(
            f"[CALC] Design depth: T={rp}yr → {target_depth:.1f}in "
            f"(source: {depth_source}) [VALID]"
        )
        storm_row = select_design_storm(catalog_df, target_depth)

        if storm_row is None:
            logger.warning(
                f"run_precipitation_stage: no storm matched for T={rp}yr "
                f"(target_depth={target_depth:.1f}in)"
            )
            results[rp] = None
            continue

        nc_path = download_storm(storm_row, bounds, output_dir, mock=mock)

        storm_event = StormEvent(
            storm_id=int(storm_row["storm_id"]),
            start_time=storm_row["start_time"],
            end_time=storm_row["end_time"],
            sim_start=storm_row["sim_start"],
            sim_end=storm_row["sim_end"],
            total_depth_in=float(storm_row["total_depth_in"]),
            peak_intensity_in_hr=float(storm_row.get("peak_intensity_in_hr", 0.0)),
            duration_hours=int(storm_row.get("duration_hours", 0)),
            wet_hours=int(storm_row.get("wet_hours", 0)),
            rank=int(storm_row["rank"]),
            year=int(storm_row["year"]),
            netcdf_path=nc_path,
        )

        results[rp] = PrecipitationResult(
            storm=storm_event,
            netcdf_path=nc_path,
            bounds=bounds,
            target_rp_yr=rp,
            mock=mock,
        )

    matched = sum(v is not None for v in results.values())
    logger.info(
        f"run_precipitation_stage: {matched}/{len(target_return_periods)} "
        "return periods matched"
    )
    return results
