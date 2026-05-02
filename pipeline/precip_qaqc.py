"""
precip_qaqc.py - station precipitation evidence for rain-on-grid QAQC.

The routines in this module are intentionally artifact-oriented. ras-agent does
not download NOAA station data here; it normalizes station/grid summaries from a
preceding precipitation step, computes review flags, and writes report-ready
JSON/CSV/PNG evidence.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import statistics
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

STATION_PRECIP_QAQC_SCHEMA_VERSION = "station-precip-qaqc/v1"
DEFAULT_RATIO_BOUNDS = (0.75, 1.25)

STATION_TABLE_COLUMNS = [
    "station_id",
    "station_name",
    "network",
    "latitude",
    "longitude",
    "distance_mi",
    "observed_depth_in",
    "gridded_depth_in",
    "station_to_grid_ratio",
    "status",
    "missing",
    "missing_reason",
    "flags",
]


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _text_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _time_text(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return _text_or_none(value)


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _round_or_none(value: Optional[float], digits: int = 3) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _mean(values: list[float]) -> Optional[float]:
    return float(statistics.fmean(values)) if values else None


def _median(values: list[float]) -> Optional[float]:
    return float(statistics.median(values)) if values else None


def _flag(
    flag_id: str,
    severity: str,
    message: str,
    *,
    category: str = "data",
    blocking_for: str = "model-readiness",
) -> dict[str, str]:
    return {
        "id": flag_id,
        "category": category,
        "severity": severity,
        "message": message,
        "blocking_for": blocking_for,
    }


def _first_value(record: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in record:
            return record[key]
    return None


def _normalize_station_record(
    record: Mapping[str, Any],
    *,
    default_grid_depth_in: Optional[float],
    ratio_bounds: tuple[float, float],
) -> dict[str, Any]:
    observed_depth = _float_or_none(
        _first_value(
            record,
            (
                "observed_depth_in",
                "station_depth_in",
                "accumulation_in",
                "precipitation_in",
                "depth_in",
            ),
        )
    )
    gridded_depth = _float_or_none(
        _first_value(
            record,
            (
                "gridded_depth_in",
                "grid_depth_in",
                "modeled_depth_in",
                "forcing_depth_in",
            ),
        )
    )
    if gridded_depth is None:
        gridded_depth = default_grid_depth_in

    missing = bool(record.get("missing", False)) or observed_depth is None
    missing_reason = _text_or_none(record.get("missing_reason"))
    if missing and missing_reason is None:
        missing_reason = "station_observation_missing"

    station_flags: list[str] = []
    ratio = None
    status = "ok"
    if missing:
        station_flags.append("missing-observation")
        status = "missing"
    if gridded_depth is None:
        station_flags.append("missing-gridded-depth")
        if status == "ok":
            status = "incomplete"
    elif gridded_depth <= 0:
        station_flags.append("zero-gridded-depth")
        if status == "ok":
            status = "incomplete"
    elif observed_depth is not None and not missing:
        ratio = observed_depth / gridded_depth
        if ratio < ratio_bounds[0] or ratio > ratio_bounds[1]:
            station_flags.append("station-grid-disagreement")
            status = "disagreement"

    return {
        "station_id": _text_or_none(
            _first_value(record, ("station_id", "id", "ghcnd_id", "noaa_station_id"))
        ),
        "station_name": _text_or_none(_first_value(record, ("station_name", "name"))),
        "network": _text_or_none(record.get("network")) or "GHCND",
        "latitude": _round_or_none(_float_or_none(record.get("latitude")), 6),
        "longitude": _round_or_none(_float_or_none(record.get("longitude")), 6),
        "distance_mi": _round_or_none(_float_or_none(record.get("distance_mi")), 3),
        "observed_depth_in": _round_or_none(observed_depth),
        "gridded_depth_in": _round_or_none(gridded_depth),
        "station_to_grid_ratio": _round_or_none(ratio),
        "status": status,
        "missing": missing,
        "missing_reason": missing_reason,
        "flags": station_flags,
    }


def compare_station_precipitation(
    *,
    stations: Iterable[Mapping[str, Any]],
    event_start: Any,
    event_end: Any,
    gridded_source: str,
    gridded_depth_in: Optional[float] = None,
    noaa_token_available: Optional[bool] = True,
    search_radius_mi: Optional[float] = None,
    accumulation_window: Optional[str] = None,
    min_valid_stations: int = 2,
    ratio_bounds: tuple[float, float] = DEFAULT_RATIO_BOUNDS,
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    """
    Compare observed station storm depths against a gridded forcing depth.

    Args:
        stations: Station summaries. Each record may include observed_depth_in,
            gridded_depth_in, station_id, station_name, distance_mi, and
            missing/missing_reason fields.
        event_start: Event accumulation start time.
        event_end: Event accumulation end time.
        gridded_source: Source label for the gridded forcing, e.g. AORC or MRMS.
        gridded_depth_in: Event depth to compare at all stations when a station
            record does not provide its own gridded_depth_in.
        noaa_token_available: False records a NOAA token data gap. None means
            the token state was not evaluated.
        search_radius_mi: Station search radius used by the upstream step.
        accumulation_window: Human-readable accumulation window label.
        min_valid_stations: Count below which station evidence is flagged as
            limited even if ratios are within tolerance.
        ratio_bounds: Inclusive lower/upper agreement bounds for station/grid
            ratios.
        generated_at: Optional generated timestamp for deterministic tests.

    Returns:
        JSON-serializable station precipitation QAQC artifact dictionary.
    """
    if ratio_bounds[0] <= 0 or ratio_bounds[0] >= ratio_bounds[1]:
        raise ValueError("ratio_bounds must be positive and ordered as (low, high)")
    if min_valid_stations < 1:
        raise ValueError("min_valid_stations must be >= 1")

    default_grid_depth = _float_or_none(gridded_depth_in)
    station_rows = [
        _normalize_station_record(
            record,
            default_grid_depth_in=default_grid_depth,
            ratio_bounds=ratio_bounds,
        )
        for record in stations
    ]

    station_count = len(station_rows)
    nearby_station_count = station_count
    valid_observed = [
        float(row["observed_depth_in"])
        for row in station_rows
        if not row["missing"] and row["observed_depth_in"] is not None
    ]
    gridded_values = [
        float(row["gridded_depth_in"])
        for row in station_rows
        if not row["missing"] and row["gridded_depth_in"] is not None
    ]
    ratios = [
        float(row["station_to_grid_ratio"])
        for row in station_rows
        if row["station_to_grid_ratio"] is not None
    ]
    missing_count = sum(1 for row in station_rows if row["missing"])
    grid_missing_count = sum(
        1 for row in station_rows
        if not row["missing"] and row["gridded_depth_in"] is None
    )

    flags: list[dict[str, str]] = []
    if noaa_token_available is False:
        flags.append(_flag(
            "no-noaa-token",
            "high",
            "NOAA token was not available, so station precipitation observations could not be fetched.",
            category="service",
        ))
    if nearby_station_count == 0:
        flags.append(_flag(
            "no-nearby-stations",
            "medium",
            "No GHCND/station precipitation gauges were available in the configured search radius.",
        ))
    if station_count > 0 and missing_count > 0:
        flags.append(_flag(
            "station-observations-missing",
            "medium",
            f"{missing_count} of {station_count} station records were missing event precipitation observations.",
        ))
    if station_count > 0 and not valid_observed:
        flags.append(_flag(
            "no-valid-observations",
            "high",
            "Nearby stations were found, but none had usable precipitation observations for the event window.",
        ))
    if valid_observed and grid_missing_count:
        flags.append(_flag(
            "gridded-depth-missing",
            "high",
            "At least one usable station observation had no comparable gridded precipitation depth.",
        ))
    if 0 < len(valid_observed) < min_valid_stations:
        flags.append(_flag(
            "low-station-count",
            "medium",
            (
                f"Only {len(valid_observed)} valid station observation(s) were available; "
                f"{min_valid_stations} are preferred before treating station evidence as representative."
            ),
        ))

    median_ratio = _median(ratios)
    if median_ratio is not None and (
        median_ratio < ratio_bounds[0] or median_ratio > ratio_bounds[1]
    ):
        flags.append(_flag(
            "station-grid-disagreement",
            "high",
            (
                f"Median station/grid precipitation ratio {median_ratio:.2f} is outside "
                f"the review band {ratio_bounds[0]:.2f}-{ratio_bounds[1]:.2f}."
            ),
        ))

    if not valid_observed or noaa_token_available is False or station_count == 0:
        assessment = "insufficient_station_evidence"
    elif median_ratio is not None and (
        median_ratio < ratio_bounds[0] or median_ratio > ratio_bounds[1]
    ):
        assessment = "conflicts_with_grid"
    elif len(valid_observed) < min_valid_stations:
        assessment = "limited_station_evidence"
    else:
        assessment = "supports_grid"

    missing_data_conditions = sorted({
        flag["id"]
        for flag in flags
        if flag["id"] in {
            "no-noaa-token",
            "no-nearby-stations",
            "station-observations-missing",
            "no-valid-observations",
            "gridded-depth-missing",
            "low-station-count",
        }
    })

    summary = {
        "assessment": assessment,
        "station_count": station_count,
        "nearby_station_count": nearby_station_count,
        "valid_observation_count": len(valid_observed),
        "missing_observation_count": missing_count,
        "ratio_count": len(ratios),
        "observed_depth_mean_in": _round_or_none(_mean(valid_observed)),
        "observed_depth_median_in": _round_or_none(_median(valid_observed)),
        "gridded_depth_mean_in": _round_or_none(_mean(gridded_values)),
        "station_to_grid_ratio_mean": _round_or_none(_mean(ratios)),
        "station_to_grid_ratio_median": _round_or_none(median_ratio),
        "agreement_ratio_bounds": [
            _round_or_none(float(ratio_bounds[0])),
            _round_or_none(float(ratio_bounds[1])),
        ],
        "missing_data_conditions": missing_data_conditions,
    }

    return {
        "schema_version": STATION_PRECIP_QAQC_SCHEMA_VERSION,
        "generated_at": generated_at or _utc_timestamp(),
        "event": {
            "start_time": _time_text(event_start),
            "end_time": _time_text(event_end),
            "accumulation_window": accumulation_window,
        },
        "grid": {
            "source": gridded_source,
            "depth_in": _round_or_none(default_grid_depth),
            "depth_units": "in",
        },
        "station_network": {
            "network": "GHCND",
            "provider": "NOAA NCEI",
            "noaa_token_available": noaa_token_available,
            "search_radius_mi": _round_or_none(_float_or_none(search_radius_mi)),
            "nearby_station_count": nearby_station_count,
            "station_count": station_count,
            "valid_observation_count": len(valid_observed),
            "missing_observation_count": missing_count,
        },
        "summary": summary,
        "flags": flags,
        "stations": station_rows,
        "artifacts": {},
    }


def _write_station_table(result: dict[str, Any], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=STATION_TABLE_COLUMNS)
        writer.writeheader()
        for row in result.get("stations", []):
            csv_row = {key: row.get(key) for key in STATION_TABLE_COLUMNS}
            csv_row["flags"] = ";".join(row.get("flags", []))
            writer.writerow(csv_row)


def _plot_station_precip_qaqc(result: dict[str, Any], figure_path: Path) -> Optional[Path]:
    rows = [
        row for row in result.get("stations", [])
        if row.get("observed_depth_in") is not None or row.get("gridded_depth_in") is not None
    ]
    if not rows:
        return None

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        logger.warning("matplotlib not available; skipping station precip QAQC figure")
        return None

    labels = [
        row.get("station_id")
        or row.get("station_name")
        or f"Station {idx + 1}"
        for idx, row in enumerate(rows)
    ]
    observed = [float(row.get("observed_depth_in") or 0.0) for row in rows]
    gridded = [float(row.get("gridded_depth_in") or 0.0) for row in rows]
    x = np.arange(len(rows))
    width = 0.38

    fig, ax = plt.subplots(figsize=(max(7.0, len(rows) * 1.2), 4.8))
    ax.bar(x - width / 2, observed, width, label="Station observed", color="#1f77b4")
    ax.bar(x + width / 2, gridded, width, label="Gridded forcing", color="#f28e2b")
    ax.set_ylabel("Event precipitation depth (in)")
    ax.set_title("Station vs Gridded Event Precipitation")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=35, ha="right")
    ax.grid(True, axis="y", linestyle="--", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    figure_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(figure_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return figure_path


def write_station_precip_qaqc(
    result: Mapping[str, Any],
    output_dir: Path,
    *,
    include_figure: bool = True,
) -> dict[str, Any]:
    """
    Write station precipitation QAQC JSON, CSV, and optional PNG artifacts.

    Returns the written result dictionary with artifact paths populated.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    written = deepcopy(dict(result))
    json_path = output_dir / "station_precip_qaqc.json"
    csv_path = output_dir / "station_precip_qaqc_stations.csv"
    figure_path = output_dir / "station_precip_qaqc.png"

    _write_station_table(written, csv_path)
    artifacts = {
        "station_table_csv": str(csv_path),
        "figure_png": None,
    }
    if include_figure:
        plotted = _plot_station_precip_qaqc(written, figure_path)
        if plotted is not None:
            artifacts["figure_png"] = str(plotted)
    artifacts["station_qaqc_json"] = str(json_path)
    written["artifacts"] = artifacts

    json_path.write_text(json.dumps(written, indent=2, allow_nan=False), encoding="utf-8")
    logger.info("Wrote station precipitation QAQC artifacts to %s", output_dir)
    return written


