"""
storm_qc.py — GHCND observed precipitation QC for AORC storm catalog

Cross-checks AORC storm total depths against NOAA GHCND daily observations
from nearby stations. Flags storms as "ok", "high", "low", or "no_obs".

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional

import numpy as np
import pandas as pd
import requests
from loguru import logger


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class GhcndStation:
    """A NOAA GHCND weather station.

    Attributes:
        station_id:   GHCND station ID (e.g. "GHCND:USW00094870").
        name:         Station name.
        lat:          Latitude (decimal degrees).
        lon:          Longitude (decimal degrees).
        elevation_m:  Elevation in meters.
        datacoverage: Fraction of available records (0.0–1.0).
    """
    station_id: str
    name: str
    lat: float
    lon: float
    elevation_m: float
    datacoverage: float


@dataclass
class StormObservation:
    """A single daily precipitation observation from GHCND.

    Attributes:
        station_id:   GHCND station ID.
        date:         Observation date.
        prcp_inches:  Observed daily precipitation in inches.
        source:       Data source label (default "ghcnd").
    """
    station_id: str
    date: date
    prcp_inches: float
    source: str = "ghcnd"


# ── Station Discovery ─────────────────────────────────────────────────────────

def find_stations(
    bounds: tuple,
    max_stations: int = 5,
    noaa_token: Optional[str] = None,
) -> List[GhcndStation]:
    """Find nearby NOAA GHCND stations with precipitation records.

    Args:
        bounds:       (west, south, east, north) in WGS84.
        max_stations: Maximum number of stations to return (default 5).
        noaa_token:   NOAA CDO API token. Falls back to NOAA_CDO_TOKEN env var.

    Returns:
        List of GhcndStation objects sorted by datacoverage descending.
        Returns [] if no token is available or on any HTTP error.
    """
    if noaa_token is None:
        noaa_token = os.environ.get("NOAA_CDO_TOKEN")

    if not noaa_token:
        logger.info("NOAA_CDO_TOKEN not set — skipping station discovery")
        return []

    west, south, east, north = bounds
    url = "https://www.ncei.noaa.gov/cdo-web/api/v2/stations"
    params = {
        "datatypeid": "PRCP",
        "extent": f"{south},{west},{north},{east}",
        "limit": 25,
        "sortfield": "datacoverage",
        "sortorder": "desc",
    }
    headers = {"token": noaa_token}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning(f"find_stations: NOAA CDO request failed: {exc}")
        return []

    stations = []
    for item in data.get("results", [])[:max_stations]:
        try:
            stations.append(
                GhcndStation(
                    station_id=item["id"],
                    name=item.get("name", ""),
                    lat=float(item.get("latitude", 0.0)),
                    lon=float(item.get("longitude", 0.0)),
                    elevation_m=float(item.get("elevation", 0.0)),
                    datacoverage=float(item.get("datacoverage", 0.0)),
                )
            )
        except (KeyError, ValueError) as exc:
            logger.warning(f"find_stations: skipping malformed station record: {exc}")

    logger.info(f"find_stations: found {len(stations)} stations near {bounds}")
    return stations


# ── Observed Precipitation ────────────────────────────────────────────────────

def get_observed_precip(
    station_ids: List[str],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Fetch GHCND daily precipitation totals from the NCEI daily-summaries API.

    Args:
        station_ids: List of GHCND station IDs (e.g. ["GHCND:USW00094870"]).
        start_date:  First date of the period (inclusive).
        end_date:    Last date of the period (inclusive).

    Returns:
        DataFrame with columns: station_id (str), date (date), prcp_inches (float).
        Returns an empty DataFrame with those columns on any error.
    """
    _empty = pd.DataFrame(columns=["station_id", "date", "prcp_inches"])

    if not station_ids:
        return _empty

    url = "https://www.ncei.noaa.gov/access/services/data/v1"
    params = {
        "dataset": "daily-summaries",
        "dataTypes": "PRCP",
        "stations": ",".join(station_ids),
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "format": "json",
        "units": "standard",
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        logger.warning(f"get_observed_precip: NCEI request failed: {exc}")
        return _empty

    if not raw:
        logger.warning("get_observed_precip: empty response from NCEI")
        return _empty

    rows = []
    for record in raw:
        try:
            prcp_val = record.get("PRCP")
            if prcp_val is None:
                continue
            rows.append({
                "station_id": record["STATION"],
                "date": date.fromisoformat(record["DATE"][:10]),
                "prcp_inches": float(prcp_val),
            })
        except (KeyError, ValueError) as exc:
            logger.warning(f"get_observed_precip: skipping malformed record: {exc}")

    if not rows:
        return _empty

    return pd.DataFrame(rows)


# ── Catalog Comparison ────────────────────────────────────────────────────────

def compare_storm_depths(
    storm_catalog_df: pd.DataFrame,
    bounds: tuple,
    noaa_token: Optional[str] = None,
    mock: bool = False,
) -> pd.DataFrame:
    """Compare AORC storm totals against GHCND observed precipitation.

    Adds three columns to a copy of storm_catalog_df:
    - ghcnd_depth_in: mean observed storm total across available stations
    - ghcnd_stations_used: number of stations contributing data
    - depth_ratio: total_depth_in / ghcnd_depth_in (NaN if no observations)

    Args:
        storm_catalog_df: DataFrame from catalog_storms().
        bounds:           (west, south, east, north) in WGS84.
        noaa_token:       NOAA CDO API token (falls back to env var).
        mock:             If True, synthesize ghcnd_depth_in = total_depth_in * 0.9.

    Returns:
        Copy of storm_catalog_df with the three new columns appended.
    """
    result = storm_catalog_df.copy()

    if mock:
        result["ghcnd_depth_in"] = result["total_depth_in"] * 0.9
        result["ghcnd_stations_used"] = 1
        result["depth_ratio"] = result["total_depth_in"] / result["ghcnd_depth_in"]
        return result

    stations = find_stations(bounds, noaa_token=noaa_token)
    station_ids = [s.station_id for s in stations]

    ghcnd_depths = []
    stations_used = []
    depth_ratios = []

    for _, storm in result.iterrows():
        if not station_ids:
            ghcnd_depths.append(float("nan"))
            stations_used.append(0)
            depth_ratios.append(float("nan"))
            continue

        try:
            obs_df = get_observed_precip(
                station_ids,
                storm["start_time"].date(),
                storm["end_time"].date(),
            )
        except Exception as exc:
            logger.warning(
                f"compare_storm_depths: observation fetch failed for "
                f"storm_id={storm.get('storm_id', '?')}: {exc}"
            )
            ghcnd_depths.append(float("nan"))
            stations_used.append(len(station_ids))
            depth_ratios.append(float("nan"))
            continue

        if obs_df.empty:
            ghcnd_depths.append(float("nan"))
            stations_used.append(len(station_ids))
            depth_ratios.append(float("nan"))
            continue

        # Mean per day across stations, then sum over storm days
        daily_means = obs_df.groupby("date")["prcp_inches"].mean()
        total_obs = daily_means.sum()

        ghcnd_depths.append(total_obs)
        stations_used.append(len(station_ids))

        if total_obs > 0:
            depth_ratios.append(float(storm["total_depth_in"]) / total_obs)
        else:
            depth_ratios.append(float("nan"))

    result["ghcnd_depth_in"] = ghcnd_depths
    result["ghcnd_stations_used"] = stations_used
    result["depth_ratio"] = depth_ratios
    return result


# ── QC Flagging ───────────────────────────────────────────────────────────────

def qc_storm_catalog(
    storm_catalog_df: pd.DataFrame,
    bounds: tuple,
    mock: bool = False,
) -> pd.DataFrame:
    """QC-flag AORC storms against GHCND observations.

    Calls compare_storm_depths() then assigns a qc_flag per storm:
    - "no_obs":  No observed data available (ghcnd_depth_in is NaN).
    - "ok":      depth_ratio in [0.6, 1.6] — AORC and observed agree.
    - "low":     depth_ratio < 0.6 — AORC depth significantly below observed.
    - "high":    depth_ratio > 1.6 — AORC depth significantly above observed.

    Args:
        storm_catalog_df: DataFrame from catalog_storms().
        bounds:           (west, south, east, north) in WGS84.
        mock:             If True, synthesize observations without network calls.

    Returns:
        Enriched copy of storm_catalog_df with qc_flag column added.
    """
    result = compare_storm_depths(storm_catalog_df, bounds, mock=mock)

    flags = []
    for _, row in result.iterrows():
        ghcnd = row["ghcnd_depth_in"]
        if np.isnan(ghcnd):
            flags.append("no_obs")
        elif row["depth_ratio"] < 0.6:
            flags.append("low")
        elif row["depth_ratio"] > 1.6:
            flags.append("high")
        else:
            flags.append("ok")

    result["qc_flag"] = flags

    counts = result["qc_flag"].value_counts().to_dict()
    logger.info(
        f"qc_storm_catalog: {len(result)} storms — "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )
    return result
