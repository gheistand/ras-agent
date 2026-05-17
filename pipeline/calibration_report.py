"""
calibration_report.py - Self-contained calibration report generator.

Builds HTML calibration reports with inline Bokeh plots and DSS-Commander-style
statistics for comparing modeled HEC-RAS results against observed gauge data.

Copyright 2026 Glenn Heistand / CHAMP - Illinois State Water Survey
Apache License 2.0
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
import io
import logging
import math
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


_VERSION = "0.1.0"
_METRIC_ORDER = ("rmse_pct", "pearson_r", "pbias", "nse", "kge")


_CSS = """\
body { font-family: Arial, sans-serif; max-width: 1180px; margin: 0 auto; padding: 20px; color: #2f3a45; }
h1 { color: #1a5276; border-bottom: 2px solid #1a5276; padding-bottom: 8px; }
h2 { color: #1a5276; margin-top: 30px; }
.meta { color: #5f6f7f; font-size: 0.92em; line-height: 1.45; }
.summary-note { color: #5f6f7f; font-size: 0.9em; margin-top: -6px; }
table { border-collapse: collapse; width: 100%; margin: 15px 0 22px; }
th { background: #1a5276; color: white; padding: 8px 10px; text-align: left; }
td { padding: 7px 10px; border-bottom: 1px solid #d9e2ea; vertical-align: top; }
tr:nth-child(even) { background: #f6f8fa; }
.metric-pass { background: #d9ead3; color: #1f5f2a; font-weight: bold; }
.metric-fail { background: #f4cccc; color: #8f1d1d; font-weight: bold; }
.metric-neutral { background: #eef3f8; color: #34495e; font-weight: bold; }
.plot-block { margin: 18px 0 28px; }
.plot-title { font-weight: bold; color: #2d506f; margin-bottom: 6px; }
.footer { margin-top: 42px; padding-top: 15px; border-top: 1px solid #d9e2ea; color: #7b8794; font-size: 0.85em; text-align: center; }
.map-container { margin: 18px 0 28px; }
.map-container iframe { display: block; }
"""


@dataclass
class GaugeRecord:
    """Observed gauge data and optional modeled series/extraction metadata."""

    name: str
    observed: pd.Series
    variable: str = "stage"
    units: str = ""
    modeled: dict[str, pd.Series] = field(default_factory=dict)
    x: Optional[float] = None
    y: Optional[float] = None
    extraction_method: str = "2d_cell"
    river: Optional[str] = None
    reach: Optional[str] = None
    station: Optional[str] = None
    ref_feature_name: Optional[str] = None
    depth_datum: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GaugeComparison:
    """Aligned observed/modeled data and calculated calibration statistics."""

    gauge_name: str
    plan_label: str
    variable: str
    units: str
    observed: pd.Series
    modeled: pd.Series
    aligned: pd.DataFrame
    stats: dict[str, float]
    n_points: int


@dataclass
class ProjectContext:
    """Optional project metadata for the report introduction and map sections."""

    title: str = ""
    description: str = ""
    data_sources: list[dict[str, str]] = field(default_factory=list)
    boundary_conditions: list[dict[str, str]] = field(default_factory=list)
    geometry: Any = None
    gauge_locations: list[dict[str, Any]] = field(default_factory=list)
    crs: str = "EPSG:4326"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _float_or_nan(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _coerce_index(index: pd.Index) -> pd.Index:
    if isinstance(index, pd.DatetimeIndex):
        return index

    converted = pd.to_datetime(index, errors="coerce")
    if len(converted) and converted.notna().all():
        return pd.DatetimeIndex(converted)
    return index


def _coerce_series(data: Any, value_name: str = "value") -> pd.Series:
    """Coerce list/dict/DataFrame/Series inputs into a numeric Series."""
    if isinstance(data, pd.Series):
        series = data.copy()
    elif isinstance(data, pd.DataFrame):
        frame = data.copy()
        time_col = next(
            (col for col in ("time", "datetime", "date", "timestamp") if col in frame.columns),
            None,
        )
        value_col = value_name if value_name in frame.columns else None
        if value_col is None:
            candidates = [
                col for col in frame.columns
                if col != time_col and pd.api.types.is_numeric_dtype(frame[col])
            ]
            if not candidates:
                raise ValueError("DataFrame series input needs a numeric value column")
            value_col = candidates[0]
        if time_col is not None:
            series = pd.Series(frame[value_col].to_numpy(), index=frame[time_col])
        else:
            series = pd.Series(frame[value_col].to_numpy(), index=frame.index)
    elif isinstance(data, Mapping):
        series = pd.Series(data)
    else:
        series = pd.Series(data)

    series = pd.to_numeric(series, errors="coerce")
    series.index = _coerce_index(series.index)
    series = series.sort_index() if not isinstance(series.index, pd.RangeIndex) else series
    series.name = value_name
    return series


def _normalize_variable(variable: str) -> str:
    value = str(variable or "stage").strip().lower()
    aliases = {
        "wse": "stage",
        "water_surface": "stage",
        "water surface": "stage",
        "stage": "stage",
        "depth": "stage",
        "flow": "flow",
        "flows": "flow",
        "discharge": "flow",
        "q": "flow",
        "velocity": "velocity",
    }
    return aliases.get(value, value)


def _ras_variable(variable: str) -> str:
    normalized = _normalize_variable(variable)
    if normalized == "stage":
        return "wse"
    return normalized


def _normalize_plan_hdfs(plan_hdfs: Any) -> dict[str, Optional[Path]]:
    if plan_hdfs is None:
        return {}
    if isinstance(plan_hdfs, (str, Path)):
        path = Path(plan_hdfs)
        return {path.stem or "plan": path}
    if isinstance(plan_hdfs, Mapping):
        plans: dict[str, Optional[Path]] = {}
        for key, value in plan_hdfs.items():
            label = str(key)
            plans[label] = None if value is None else Path(value)
        return plans

    plans = {}
    for idx, raw_path in enumerate(plan_hdfs):
        if raw_path is None:
            label = f"plan_{idx + 1}"
            plans[label] = None
            continue
        path = Path(raw_path)
        label = path.stem or f"plan_{idx + 1}"
        if label in plans:
            label = f"{label}_{idx + 1}"
        plans[label] = path
    return plans


def _modeled_mapping(raw_modeled: Any, plan_labels: Sequence[str]) -> dict[str, pd.Series]:
    if raw_modeled is None:
        return {}

    if isinstance(raw_modeled, Mapping) and raw_modeled:
        values = list(raw_modeled.values())
        if any(isinstance(value, (pd.Series, pd.DataFrame, Mapping, list, tuple, np.ndarray)) for value in values):
            try:
                return {
                    str(label): _coerce_series(values, value_name="modeled")
                    for label, values in raw_modeled.items()
                }
            except Exception:
                pass

    label = plan_labels[0] if plan_labels else "Modeled"
    return {label: _coerce_series(raw_modeled, value_name="modeled")}


def _record_from_mapping(
    name: str,
    config: Mapping[str, Any],
    plan_labels: Sequence[str],
) -> GaugeRecord:
    observed = config.get("observed")
    if observed is None:
        observed = config.get("obs")
    if observed is None:
        raise ValueError(f"Gauge '{name}' is missing observed data")

    variable = _normalize_variable(str(config.get("variable", config.get("kind", "stage"))))
    units = str(config.get("units", "ft" if variable == "stage" else "cfs" if variable == "flow" else ""))
    modeled = _modeled_mapping(
        config.get("modeled", config.get("simulated", config.get("model"))),
        plan_labels,
    )

    metadata = {
        str(key): value
        for key, value in config.items()
        if key not in {
            "name", "gauge", "gauge_name", "observed", "obs", "modeled",
            "simulated", "model", "variable", "kind", "units", "x", "y",
            "extraction_method", "river", "reach", "station",
            "ref_feature_name", "depth_datum",
        }
    }

    return GaugeRecord(
        name=str(config.get("name", config.get("gauge", config.get("gauge_name", name)))),
        observed=_coerce_series(observed, value_name="observed"),
        variable=variable,
        units=units,
        modeled=modeled,
        x=None if config.get("x") is None else float(config.get("x")),
        y=None if config.get("y") is None else float(config.get("y")),
        extraction_method=str(config.get("extraction_method", "2d_cell")),
        river=config.get("river"),
        reach=config.get("reach"),
        station=config.get("station"),
        ref_feature_name=config.get("ref_feature_name"),
        depth_datum=None if config.get("depth_datum") is None else float(config.get("depth_datum")),
        metadata=metadata,
    )


def _records_from_dataframe(frame: pd.DataFrame, plan_labels: Sequence[str]) -> list[GaugeRecord]:
    if "gauge" in frame.columns:
        gauge_col = "gauge"
    elif "name" in frame.columns:
        gauge_col = "name"
    else:
        raise ValueError("Observed DataFrame input needs a 'gauge' or 'name' column")

    time_col = next(
        (col for col in ("time", "datetime", "date", "timestamp") if col in frame.columns),
        None,
    )
    if "observed" not in frame.columns:
        raise ValueError("Observed DataFrame input needs an 'observed' column")

    records: list[GaugeRecord] = []
    for gauge_name, group in frame.groupby(gauge_col, sort=False):
        group = group.copy()
        observed = pd.Series(
            group["observed"].to_numpy(),
            index=group[time_col] if time_col is not None else group.index,
        )
        modeled: dict[str, pd.Series] = {}
        if "modeled" in group.columns:
            if "plan" in group.columns:
                for plan_label, plan_group in group.groupby("plan", sort=False):
                    modeled[str(plan_label)] = pd.Series(
                        plan_group["modeled"].to_numpy(),
                        index=plan_group[time_col] if time_col is not None else plan_group.index,
                    )
            else:
                label = plan_labels[0] if plan_labels else "Modeled"
                modeled[label] = pd.Series(
                    group["modeled"].to_numpy(),
                    index=group[time_col] if time_col is not None else group.index,
                )

        variable = _normalize_variable(
            str(group["variable"].iloc[0]) if "variable" in group.columns else "stage"
        )
        units = (
            str(group["units"].iloc[0])
            if "units" in group.columns
            else "ft" if variable == "stage" else "cfs" if variable == "flow" else ""
        )
        records.append(
            GaugeRecord(
                name=str(gauge_name),
                observed=_coerce_series(observed, value_name="observed"),
                variable=variable,
                units=units,
                modeled={
                    label: _coerce_series(series, value_name="modeled")
                    for label, series in modeled.items()
                },
                x=float(group["x"].iloc[0]) if "x" in group.columns and pd.notna(group["x"].iloc[0]) else None,
                y=float(group["y"].iloc[0]) if "y" in group.columns and pd.notna(group["y"].iloc[0]) else None,
            )
        )
    return records


def _coerce_observed_data(observed_data: Any, plan_labels: Sequence[str]) -> list[GaugeRecord]:
    if isinstance(observed_data, pd.DataFrame):
        return _records_from_dataframe(observed_data, plan_labels)

    if isinstance(observed_data, Mapping):
        if "gauges" in observed_data:
            return _coerce_observed_data(observed_data["gauges"], plan_labels)

        records = []
        for key, value in observed_data.items():
            if isinstance(value, Mapping) and (
                "observed" in value or "obs" in value or "modeled" in value
            ):
                records.append(_record_from_mapping(str(key), value, plan_labels))
            else:
                records.append(
                    GaugeRecord(
                        name=str(key),
                        observed=_coerce_series(value, value_name="observed"),
                        modeled={},
                    )
                )
        return records

    records = []
    for idx, item in enumerate(observed_data):
        if isinstance(item, GaugeRecord):
            records.append(item)
        elif isinstance(item, Mapping):
            name = str(item.get("name", item.get("gauge", item.get("gauge_name", f"gauge_{idx + 1}"))))
            records.append(_record_from_mapping(name, item, plan_labels))
        else:
            raise TypeError("Observed data sequence items must be mappings or GaugeRecord instances")
    return records


def _align_series(observed: pd.Series, modeled: pd.Series) -> pd.DataFrame:
    observed = _coerce_series(observed, value_name="observed")
    modeled = _coerce_series(modeled, value_name="modeled")
    aligned = pd.concat(
        [observed.rename("observed"), modeled.rename("modeled")],
        axis=1,
        join="inner",
    ).dropna()

    if aligned.empty and len(observed) == len(modeled) and isinstance(observed.index, pd.RangeIndex):
        aligned = pd.DataFrame(
            {
                "observed": observed.to_numpy(dtype=float),
                "modeled": modeled.to_numpy(dtype=float),
            },
            index=observed.index,
        ).dropna()

    if aligned.empty:
        raise ValueError("Observed and modeled series have no overlapping values")
    return aligned


def _fallback_metrics(observed: np.ndarray, modeled: np.ndarray) -> dict[str, float]:
    residual = modeled - observed
    rmse = float(np.sqrt(np.mean(residual ** 2)))
    obs_sum = float(np.sum(observed))
    pbias = float(100.0 * np.sum(residual) / obs_sum) if obs_sum != 0 else float("nan")
    if len(observed) > 1 and np.std(observed) > 0 and np.std(modeled) > 0:
        correlation = float(np.corrcoef(observed, modeled)[0, 1])
    else:
        correlation = float("nan")

    denominator = float(np.sum((observed - np.mean(observed)) ** 2))
    nse = float(1.0 - np.sum(residual ** 2) / denominator) if denominator != 0 else float("nan")

    if np.std(observed) > 0 and np.mean(observed) != 0 and not np.isnan(correlation):
        alpha = float(np.std(modeled) / np.std(observed))
        beta = float(np.mean(modeled) / np.mean(observed))
        kge = float(1.0 - np.sqrt((correlation - 1.0) ** 2 + (alpha - 1.0) ** 2 + (beta - 1.0) ** 2))
    else:
        kge = float("nan")

    return {
        "rmse": rmse,
        "pbias": pbias,
        "correlation": correlation,
        "nse": nse,
        "kge": kge,
    }


def _commander_metrics(observed: np.ndarray, modeled: np.ndarray, index: pd.Index) -> dict[str, float]:
    try:
        from ras_commander.usgs.metrics import RasUsgsMetrics

        time_index = index if isinstance(index, pd.DatetimeIndex) else None
        if isinstance(time_index, pd.DatetimeIndex) and len(time_index) > 1:
            deltas = time_index.to_series().diff().dropna().dt.total_seconds() / 3600.0
            dt_hours = float(deltas.median()) if not deltas.empty else 1.0
        else:
            dt_hours = 1.0
        metrics = RasUsgsMetrics.calculate_all_metrics(
            observed,
            modeled,
            time_index=time_index,
            dt_hours=dt_hours,
        )
        return {str(key): _float_or_nan(value) for key, value in metrics.items()}
    except Exception as exc:
        logger.debug("Falling back to local calibration metric formulas: %s", exc)
        return _fallback_metrics(observed, modeled)


def calculate_stats(
    observed: Any,
    modeled: Any,
    variable: str = "stage",
) -> dict[str, float]:
    """
    Calculate DSS-Commander-style calibration statistics.

    RMSE percent is normalized by mean observed stage/depth and by peak observed
    flow. NSE, KGE, PBIAS, and Pearson correlation come from ras-commander when
    available, with local formulas used as a fallback for short/mock series.
    """
    aligned = _align_series(
        _coerce_series(observed, value_name="observed"),
        _coerce_series(modeled, value_name="modeled"),
    )
    obs = aligned["observed"].to_numpy(dtype=float)
    mod = aligned["modeled"].to_numpy(dtype=float)
    metrics = _commander_metrics(obs, mod, aligned.index)

    rmse = metrics.get("rmse", _fallback_metrics(obs, mod)["rmse"])
    normalized_variable = _normalize_variable(variable)
    if normalized_variable == "flow":
        denominator = float(np.nanmax(np.abs(obs)))
    else:
        denominator = float(abs(np.nanmean(obs)))
    rmse_pct = float(100.0 * rmse / denominator) if denominator != 0 else float("nan")

    return {
        "rmse": _float_or_nan(rmse),
        "rmse_pct": rmse_pct,
        "pearson_r": _float_or_nan(metrics.get("correlation", metrics.get("r"))),
        "pbias": _float_or_nan(metrics.get("pbias")),
        "nse": _float_or_nan(metrics.get("nse")),
        "kge": _float_or_nan(metrics.get("kge")),
    }


def _extract_modeled_from_plan(plan_hdf: Path, gauge: GaugeRecord) -> pd.Series:
    try:
        from ras_commander.RasCalibrate import CalibrationPoint, extract_modeled
    except Exception as exc:
        raise RuntimeError(
            "ras-commander calibration extraction is unavailable; provide "
            "modeled series in observed_data for mock/report-only use."
        ) from exc

    point_kwargs = {
        "name": gauge.name,
        "variable": _ras_variable(gauge.variable),
        "extraction_method": gauge.extraction_method,
        "observed": gauge.observed,
        "time_index": "all",
        "depth_datum": gauge.depth_datum,
    }
    if gauge.extraction_method == "2d_cell":
        point_kwargs.update({"x": gauge.x, "y": gauge.y})
    elif gauge.extraction_method == "1d_xs":
        point_kwargs.update({"river": gauge.river, "reach": gauge.reach, "station": gauge.station})
    else:
        point_kwargs.update({"ref_feature_name": gauge.ref_feature_name})

    point = CalibrationPoint(**point_kwargs)
    modeled = extract_modeled(point, Path(plan_hdf))
    return _coerce_series(modeled, value_name="modeled")


def _build_comparisons(
    plan_hdfs: dict[str, Optional[Path]],
    gauges: Sequence[GaugeRecord],
) -> list[GaugeComparison]:
    comparisons: list[GaugeComparison] = []

    for gauge in gauges:
        modeled_by_plan = dict(gauge.modeled)
        for plan_label, plan_hdf in plan_hdfs.items():
            if plan_label in modeled_by_plan:
                continue
            if plan_hdf is None:
                continue
            modeled_by_plan[plan_label] = _extract_modeled_from_plan(plan_hdf, gauge)

        if not modeled_by_plan:
            raise ValueError(
                f"Gauge '{gauge.name}' has no modeled series and no extractable plan HDF"
            )

        for plan_label, modeled in modeled_by_plan.items():
            aligned = _align_series(gauge.observed, modeled)
            stats = calculate_stats(
                aligned["observed"],
                aligned["modeled"],
                variable=gauge.variable,
            )
            comparisons.append(
                GaugeComparison(
                    gauge_name=gauge.name,
                    plan_label=str(plan_label),
                    variable=gauge.variable,
                    units=gauge.units,
                    observed=aligned["observed"],
                    modeled=aligned["modeled"],
                    aligned=aligned,
                    stats=stats,
                    n_points=len(aligned),
                )
            )

    return comparisons


def _metric_status(metric_name: str, value: float) -> str:
    if not _finite(value):
        return "neutral"
    value = float(value)
    if metric_name == "rmse_pct":
        return "pass" if value <= 15.0 else "fail"
    if metric_name == "pearson_r":
        return "pass" if value >= 0.9 else "fail"
    if metric_name == "pbias":
        return "pass" if abs(value) <= 10.0 else "fail"
    if metric_name == "nse":
        return "pass" if value >= 0.6 else "fail"
    return "neutral"


def _format_metric(metric_name: str, value: float) -> str:
    if not _finite(value):
        return "n/a"
    if metric_name in {"rmse_pct", "pbias"}:
        return f"{float(value):.1f}%"
    return f"{float(value):.3f}"


def _metric_cell(metric_name: str, value: float) -> str:
    status = _metric_status(metric_name, value)
    return (
        f'<td class="metric-{status}">'
        f"{escape(_format_metric(metric_name, value))}"
        "</td>"
    )


def _summary_rows(comparisons: Sequence[GaugeComparison]) -> list[tuple[str, int, dict[str, float]]]:
    rows = []
    by_plan: dict[str, list[GaugeComparison]] = {}
    for comparison in comparisons:
        by_plan.setdefault(comparison.plan_label, []).append(comparison)

    for plan_label, plan_comparisons in by_plan.items():
        rows.append((plan_label, len(plan_comparisons), _average_stats(plan_comparisons)))
    if len(by_plan) > 1:
        rows.append(("Overall", len(comparisons), _average_stats(comparisons)))
    elif comparisons:
        rows[0] = ("Average", len(comparisons), rows[0][2])
    return rows


def _average_stats(comparisons: Sequence[GaugeComparison]) -> dict[str, float]:
    averaged = {}
    for metric_name in _METRIC_ORDER:
        values = [
            comparison.stats.get(metric_name, float("nan"))
            for comparison in comparisons
            if _finite(comparison.stats.get(metric_name, float("nan")))
        ]
        averaged[metric_name] = float(np.mean(values)) if values else float("nan")
    return averaged


def _html_project_intro(ctx: ProjectContext) -> str:
    parts = []
    if ctx.title:
        parts.append(f"<h2>{escape(ctx.title)}</h2>")
    if ctx.description:
        paragraphs = ctx.description.strip().split("\n\n")
        for para in paragraphs:
            parts.append(f"<p>{escape(para.strip())}</p>")
    return "\n".join(parts)


def _html_data_sources(ctx: ProjectContext) -> str:
    if not ctx.data_sources:
        return ""
    parts = ["<h2>Data Sources</h2>", "<table><thead><tr>"]
    headers = set()
    for src in ctx.data_sources:
        headers.update(src.keys())
    col_order = []
    for preferred in ("name", "type", "source", "file", "description", "period", "interval"):
        if preferred in headers:
            col_order.append(preferred)
            headers.discard(preferred)
    col_order.extend(sorted(headers))
    parts.append("".join(f"<th>{escape(col.replace('_', ' ').title())}</th>" for col in col_order))
    parts.append("</tr></thead><tbody>")
    for src in ctx.data_sources:
        parts.append("<tr>" + "".join(
            f"<td>{escape(str(src.get(col, '')))}</td>" for col in col_order
        ) + "</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _html_boundary_conditions(ctx: ProjectContext) -> str:
    if not ctx.boundary_conditions:
        return ""
    parts = ["<h2>Boundary Conditions</h2>", "<table><thead><tr>"]
    headers = set()
    for bc in ctx.boundary_conditions:
        headers.update(bc.keys())
    col_order = []
    for preferred in ("name", "location", "type", "source", "description"):
        if preferred in headers:
            col_order.append(preferred)
            headers.discard(preferred)
    col_order.extend(sorted(headers))
    parts.append("".join(f"<th>{escape(col.replace('_', ' ').title())}</th>" for col in col_order))
    parts.append("</tr></thead><tbody>")
    for bc in ctx.boundary_conditions:
        parts.append("<tr>" + "".join(
            f"<td>{escape(str(bc.get(col, '')))}</td>" for col in col_order
        ) + "</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _build_map_html(ctx: ProjectContext, gauges: Sequence[GaugeRecord]) -> str:
    try:
        import folium
        from folium import GeoJson, Marker, CircleMarker
    except ImportError:
        logger.debug("folium not available — skipping map section")
        return ""

    locations = list(ctx.gauge_locations)
    if not locations:
        for gauge in gauges:
            if gauge.y is not None and gauge.x is not None:
                locations.append({"name": gauge.name, "lat": gauge.y, "lon": gauge.x})

    if not locations and ctx.geometry is None:
        return ""

    center_lat, center_lon = 0.0, 0.0
    if locations:
        center_lat = np.mean([loc["lat"] for loc in locations])
        center_lon = np.mean([loc["lon"] for loc in locations])

    m = folium.Map(location=[center_lat, center_lon], zoom_start=9, tiles="CartoDB positron")

    if ctx.geometry is not None:
        try:
            import geopandas as gpd
            if isinstance(ctx.geometry, gpd.GeoDataFrame):
                geojson_data = ctx.geometry.to_crs("EPSG:4326").__geo_interface__
            elif isinstance(ctx.geometry, dict):
                geojson_data = ctx.geometry
            elif isinstance(ctx.geometry, (list, tuple)):
                for layer in ctx.geometry:
                    _add_geometry_layer(m, layer)
                geojson_data = None
            else:
                geojson_data = None

            if geojson_data is not None:
                _add_geojson_to_map(m, geojson_data)
        except Exception as exc:
            logger.warning("Failed to add geometry to map: %s", exc)

    for loc in locations:
        folium.Marker(
            location=[loc["lat"], loc["lon"]],
            popup=escape(loc.get("name", "")),
            tooltip=escape(loc.get("name", "")),
            icon=folium.Icon(color="red", icon="tint", prefix="fa"),
        ).add_to(m)

    if not locations and ctx.geometry is not None:
        m.fit_bounds(m.get_bounds())

    map_html = m.get_root().render()
    iframe_html = (
        '<h2>Model Geometry &amp; Observation Locations</h2>'
        '<div class="map-container">'
        f'<iframe srcdoc="{escape(map_html, quote=True)}" '
        'width="100%" height="550" style="border:1px solid #d9e2ea; border-radius:4px;" '
        'sandbox="allow-scripts allow-same-origin"></iframe>'
        '</div>'
    )
    return iframe_html


def _add_geometry_layer(m, layer_config: dict) -> None:
    import folium

    geojson = layer_config.get("geojson")
    if geojson is None:
        return

    try:
        import geopandas as gpd
        if isinstance(geojson, gpd.GeoDataFrame):
            geojson = geojson.to_crs("EPSG:4326").__geo_interface__
    except ImportError:
        pass

    style = layer_config.get("style", {})
    name = layer_config.get("name", "Layer")
    folium.GeoJson(
        geojson,
        name=name,
        style_function=lambda feature, s=style: {
            "color": s.get("color", "#1a5276"),
            "weight": s.get("weight", 2),
            "fillOpacity": s.get("fill_opacity", 0.1),
            "fillColor": s.get("fill_color", s.get("color", "#1a5276")),
        },
    ).add_to(m)


def _add_geojson_to_map(m, geojson_data: dict) -> None:
    import folium

    features = geojson_data.get("features", [])
    mesh_features = []
    line_features = []
    other_features = []

    for feat in features:
        geom_type = feat.get("geometry", {}).get("type", "")
        if "Polygon" in geom_type:
            mesh_features.append(feat)
        elif "Line" in geom_type:
            line_features.append(feat)
        else:
            other_features.append(feat)

    if mesh_features:
        folium.GeoJson(
            {"type": "FeatureCollection", "features": mesh_features},
            name="2D Mesh Areas",
            style_function=lambda f: {
                "color": "#1a5276",
                "weight": 1.5,
                "fillOpacity": 0.08,
                "fillColor": "#5dade2",
            },
        ).add_to(m)

    if line_features:
        folium.GeoJson(
            {"type": "FeatureCollection", "features": line_features},
            name="Breaklines / Cross Sections",
            style_function=lambda f: {
                "color": "#d35400",
                "weight": 2,
                "dashArray": "5,5",
            },
        ).add_to(m)

    if other_features:
        folium.GeoJson(
            {"type": "FeatureCollection", "features": other_features},
            name="Other Features",
        ).add_to(m)

    if mesh_features or line_features or other_features:
        folium.LayerControl().add_to(m)


def _html_summary_table(comparisons: Sequence[GaugeComparison]) -> str:
    rows = []
    for label, n_comparisons, stats in _summary_rows(comparisons):
        rows.append(
            "<tr>"
            f"<td>{escape(label)}</td>"
            f"<td>{n_comparisons}</td>"
            + "".join(_metric_cell(metric, stats.get(metric, float("nan"))) for metric in _METRIC_ORDER)
            + "</tr>"
        )
    return (
        "<h2>Global Summary</h2>"
        '<p class="summary-note">Metric cells follow DSS-Commander thresholds: '
        "RMSE% <= 15%, r >= 0.9, PBIAS within +/-10%, NSE >= 0.6. KGE is reported without a DSS color threshold.</p>"
        "<table><thead><tr>"
        "<th>Plan</th><th>Gauge comparisons</th><th>RMSE%</th><th>Pearson r</th><th>PBIAS</th><th>NSE</th><th>KGE</th>"
        "</tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table>"
    )


def _html_stats_table(comparisons: Sequence[GaugeComparison]) -> str:
    rows = []
    for comparison in comparisons:
        rows.append(
            "<tr>"
            f"<td>{escape(comparison.gauge_name)}</td>"
            f"<td>{escape(comparison.plan_label)}</td>"
            f"<td>{escape(comparison.variable)}</td>"
            f"<td>{comparison.n_points}</td>"
            + "".join(_metric_cell(metric, comparison.stats.get(metric, float("nan"))) for metric in _METRIC_ORDER)
            + "</tr>"
        )
    return (
        "<h2>Gauge Statistics</h2>"
        "<table><thead><tr>"
        "<th>Gauge</th><th>Plan</th><th>Variable</th><th>Points</th>"
        "<th>RMSE%</th><th>Pearson r</th><th>PBIAS</th><th>NSE</th><th>KGE</th>"
        "</tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table>"
    )


def _bokeh_resources_and_components(comparisons: Sequence[GaugeComparison]) -> tuple[str, str]:
    try:
        from bokeh.embed import components
        from bokeh.models import ColumnDataSource, HoverTool
        from bokeh.plotting import figure
        from bokeh.resources import INLINE
    except Exception as exc:
        raise RuntimeError(
            "Bokeh is required to generate calibration report plots. "
            "Install bokeh or provide an environment with pipeline requirements."
        ) from exc

    plots = []
    colors = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]
    for comparison in comparisons:
        index = comparison.aligned.index
        x_axis_type = "datetime" if isinstance(index, pd.DatetimeIndex) else "linear"
        x_values = index.to_pydatetime() if isinstance(index, pd.DatetimeIndex) else list(range(len(index)))
        source = ColumnDataSource(
            {
                "x": list(x_values),
                "observed": comparison.aligned["observed"].to_numpy(dtype=float),
                "modeled": comparison.aligned["modeled"].to_numpy(dtype=float),
            }
        )
        y_label = comparison.units or comparison.variable
        plot = figure(
            title=f"{comparison.gauge_name} - {comparison.plan_label}",
            x_axis_type=x_axis_type,
            width=1040,
            height=360,
            tools="pan,wheel_zoom,box_zoom,reset,save",
            toolbar_location="above",
        )
        plot.line("x", "observed", source=source, line_width=2, color="#1a5276", legend_label="Observed")
        plot.scatter("x", "observed", source=source, size=4, color="#1a5276", alpha=0.75)
        plot.line(
            "x",
            "modeled",
            source=source,
            line_width=2,
            color=colors[len(plots) % len(colors)],
            legend_label="Modeled",
        )
        plot.xaxis.axis_label = "Time" if x_axis_type == "datetime" else "Time step"
        plot.yaxis.axis_label = y_label
        plot.legend.click_policy = "hide"
        plot.add_tools(
            HoverTool(
                tooltips=[
                    ("Observed", "@observed{0.000}"),
                    ("Modeled", "@modeled{0.000}"),
                ],
                mode="vline",
            )
        )
        plots.append(plot)

    script, divs = components(plots)
    if isinstance(divs, str):
        div_list = [divs]
    else:
        div_list = list(divs)

    plot_html = ["<h2>Time Series Comparison Plots</h2>"]
    for comparison, div in zip(comparisons, div_list):
        plot_html.append(
            '<div class="plot-block">'
            f'<div class="plot-title">{escape(comparison.gauge_name)} - {escape(comparison.plan_label)}</div>'
            f"{div}"
            "</div>"
        )

    return INLINE.render() + script, "\n".join(plot_html)


def generate_calibration_report(
    plan_hdfs: Any,
    observed_data: Any,
    output_path: Path,
    project_context: Optional[ProjectContext | Mapping[str, Any]] = None,
) -> Path:
    """
    Generate a self-contained HTML calibration report.

    Args:
        plan_hdfs: Path/list/dict of HEC-RAS plan HDF files. May be empty when
            observed_data supplies modeled series directly for mock/report tests.
        observed_data: Gauge observed data. Supported forms include a mapping of
            gauge names to config dictionaries, a list of config dictionaries, or
            a DataFrame with gauge/time/observed columns and optional modeled data.
        output_path: Path to write the HTML report.
        project_context: Optional ProjectContext or dict with project metadata
            (title, description, data_sources, boundary_conditions, geometry,
            gauge_locations). When provided, the report starts with an introduction,
            an interactive map of model geometry and gauge locations, and tables
            describing data sources and boundary conditions.

    Returns:
        Path to the written HTML file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    plan_map = _normalize_plan_hdfs(plan_hdfs)
    gauges = _coerce_observed_data(observed_data, list(plan_map.keys()))
    comparisons = _build_comparisons(plan_map, gauges)
    if not comparisons:
        raise ValueError("No gauge comparisons were available for calibration report generation")

    ctx = _coerce_project_context(project_context)

    bokeh_resources, plots_html = _bokeh_resources_and_components(comparisons)
    run_ts = _utc_timestamp()
    plan_count = len(plan_map) if plan_map else len({comparison.plan_label for comparison in comparisons})

    context_sections = []
    if ctx is not None:
        intro = _html_project_intro(ctx)
        if intro:
            context_sections.append(intro)
        map_html = _build_map_html(ctx, gauges)
        if map_html:
            context_sections.append(map_html)
        ds_html = _html_data_sources(ctx)
        if ds_html:
            context_sections.append(ds_html)
        bc_html = _html_boundary_conditions(ctx)
        if bc_html:
            context_sections.append(bc_html)

    report_title = escape(ctx.title) if ctx and ctx.title else "RAS Agent Calibration Report"

    body = "\n".join(
        [
            f"<h1>{report_title}</h1>",
            '<p class="meta">'
            f"<strong>Generated:</strong> {escape(run_ts)}<br>"
            f"<strong>Gauge comparisons:</strong> {len(comparisons)}<br>"
            f"<strong>Plans:</strong> {plan_count}<br>"
            f"<strong>RAS Agent calibration report version:</strong> {_VERSION}"
            "</p>",
            *context_sections,
            _html_summary_table(comparisons),
            _html_stats_table(comparisons),
            plots_html,
            '<div class="footer">Generated by RAS Agent calibration reporting (Apache 2.0)</div>',
        ]
    )

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{report_title}</title>
  <style>
{_CSS}  </style>
{bokeh_resources}
</head>
<body>
{body}
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    logger.info("[CalibrationReport] Written to %s", output_path)
    return output_path


def _coerce_project_context(raw: Any) -> Optional[ProjectContext]:
    if raw is None:
        return None
    if isinstance(raw, ProjectContext):
        return raw
    if isinstance(raw, Mapping):
        return ProjectContext(
            title=str(raw.get("title", "")),
            description=str(raw.get("description", "")),
            data_sources=list(raw.get("data_sources", [])),
            boundary_conditions=list(raw.get("boundary_conditions", [])),
            geometry=raw.get("geometry"),
            gauge_locations=list(raw.get("gauge_locations", [])),
            crs=str(raw.get("crs", "EPSG:4326")),
        )
    return None


__all__ = [
    "GaugeComparison",
    "GaugeRecord",
    "ProjectContext",
    "calculate_stats",
    "generate_calibration_report",
]
