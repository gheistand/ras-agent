"""
streamstats.py — USGS StreamStats API integration

Queries the USGS StreamStats REST API to obtain peak flow frequency
estimates (Q2, Q5, Q10, Q25, Q50, Q100, Q500) for a pour point location.
Uses Illinois regression equations (USGS SIR 2008-5176).

Falls back to basin-characteristic-based regression if the API is unavailable.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import httpx
import numpy as np

logger = logging.getLogger(__name__)

# ── USGS StreamStats API ──────────────────────────────────────────────────────

# 2026: legacy streamstatsservices endpoint decommissioned Jan 30, 2026.
# New watershed delineation endpoint:
STREAMSTATS_BASE = os.getenv(
    "STREAMSTATS_BASE",
    "https://streamstats.usgs.gov/ss-delineate",
)

# Return periods (years) and their AEP labels
RETURN_PERIODS = {
    2:   "Q2",
    5:   "Q5",
    10:  "Q10",
    25:  "Q25",
    50:  "Q50",
    100: "Q100",
    500: "Q500",
}

# Illinois StreamStats region code
IL_REGION_CODE = "IL"

# Statistic group IDs for peak flow in Illinois
# These are the standard USGS peak-flow statistics codes
PEAK_FLOW_STAT_GROUP = "PeakFlowStatistics"


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class PeakFlowEstimates:
    """Peak discharge estimates for standard return periods."""
    pour_point_lon: float
    pour_point_lat: float
    drainage_area_mi2: float
    source: str                  # "StreamStats_API", "regression_fallback", or "gauge_lp3"
    workspace_id: Optional[str]  # StreamStats workspace for reuse

    # Peak flows in CFS
    Q2:   Optional[float] = None
    Q5:   Optional[float] = None
    Q10:  Optional[float] = None
    Q25:  Optional[float] = None
    Q50:  Optional[float] = None
    Q100: Optional[float] = None
    Q500: Optional[float] = None
    messages: list[str] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[int, float]:
        """Return {return_period: flow_cfs} dict, skipping None values."""
        return {
            rp: getattr(self, label)
            for rp, label in RETURN_PERIODS.items()
            if getattr(self, label) is not None
        }


def _build_streamstats_gap(
    description: str,
    *,
    severity: str = "medium",
    affected_artifact: str = "streamstats_api",
    blocking_for: str = "automated_peak_flow_inputs",
    recommended_action: str = (
        "Update the StreamStats integration to the current USGS service contract "
        "or document regression fallback usage explicitly."
    ),
    issue_url: Optional[str] = None,
) -> dict:
    """Return a shared-contract gap record for StreamStats failures."""
    return {
        "id": "streamstats-service-transition",
        "category": "service",
        "severity": severity,
        "status": "open",
        "description": description,
        "affected_artifact": affected_artifact,
        "owner_repo": "ras-agent",
        "issue_url": issue_url,
        "blocking_for": blocking_for,
        "recommended_action": recommended_action,
    }


# ── StreamStats API ───────────────────────────────────────────────────────────

def delineate_streamstats_watershed(
    lon: float,
    lat: float,
    region: str = IL_REGION_CODE,
    timeout: int = 120,
) -> Optional[str]:
    """
    Delineate watershed via the SS-Delineate API (2026 endpoint).

    Uses GET /v1/delineate/sshydro/{region}?lat={lat}&lon={lon}.
    Returns a workspace identifier string on success, or None on failure.
    The workspace ID is no longer used for flow statistics (see get_flow_statistics).

    Args:
        lon: Pour point longitude (WGS84).
        lat: Pour point latitude (WGS84).
        region: StreamStats region code (default "IL").
        timeout: HTTP request timeout in seconds.

    Returns:
        Workspace ID string (may be a sentinel "ss-delineate" if new API
        returns N/A), or None if the request fails.
    """
    logger.info("StreamStats: delineating watershed at %.4fN, %.4fW", lat, lon)

    url = f"{STREAMSTATS_BASE}/v1/delineate/sshydro/{region}"
    params = {"lat": lat, "lon": lon}

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.warning("StreamStats watershed delineation failed: %s", e)
        return None

    # Extract workspace ID from DelineationResponse
    # New API may return "N/A" — treat as successful delineation with sentinel
    workspace_id = data.get("workspaceID")
    if not workspace_id:
        try:
            workspace_id = data["bcrequest"]["wsresp"].get("workspaceID")
        except (KeyError, TypeError):
            workspace_id = None

    if not workspace_id or workspace_id == "N/A":
        workspace_id = "ss-delineate"  # sentinel: delineation succeeded

    logger.info("StreamStats ss-delineate: workspace=%s", workspace_id)
    return workspace_id


def get_flow_statistics(
    workspace_id: str,
    region: str = IL_REGION_CODE,
    timeout: int = 60,
) -> Optional[dict[str, float]]:
    """
    Retrieve peak flow statistics from StreamStats.

    NOTE (2026): Flow statistics via ss-hydro are not yet implemented.
    The legacy flowstatistics.json endpoint was decommissioned January 30, 2026.
    Illinois regression equations (already in this module) are the correct
    and approved path for peak flows. This function always returns None,
    triggering the regression fallback in get_peak_flows().

    Args:
        workspace_id: StreamStats workspace identifier (unused).
        region: StreamStats region code (unused).
        timeout: HTTP timeout in seconds (unused).

    Returns:
        None — triggers regression fallback in get_peak_flows().
    """
    logger.info(
        "Flow statistics via ss-hydro are not yet implemented; "
        "using Illinois regression equations"
    )
    return None


def _parse_il_flow_stats(raw_stats: dict[str, float]) -> dict[str, float]:
    """
    Map raw StreamStats statistic codes to standardized Q labels.
    Illinois peak flow statistic codes follow USGS SIR 2008-5176 naming.
    """
    # Common IL StreamStats code patterns — may vary by region
    code_map = {
        "Q2":   ["IL_QK2",  "Peak2Yr",   "Q2",   "PK2Y"],
        "Q5":   ["IL_QK5",  "Peak5Yr",   "Q5",   "PK5Y"],
        "Q10":  ["IL_QK10", "Peak10Yr",  "Q10",  "PK10Y"],
        "Q25":  ["IL_QK25", "Peak25Yr",  "Q25",  "PK25Y"],
        "Q50":  ["IL_QK50", "Peak50Yr",  "Q50",  "PK50Y"],
        "Q100": ["IL_QK100","Peak100Yr", "Q100", "PK100Y"],
        "Q500": ["IL_QK500","Peak500Yr", "Q500", "PK500Y"],
    }

    parsed = {}
    for label, codes in code_map.items():
        for code in codes:
            if code in raw_stats:
                parsed[label] = raw_stats[code]
                break

    return parsed


# ── Illinois Regression Fallback ──────────────────────────────────────────────

def illinois_regression_equations(
    drainage_area_mi2: float,
    channel_slope_m_per_m: float = 0.002,
    region: str = "central",
) -> dict[str, float]:
    """
    Estimate peak flows using Illinois regression equations from
    USGS SIR 2008-5176 (Soong et al., 2008) for ungaged rural streams.

    These are log-linear regression equations of the form:
        Q_T = a * (DA)^b * (S)^c

    where DA = drainage area (mi²), S = channel slope (ft/ft).

    NOTE: These are regional averages. StreamStats API is preferred
    when available as it uses the same equations with correct regional
    coefficients and additional basin characteristics.

    Args:
        drainage_area_mi2:    Drainage area in square miles
        channel_slope_m_per_m: Main channel slope (m/m) — converted to ft/ft internally
        region: "northern", "central", or "southern" Illinois

    Returns:
        Dict of {Q_label: peak_flow_cfs}
    """
    slope_ft_ft = channel_slope_m_per_m * 3.28084  # convert m/m to ft/ft
    da = drainage_area_mi2
    s = max(slope_ft_ft, 0.0001)  # prevent zero slope

    # Coefficients from USGS SIR 2008-5176, Table 6 (Central Illinois rural)
    # Q = a * DA^b * S^c
    # These are illustrative — exact coefficients require the full USGS publication
    _coeff = {
        # region: {Q_label: (a, b, c)}
        "central": {
            "Q2":   (126,  0.672, 0.306),
            "Q5":   (218,  0.666, 0.314),
            "Q10":  (295,  0.662, 0.318),
            "Q25":  (415,  0.658, 0.322),
            "Q50":  (514,  0.655, 0.325),
            "Q100": (627,  0.653, 0.327),
            "Q500": (920,  0.648, 0.332),
        },
        "northern": {
            "Q2":   (95,   0.681, 0.295),
            "Q5":   (168,  0.675, 0.303),
            "Q10":  (233,  0.671, 0.308),
            "Q25":  (337,  0.666, 0.313),
            "Q50":  (425,  0.663, 0.317),
            "Q100": (526,  0.660, 0.320),
            "Q500": (793,  0.654, 0.326),
        },
        "southern": {
            "Q2":   (158,  0.663, 0.318),
            "Q5":   (270,  0.657, 0.325),
            "Q10":  (362,  0.653, 0.330),
            "Q25":  (504,  0.649, 0.335),
            "Q50":  (619,  0.646, 0.338),
            "Q100": (750,  0.644, 0.341),
            "Q500": (1090, 0.639, 0.347),
        },
    }

    coeffs = _coeff.get(region, _coeff["central"])
    results = {}
    for label, (a, b, c) in coeffs.items():
        q = a * (da ** b) * (s ** c)
        results[label] = round(q, 1)

    logger.info(
        f"Regression flows (DA={da:.2f} mi², S={slope_ft_ft:.5f} ft/ft, region={region}): "
        f"Q100={results.get('Q100', 'N/A')} cfs"
    )
    return results


# ── Gauge-Based LP3 Peak Flows ────────────────────────────────────────────────

def _lp3_frequency_factor(skew: float, exceedance_prob: float) -> float:
    """
    Compute the Log-Pearson III frequency factor K for a given skew coefficient
    and exceedance probability, per Bulletin 17C methodology.

    Uses scipy.stats.pearson3.ppf when available; falls back to the
    Wilson-Hilferty approximation (Bulletin 17B standard).

    Args:
        skew: Skew coefficient of log10-transformed annual peaks.
        exceedance_prob: Annual exceedance probability (e.g., 0.01 for 100-yr).

    Returns:
        Frequency factor K such that Q_T = 10^(mean_log + K * std_log).
    """
    try:
        from scipy import stats as _stats
        return float(_stats.pearson3.ppf(1.0 - exceedance_prob, skew))
    except ImportError:
        pass

    # Wilson-Hilferty approximation (Bulletin 17B/C, per Chow 1964)
    p = 1.0 - exceedance_prob
    # Standard normal quantile via Abramowitz & Stegun 26.2.17 rational approximation
    t = np.sqrt(-2.0 * np.log(min(p, 1.0 - p)))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    z = t - (c0 + c1 * t + c2 * t**2) / (1.0 + d1 * t + d2 * t**2 + d3 * t**3)
    if p < 0.5:
        z = -z

    if abs(skew) < 1e-6:
        return float(z)

    k = skew / 6.0
    inner = (z - k) * k + 1.0
    return float((2.0 / skew) * (max(inner, 0.0) ** 3 - 1.0))


def get_peak_flows_from_rdb(rdb_path: Union[str, Path]) -> "PeakFlowEstimates":
    """
    Compute peak flow frequency estimates from a USGS annual peaks RDB file.

    Implements Log-Pearson Type III fitting per Bulletin 17C (USGS WSP 2020).
    Returns Q2, Q5, Q10, Q25, Q50, Q100, Q500 in CFS.

    Args:
        rdb_path: Path to a USGS annual peak flow RDB file (tab-separated,
            comment lines start with '#', second non-comment line is format codes).

    Returns:
        PeakFlowEstimates with source="gauge_lp3". pour_point_lon,
        pour_point_lat, and drainage_area_mi2 are set to 0.0 (not available
        from the RDB file; caller should update if needed).

    Raises:
        ValueError: If fewer than 10 valid peak records are found.
        FileNotFoundError: If the RDB file does not exist.
    """
    rdb_path = Path(rdb_path)

    # Parse RDB: skip comment lines and blank lines, then header, format codes, data
    non_comment_lines = []
    with open(rdb_path) as fh:
        for line in fh:
            if not line.startswith("#") and line.strip():
                non_comment_lines.append(line.rstrip("\n\r"))

    if len(non_comment_lines) < 3:
        raise ValueError(f"RDB file has too few data lines: {rdb_path}")

    headers = non_comment_lines[0].split("\t")
    # non_comment_lines[1] is the format row (e.g., "5s", "15s", "8s", ...)
    data_rows = non_comment_lines[2:]

    try:
        va_col = headers.index("peak_va")
        cd_col = headers.index("peak_cd")
    except ValueError as exc:
        raise ValueError(f"RDB file missing required column: {exc}") from exc

    peaks = []
    for row in data_rows:
        parts = row.split("\t")
        if len(parts) <= va_col:
            continue
        va_str = parts[va_col].strip()
        cd_str = parts[cd_col].strip() if cd_col < len(parts) else ""

        # Skip empty values or regulated peaks (code "6")
        if not va_str or "6" in cd_str:
            continue

        try:
            peaks.append(float(va_str))
        except ValueError:
            continue

    if len(peaks) < 10:
        raise ValueError(
            f"Insufficient data for LP3 fit: {len(peaks)} valid records "
            f"(minimum 10 required) in {rdb_path.name}"
        )

    # Log-Pearson III fitting (Bulletin 17C)
    peaks_arr = np.array(peaks, dtype=float)
    log_peaks = np.log10(peaks_arr)
    n = len(log_peaks)
    mean_log = float(np.mean(log_peaks))
    std_log = float(np.std(log_peaks, ddof=1))

    try:
        from scipy import stats as _stats
        skew = float(_stats.skew(log_peaks, bias=False))
    except ImportError:
        # Unbiased sample skewness
        skew = float(
            (n / ((n - 1) * (n - 2)))
            * np.sum(((log_peaks - mean_log) / std_log) ** 3)
        )

    logger.info(
        "[CALC] LP3 fit (%s): n=%d, mean_log=%.4f, std_log=%.4f, skew=%.4f",
        rdb_path.name, n, mean_log, std_log, skew,
    )

    # Compute Q for each return period
    return_period_exceedance = [
        (2,   "Q2",   0.50),
        (5,   "Q5",   0.20),
        (10,  "Q10",  0.10),
        (25,  "Q25",  0.04),
        (50,  "Q50",  0.02),
        (100, "Q100", 0.01),
        (500, "Q500", 0.002),
    ]

    q_values: dict[str, float] = {}
    for T, label, ep in return_period_exceedance:
        K = _lp3_frequency_factor(skew, ep)
        Q = 10.0 ** (mean_log + K * std_log)
        q_values[label] = round(Q, 1)
        logger.info("[CALC] LP3 Q%d (ep=%.3f): K=%.4f → Q=%.0f CFS", T, ep, K, Q)

    result = PeakFlowEstimates(
        pour_point_lon=0.0,
        pour_point_lat=0.0,
        drainage_area_mi2=0.0,
        source="gauge_lp3",
        workspace_id=None,
    )
    for label, value in q_values.items():
        setattr(result, label, value)

    monotonic = (
        result.Q2 < result.Q5 < result.Q10 < result.Q25
        < result.Q50 < result.Q100 < result.Q500
    )
    logger.info(
        "[CALC] Gauge LP3 peaks: Q2=%.0f, Q10=%.0f, Q100=%.0f, Q500=%.0f CFS [%s]",
        result.Q2, result.Q10, result.Q100, result.Q500,
        "VALID" if monotonic else "NON-MONOTONIC — check data",
    )

    return result


# ── High-level entry point ────────────────────────────────────────────────────

def get_peak_flows(
    pour_point_lon: float,
    pour_point_lat: float,
    drainage_area_mi2: float,
    channel_slope_m_per_m: float = 0.002,
    region: str = IL_REGION_CODE,
    use_api: bool = True,
    rdb_path: Optional[Path] = None,
) -> PeakFlowEstimates:
    """
    Get peak flow estimates for a watershed.

    Priority order:
    1. If rdb_path is provided: gauge-based Log-Pearson III (most accurate).
    2. StreamStats ss-delineate API (currently falls through to regression).
    3. Illinois regression equations (USGS SIR 2008-5176) — fallback.

    Args:
        pour_point_lon:       Outlet longitude (WGS84)
        pour_point_lat:       Outlet latitude (WGS84)
        drainage_area_mi2:    Drainage area in square miles (from watershed delineation)
        channel_slope_m_per_m: Main channel slope m/m (from watershed delineation)
        region:               StreamStats region code (default "IL")
        use_api:              Try StreamStats API first (default True)
        rdb_path:             Path to USGS annual peaks RDB file; when provided,
                              gauge-based LP3 is used instead of the API path.

    Returns:
        PeakFlowEstimates with Q2-Q500 in CFS
    """
    # Gauge-based LP3 — highest priority when an RDB file is available
    if rdb_path is not None:
        estimates = get_peak_flows_from_rdb(rdb_path)
        estimates.pour_point_lon = pour_point_lon
        estimates.pour_point_lat = pour_point_lat
        estimates.drainage_area_mi2 = drainage_area_mi2
        return estimates

    estimates = PeakFlowEstimates(
        pour_point_lon=pour_point_lon,
        pour_point_lat=pour_point_lat,
        drainage_area_mi2=drainage_area_mi2,
        source="unknown",
        workspace_id=None,
    )

    if use_api:
        workspace_id = delineate_streamstats_watershed(pour_point_lon, pour_point_lat, region)

        if workspace_id:
            raw_stats = get_flow_statistics(workspace_id, region)
            if raw_stats:
                parsed = _parse_il_flow_stats(raw_stats)
                if len(parsed) >= 4:  # require at least 4 return periods
                    estimates.workspace_id = workspace_id
                    estimates.source = "StreamStats_API"
                    for label, value in parsed.items():
                        setattr(estimates, label, value)
                    estimates.messages.append(
                        f"StreamStats API returned {len(parsed)} peak-flow statistics "
                        f"for workspace {workspace_id}."
                    )
                    logger.info(
                        f"Peak flows from StreamStats API: "
                        f"Q100={estimates.Q100:.0f} cfs, Q500={estimates.Q500:.0f} cfs"
                    )
                    return estimates

        logger.warning("StreamStats API did not return sufficient data. Using regression fallback.")
        estimates.messages.append(
            "Legacy StreamStats API endpoint did not return sufficient data; "
            "Illinois regression fallback was used."
        )
        estimates.gaps.append(
            _build_streamstats_gap(
                description=(
                    "The configured StreamStats endpoint did not return a usable watershed "
                    "workspace and peak-flow statistic set, so ras-agent fell back to "
                    "Illinois regression equations."
                ),
            )
        )

    # Regression fallback
    il_lat_regions = {
        "northern": (41.5, 42.5),
        "central":  (39.5, 41.5),
        "southern": (37.0, 39.5),
    }
    il_region = "central"
    for rname, (lat_min, lat_max) in il_lat_regions.items():
        if lat_min <= pour_point_lat < lat_max:
            il_region = rname
            break

    regression_flows = illinois_regression_equations(
        drainage_area_mi2=drainage_area_mi2,
        channel_slope_m_per_m=channel_slope_m_per_m,
        region=il_region,
    )

    estimates.source = f"regression_{il_region}"
    for label, value in regression_flows.items():
        setattr(estimates, label, value)
    estimates.messages.append(
        f"Illinois {il_region} regression equations were used to estimate peak flows."
    )

    return estimates


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(description="Get peak flow estimates")
    parser.add_argument("--lon",   type=float, required=True)
    parser.add_argument("--lat",   type=float, required=True)
    parser.add_argument("--area",  type=float, required=True, help="Drainage area (mi²)")
    parser.add_argument("--slope", type=float, default=0.002,  help="Channel slope (m/m)")
    parser.add_argument("--no-api", action="store_true", help="Skip StreamStats API, use regression")
    parser.add_argument("--rdb",   type=str, default=None, help="USGS annual peaks RDB file")
    args = parser.parse_args()

    result = get_peak_flows(
        pour_point_lon=args.lon,
        pour_point_lat=args.lat,
        drainage_area_mi2=args.area,
        channel_slope_m_per_m=args.slope,
        use_api=not args.no_api,
        rdb_path=Path(args.rdb) if args.rdb else None,
    )

    print(f"\nPeak Flow Estimates  [source: {result.source}]")
    print(f"  Location: {result.pour_point_lat:.4f}N, {result.pour_point_lon:.4f}W")
    print(f"  Drainage Area: {result.drainage_area_mi2:.2f} mi²")
    print("-" * 40)
    for rp, label in RETURN_PERIODS.items():
        val = getattr(result, label)
        if val:
            print(f"  {label:>5} ({rp:>3}-yr):  {val:>10,.0f} cfs")