def build_station_precip_qaqc(
    *,
    stations: Iterable[Mapping[str, Any]],
    output_dir: Path,
    event_start: Any,
    event_end: Any,
    gridded_source: str,
    gridded_depth_in: Optional[float] = None,
    noaa_token_available: Optional[bool] = True,
    search_radius_mi: Optional[float] = None,
    accumulation_window: Optional[str] = None,
    min_valid_stations: int = 2,
    ratio_bounds: tuple[float, float] = DEFAULT_RATIO_BOUNDS,
    generated_at: Optional[str] = None,
    include_figure: bool = True,
) -> dict[str, Any]:
    """Compute and write report-ready station precipitation QAQC artifacts."""
    result = compare_station_precipitation(
        stations=stations,
        event_start=event_start,
        event_end=event_end,
        gridded_source=gridded_source,
        gridded_depth_in=gridded_depth_in,
        noaa_token_available=noaa_token_available,
        search_radius_mi=search_radius_mi,
        accumulation_window=accumulation_window,
        min_valid_stations=min_valid_stations,
        ratio_bounds=ratio_bounds,
        generated_at=generated_at,
    )
    return write_station_precip_qaqc(result, output_dir, include_figure=include_figure)


def load_station_precip_qaqc(path: Path) -> dict[str, Any]:
    """Load a station precipitation QAQC JSON artifact."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
