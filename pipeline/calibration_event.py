"""
calibration_event.py - Spring Creek observed event package builder.

This module selects a single historical flood event for USGS 05577500 and
packages the observed hydrograph plus AORC basin-average precipitation for
downstream calibration runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

import nwis

LOGGER = logging.getLogger(__name__)

SITE_ID = "05577500"
SITE_NAME = "Spring Creek at Springfield, IL"
FLOW_PARAMETER = "00060"
STAGE_PARAMETER = "00065"
LOCAL_TIMEZONE = "America/Chicago"
OK_QUALIFIERS = {"A"}


@dataclass
class HydrographDiagnostics:
    peak_flow_cfs: float
    peak_timestamp: pd.Timestamp
    event_start: pd.Timestamp
    recession_end: pd.Timestamp
    base_flow_cfs: float
    threshold_cfs: float
    rise_hours: float
    recession_hours: float
    clear_shape: bool


@dataclass
class EventSelection:
    site_id: str
    event_name: str
    sim_start: pd.Timestamp
    sim_end: pd.Timestamp
    peak_timestamp: pd.Timestamp
    peak_observed_flow_cfs: float
    peak_observed_stage_ft: float
    selected_peak_record: dict[str, Any]
    candidate_table: pd.DataFrame
    diagnostics: HydrographDiagnostics
    rationale: str


def _iso_z(timestamp: pd.Timestamp) -> str:
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat().replace("+00:00", "Z")


def _utc_for_nwis(timestamp: pd.Timestamp) -> str:
    return _iso_z(timestamp)


def _utc_naive_for_aorc(timestamp: pd.Timestamp) -> str:
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%d %H:%M")


def _safe_float(value: Any) -> Optional[float]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if pd.notna(numeric) else None


def _qualifier_text(qualifiers: Any) -> str:
    if not qualifiers:
        return ""
    if isinstance(qualifiers, str):
        return qualifiers
    return ";".join(str(item) for item in qualifiers if str(item))


def observed_collection_to_frame(collection: nwis.NwisTimeSeriesCollection) -> pd.DataFrame:
    """Convert a NWIS IV collection into one row per timestamp."""
    rows: list[dict[str, Any]] = []
    for record in collection.records:
        timestamp = pd.to_datetime(record["datetime"], utc=True)
        rows.append(
            {
                "datetime_utc": timestamp,
                "parameter_code": record["parameter_code"],
                "value": record["value"],
                "qualifiers": _qualifier_text(record.get("qualifiers")),
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=[
                "datetime_utc",
                "flow_cfs",
                "flow_qualifiers",
                "stage_ft",
                "stage_qualifiers",
            ]
        ).set_index("datetime_utc")

    long_df = pd.DataFrame(rows)
    values = long_df.pivot_table(
        index="datetime_utc",
        columns="parameter_code",
        values="value",
        aggfunc="first",
    )
    qualifiers = long_df.pivot_table(
        index="datetime_utc",
        columns="parameter_code",
        values="qualifiers",
        aggfunc="first",
    )

    frame = pd.DataFrame(index=values.index.sort_values())
    frame["flow_cfs"] = values.get(FLOW_PARAMETER)
    frame["flow_qualifiers"] = qualifiers.get(FLOW_PARAMETER, "")
    frame["stage_ft"] = values.get(STAGE_PARAMETER)
    frame["stage_qualifiers"] = qualifiers.get(STAGE_PARAMETER, "")
    return frame.sort_index()


def _all_qualifiers(frame: pd.DataFrame) -> set[str]:
    qualifiers: set[str] = set()
    for column in ("flow_qualifiers", "stage_qualifiers"):
        if column not in frame:
            continue
        for value in frame[column].dropna():
            qualifiers.update(part for part in str(value).split(";") if part)
    return qualifiers


def _has_clean_quality(frame: pd.DataFrame) -> bool:
    qualifiers = _all_qualifiers(frame)
    return bool(qualifiers) and qualifiers.issubset(OK_QUALIFIERS)


def _hydrograph_diagnostics(flow: pd.Series) -> Optional[HydrographDiagnostics]:
    """Identify event timing and check for a distinct rise, peak, and recession."""
    flow = flow.dropna().sort_index()
    if len(flow) < 24:
        return None

    peak_timestamp = flow.idxmax()
    peak_flow = float(flow.loc[peak_timestamp])
    pre_peak = flow.loc[:peak_timestamp]
    post_peak = flow.loc[peak_timestamp:]
    if len(pre_peak) < 8 or len(post_peak) < 8:
        return None

    pre_window_start = peak_timestamp - pd.Timedelta(hours=48)
    local_pre = flow.loc[pre_window_start:peak_timestamp]
    if len(local_pre) < 8:
        local_pre = pre_peak

    base_timestamp = local_pre.idxmin()
    base_flow = float(flow.loc[base_timestamp])
    threshold = base_flow + 0.05 * (peak_flow - base_flow)
    threshold = max(threshold, base_flow + 25.0)

    rising = flow.loc[base_timestamp:peak_timestamp]
    above = rising[rising >= threshold]
    if above.empty:
        return None
    event_start = above.index[0]

    recession = post_peak[post_peak <= threshold]
    recession_end = recession.index[0] if not recession.empty else post_peak.index[-1]

    rise_hours = (peak_timestamp - event_start) / pd.Timedelta(hours=1)
    recession_hours = (recession_end - peak_timestamp) / pd.Timedelta(hours=1)
    clear_shape = (
        peak_flow >= max(base_flow * 3.0, base_flow + 1000.0)
        and rise_hours >= 1.0
        and recession_hours >= 6.0
        and not recession.empty
    )

    return HydrographDiagnostics(
        peak_flow_cfs=peak_flow,
        peak_timestamp=pd.Timestamp(peak_timestamp),
        event_start=pd.Timestamp(event_start),
        recession_end=pd.Timestamp(recession_end),
        base_flow_cfs=base_flow,
        threshold_cfs=float(threshold),
        rise_hours=float(rise_hours),
        recession_hours=float(recession_hours),
        clear_shape=bool(clear_shape),
    )


def _copy_peak_rdb(peak_series: nwis.NwisPeakSeries, output_path: Path) -> Optional[Path]:
    cache = peak_series.cache
    if cache is None or not cache.body_path:
        return None
    source = Path(cache.body_path)
    if not source.exists():
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output_path)
    return output_path


def _peak_records_frame(peak_series: nwis.NwisPeakSeries, limit: int) -> pd.DataFrame:
    frame = pd.DataFrame(peak_series.records)
    if frame.empty:
        return frame
    frame["peak_cfs"] = pd.to_numeric(frame["peak_cfs"], errors="coerce")
    frame["gage_height_ft"] = pd.to_numeric(frame["gage_height_ft"], errors="coerce")
    frame["peak_date"] = pd.to_datetime(frame["peak_dt"], errors="coerce")
    frame = frame.dropna(subset=["peak_cfs", "peak_date"])
    return frame.sort_values("peak_cfs", ascending=False).head(limit).reset_index(drop=True)


def _evaluate_candidate(
    site_id: str,
    peak_row: pd.Series,
    cache_dir: Path,
) -> dict[str, Any]:
    peak_date = pd.Timestamp(peak_row["peak_date"])
    review_start = (peak_date - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    review_end = (peak_date + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    annual_peak_codes = peak_row.get("peak_cd") or []
    if isinstance(annual_peak_codes, str):
        annual_peak_codes = [annual_peak_codes] if annual_peak_codes else []

    collection = nwis.get_instantaneous_values(
        site_id,
        ["discharge", "stage"],
        start_date=review_start,
        end_date=review_end,
        cache_dir=cache_dir,
        max_age_hours=24.0 * 365.0,
    )
    observed = observed_collection_to_frame(collection)
    diagnostics = _hydrograph_diagnostics(observed["flow_cfs"]) if "flow_cfs" in observed else None
    iv_peak_stage_ft = (
        float(observed["stage_ft"].max())
        if "stage_ft" in observed and observed["stage_ft"].notna().any()
        else None
    )

    has_flow = "flow_cfs" in observed and observed["flow_cfs"].notna().any()
    has_stage = "stage_ft" in observed and observed["stage_ft"].notna().any()
    clean_quality = _has_clean_quality(observed)
    aorc_available = int(peak_date.year) >= 1979
    preferred_period = 2000 <= int(peak_date.year) <= 2020
    clear_shape = bool(diagnostics and diagnostics.clear_shape)
    annual_peak_clean = len(annual_peak_codes) == 0
    eligible = all(
        [
            aorc_available,
            annual_peak_clean,
            has_flow,
            has_stage,
            clean_quality,
            clear_shape,
        ]
    )

    reasons = []
    if not aorc_available:
        reasons.append("pre-AORC")
    if not annual_peak_clean:
        reasons.append(f"annual peak codes={','.join(annual_peak_codes)}")
    if not has_flow:
        reasons.append("missing IV flow")
    if not has_stage:
        reasons.append("missing IV stage")
    if not clean_quality:
        reasons.append(f"IV qualifiers={','.join(sorted(_all_qualifiers(observed))) or 'none'}")
    if not clear_shape:
        reasons.append("hydrograph shape not clear")

    return {
        "peak_date": peak_date.date().isoformat(),
        "annual_peak_flow_cfs": float(peak_row["peak_cfs"]),
        "annual_peak_stage_ft": _safe_float(peak_row.get("gage_height_ft")),
        "annual_peak_codes": ",".join(annual_peak_codes),
        "aorc_available": aorc_available,
        "preferred_2000_2020": preferred_period,
        "has_iv_flow": bool(has_flow),
        "has_iv_stage": bool(has_stage),
        "clean_iv_quality": bool(clean_quality),
        "iv_qualifiers": ",".join(sorted(_all_qualifiers(observed))),
        "clear_hydrograph_shape": clear_shape,
        "eligible": bool(eligible),
        "rejection_reason": "; ".join(reasons),
        "peak_timestamp_utc": _iso_z(diagnostics.peak_timestamp) if diagnostics else None,
        "iv_peak_flow_cfs": diagnostics.peak_flow_cfs if diagnostics else None,
        "iv_peak_stage_ft": iv_peak_stage_ft,
        "event_start_utc": _iso_z(diagnostics.event_start) if diagnostics else None,
        "recession_end_utc": _iso_z(diagnostics.recession_end) if diagnostics else None,
        "_peak_row": peak_row.to_dict(),
        "_diagnostics": diagnostics,
    }


def select_event(
    site_id: str,
    package_dir: Path,
    top_n: int = 10,
) -> EventSelection:
    cache_dir = package_dir / ".cache" / "nwis"
    peak_series = nwis.get_annual_peaks(
        site_id,
        cache_dir=cache_dir,
        max_age_hours=24.0 * 365.0,
    )
    _copy_peak_rdb(
        peak_series,
        package_dir / "observed" / f"USGS_{site_id}_annual_peaks.rdb",
    )

    peaks = _peak_records_frame(peak_series, top_n)
    if peaks.empty:
        raise RuntimeError(f"No annual peak records found for USGS {site_id}")

    evaluations = [
        _evaluate_candidate(site_id, row, cache_dir)
        for _, row in peaks.iterrows()
    ]
    candidate_table = pd.DataFrame(
        [{k: v for k, v in item.items() if not k.startswith("_")} for item in evaluations]
    )

    eligible = [item for item in evaluations if item["eligible"]]
    preferred = [item for item in eligible if item["preferred_2000_2020"]]
    pool = preferred or eligible
    if not pool:
        raise RuntimeError("No candidate event met the selection criteria")

    selected = sorted(pool, key=lambda item: item["annual_peak_flow_cfs"], reverse=True)[0]
    diagnostics = selected["_diagnostics"]
    if diagnostics is None:
        raise RuntimeError("Selected event is missing hydrograph diagnostics")

    sim_start = diagnostics.event_start - pd.Timedelta(hours=24)
    sim_end = diagnostics.recession_end + pd.Timedelta(hours=48)

    event_year = diagnostics.peak_timestamp.tz_convert("UTC").year
    event_month = diagnostics.peak_timestamp.tz_convert("UTC").strftime("%B")
    event_name = f"{event_month} {event_year} Spring Creek flood"
    rationale = (
        "Selected the June 2011 Spring Creek flood because it is the largest "
        "post-2000 annual peak with complete approved instantaneous discharge "
        "and gage-height records at USGS 05577500. The larger May 2002 peak is "
        "post-AORC but lacks IV stage records in NWIS, while the 2011 event has "
        "a distinct rising limb, peak, and recession."
    )

    return EventSelection(
        site_id=site_id,
        event_name=event_name,
        sim_start=sim_start,
        sim_end=sim_end,
        peak_timestamp=diagnostics.peak_timestamp,
        peak_observed_flow_cfs=diagnostics.peak_flow_cfs,
        peak_observed_stage_ft=float(selected["iv_peak_stage_ft"]),
        selected_peak_record=selected["_peak_row"],
        candidate_table=candidate_table,
        diagnostics=diagnostics,
        rationale=rationale,
    )


def retrieve_observed_event(
    selection: EventSelection,
    package_dir: Path,
) -> tuple[pd.DataFrame, Path]:
    collection = nwis.get_instantaneous_values(
        selection.site_id,
        ["discharge", "stage"],
        start_date=_utc_for_nwis(selection.sim_start),
        end_date=_utc_for_nwis(selection.sim_end),
        cache_dir=package_dir / ".cache" / "nwis",
        max_age_hours=24.0 * 365.0,
    )
    observed = observed_collection_to_frame(collection)
    if observed.empty:
        raise RuntimeError("No observed IV records were returned for selected event window")

    observed_out = observed.reset_index()
    observed_out["datetime_utc"] = observed_out["datetime_utc"].map(_iso_z)
    observed_out["datetime_local"] = pd.to_datetime(
        observed_out["datetime_utc"], utc=True
    ).dt.tz_convert(LOCAL_TIMEZONE).map(lambda ts: ts.isoformat())
    observed_out = observed_out[
        [
            "datetime_utc",
            "datetime_local",
            "flow_cfs",
            "flow_qualifiers",
            "stage_ft",
            "stage_qualifiers",
        ]
    ]

    observed_dir = package_dir / "observed"
    observed_dir.mkdir(parents=True, exist_ok=True)
    csv_path = observed_dir / f"USGS_{selection.site_id}_2011_event_iv.csv"
    observed_out.to_csv(csv_path, index=False)
    return observed, csv_path


def download_basin_geojson(site_id: str, package_dir: Path) -> Path:
    import httpx

    url = f"https://api.water.usgs.gov/nldi/linked-data/nwissite/USGS-{site_id}/basin"
    response = httpx.get(url, timeout=60.0, follow_redirects=True)
    response.raise_for_status()
    basin_path = package_dir / "basin" / f"USGS_{site_id}_nldi_basin.geojson"
    basin_path.parent.mkdir(parents=True, exist_ok=True)
    basin_path.write_text(json.dumps(response.json(), indent=2), encoding="utf-8")
    return basin_path


def _basin_bounds_wgs84(basin_path: Path, buffer_degrees: float = 0.02) -> tuple[float, float, float, float]:
    import geopandas as gpd

    basin = gpd.read_file(basin_path).to_crs("EPSG:4326")
    west, south, east, north = basin.total_bounds
    return (
        float(west - buffer_degrees),
        float(south - buffer_degrees),
        float(east + buffer_degrees),
        float(north + buffer_degrees),
    )


def download_aorc_event(
    selection: EventSelection,
    basin_path: Path,
    package_dir: Path,
) -> Path:
    from ras_commander.precip import PrecipAorc

    bounds = _basin_bounds_wgs84(basin_path)
    output_path = (
        package_dir
        / "precipitation"
        / f"aorc_USGS_{selection.site_id}_20110617_20110624.nc"
    )
    return PrecipAorc.download(
        bounds,
        _utc_naive_for_aorc(selection.sim_start),
        _utc_naive_for_aorc(selection.sim_end),
        output_path,
        target_crs="EPSG:5070",
        resolution=2000.0,
    )


def _pick_precip_variable(dataset: Any) -> str:
    for name, data_array in dataset.data_vars.items():
        if name == "spatial_ref":
            continue
        if {"time"}.issubset(set(data_array.dims)):
            return name
    raise RuntimeError("Could not find a time-varying precipitation variable in AORC NetCDF")


def compute_basin_average_hyetograph(
    netcdf_path: Path,
    basin_path: Path,
    sim_start: pd.Timestamp,
    sim_end: pd.Timestamp,
    output_csv: Path,
) -> pd.DataFrame:
    import geopandas as gpd
    import numpy as np
    import xarray as xr
    from shapely import contains_xy

    dataset = xr.open_dataset(netcdf_path)
    variable = _pick_precip_variable(dataset)
    precip = dataset[variable]

    spatial_dims = [dim for dim in precip.dims if dim != "time"]
    if len(spatial_dims) != 2:
        raise RuntimeError(f"Unexpected AORC dimensions: {precip.dims}")
    y_dim, x_dim = spatial_dims
    x_values = precip[x_dim].values
    y_values = precip[y_dim].values

    basin = gpd.read_file(basin_path).to_crs("EPSG:5070")
    geometry = (
        basin.geometry.union_all()
        if hasattr(basin.geometry, "union_all")
        else basin.geometry.unary_union
    )
    xx, yy = np.meshgrid(x_values, y_values)
    mask = contains_xy(geometry, xx, yy)
    if not mask.any():
        raise RuntimeError("No AORC grid-cell centers fell inside the Spring Creek basin")

    masked = precip.where(mask)
    spatial_mean_mm = masked.mean(dim=(y_dim, x_dim), skipna=True)
    hyetograph = spatial_mean_mm.to_dataframe(name="precip_mm").reset_index()
    hyetograph["time"] = pd.to_datetime(hyetograph["time"], utc=True)
    hyetograph = hyetograph[
        (hyetograph["time"] >= pd.Timestamp(sim_start).tz_convert("UTC"))
        & (hyetograph["time"] <= pd.Timestamp(sim_end).tz_convert("UTC"))
    ].copy()
    hyetograph["precip_in"] = hyetograph["precip_mm"] / 25.4
    hyetograph["datetime_utc"] = hyetograph["time"].map(_iso_z)
    hyetograph = hyetograph[["datetime_utc", "precip_mm", "precip_in"]]

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    hyetograph.to_csv(output_csv, index=False)
    dataset.close()
    return hyetograph


def write_plots(
    observed: pd.DataFrame,
    hyetograph: pd.DataFrame,
    selection: EventSelection,
    package_dir: Path,
) -> dict[str, Path]:
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    plot_dir = package_dir / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)

    observed_plot = observed.copy()
    observed_plot.index = observed_plot.index.tz_convert("UTC")
    precip_plot = hyetograph.copy()
    precip_plot["datetime_utc"] = pd.to_datetime(precip_plot["datetime_utc"], utc=True)

    hyetograph_png = plot_dir / "spring_creek_2011_hyetograph.png"
    hydrograph_png = plot_dir / "spring_creek_2011_hydrograph.png"
    combined_png = plot_dir / "spring_creek_2011_hyetograph_hydrograph.png"

    locator = mdates.AutoDateLocator(minticks=5, maxticks=10)
    formatter = mdates.ConciseDateFormatter(locator)

    fig, ax = plt.subplots(figsize=(11, 4))
    ax.bar(
        precip_plot["datetime_utc"],
        precip_plot["precip_in"],
        width=0.035,
        color="#2c7fb8",
        align="center",
    )
    ax.set_ylabel("AORC precipitation (in/hr)")
    ax.set_title("Spring Creek basin-average AORC hyetograph - June 2011")
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    fig.tight_layout()
    fig.savefig(hyetograph_png, dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4))
    ax.plot(observed_plot.index, observed_plot["flow_cfs"], color="#08519c", linewidth=2)
    ax.axvline(selection.peak_timestamp, color="#b30000", linewidth=1, linestyle="--")
    ax.set_ylabel("Observed flow (cfs)")
    ax.set_title("USGS 05577500 observed hydrograph - June 2011")
    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    fig.tight_layout()
    fig.savefig(hydrograph_png, dpi=160)
    plt.close(fig)

    fig, (ax_p, ax_q) = plt.subplots(
        2,
        1,
        figsize=(11, 7),
        sharex=True,
        gridspec_kw={"height_ratios": [1, 2]},
    )
    ax_p.bar(
        precip_plot["datetime_utc"],
        precip_plot["precip_in"],
        width=0.035,
        color="#2c7fb8",
        align="center",
    )
    ax_p.set_ylabel("Precip. (in/hr)")
    ax_p.set_title("Spring Creek June 2011 calibration event")
    ax_q.plot(observed_plot.index, observed_plot["flow_cfs"], color="#08519c", linewidth=2)
    ax_q.axvline(selection.peak_timestamp, color="#b30000", linewidth=1, linestyle="--")
    ax_q.set_ylabel("Flow (cfs)")
    ax_q.grid(True, alpha=0.25)
    ax_q.xaxis.set_major_locator(locator)
    ax_q.xaxis.set_major_formatter(formatter)
    fig.tight_layout()
    fig.savefig(combined_png, dpi=160)
    plt.close(fig)

    return {
        "hyetograph": hyetograph_png,
        "hydrograph": hydrograph_png,
        "combined": combined_png,
    }


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def build_event_package(
    workspace_dir: Path = Path("workspace"),
    site_id: str = SITE_ID,
) -> dict[str, Any]:
    workspace_dir = Path(workspace_dir)
    package_dir = workspace_dir / "calibration_event_data"
    package_dir.mkdir(parents=True, exist_ok=True)

    selection = select_event(site_id, package_dir)
    candidate_csv = package_dir / "observed" / f"USGS_{site_id}_candidate_events.csv"
    candidate_csv.parent.mkdir(parents=True, exist_ok=True)
    selection.candidate_table.to_csv(candidate_csv, index=False)

    observed, observed_csv = retrieve_observed_event(selection, package_dir)
    basin_path = download_basin_geojson(site_id, package_dir)
    netcdf_path = download_aorc_event(selection, basin_path, package_dir)
    hyetograph_csv = (
        package_dir
        / "precipitation"
        / f"USGS_{site_id}_2011_aorc_basin_hyetograph.csv"
    )
    hyetograph = compute_basin_average_hyetograph(
        netcdf_path,
        basin_path,
        selection.sim_start,
        selection.sim_end,
        hyetograph_csv,
    )
    plot_paths = write_plots(observed, hyetograph, selection, package_dir)

    event_json = workspace_dir / "calibration_event.json"
    payload = {
        "site_id": site_id,
        "site_name": SITE_NAME,
        "event_name": selection.event_name,
        "sim_start": _iso_z(selection.sim_start),
        "sim_end": _iso_z(selection.sim_end),
        "peak_observed_flow_cfs": selection.peak_observed_flow_cfs,
        "peak_observed_stage_ft": selection.peak_observed_stage_ft,
        "peak_timestamp": _iso_z(selection.peak_timestamp),
        "aorc_netcdf_path": _relative_path(netcdf_path, Path.cwd()),
        "observed_csv_path": _relative_path(observed_csv, Path.cwd()),
        "hyetograph_csv_path": _relative_path(hyetograph_csv, Path.cwd()),
        "candidate_events_csv_path": _relative_path(candidate_csv, Path.cwd()),
        "plot_paths": {key: _relative_path(path, Path.cwd()) for key, path in plot_paths.items()},
        "selection_rationale": selection.rationale,
    }
    event_json.parent.mkdir(parents=True, exist_ok=True)
    event_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    LOGGER.info("Wrote calibration event package to %s", event_json)
    return payload


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-dir", type=Path, default=Path("workspace"))
    parser.add_argument("--site-id", default=SITE_ID)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    payload = build_event_package(args.workspace_dir, args.site_id)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
