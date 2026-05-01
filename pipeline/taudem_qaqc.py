"""
taudem_qaqc.py - TauDEM delineation QAQC bundle generation.

This module builds the reviewer-facing package that sits beside direct TauDEM
CLI outputs. It does not run TauDEM or alter delineation results; it summarizes
the artifacts, diagnostics, figures, and human signoff state required before a
watershed can be promoted to production-quality model build inputs.
"""

from __future__ import annotations

import csv
import html
import json
import math
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import geopandas as gpd
import numpy as np
import rasterio
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union


QAQC_SCHEMA_VERSION = "taudem-delineation-qaqc/v1"
SIGNOFF_SCHEMA_VERSION = "taudem-delineation-signoff/v1"

REVIEW_TOPICS = [
    "outlet_snapping",
    "stream_threshold",
    "stream_order",
    "drainage_area",
    "subbasin_geometry",
    "slope",
    "low_relief_risk",
    "dem_boundary_effects",
]

ARTIFACT_ROLES = {
    "fel": "pit-filled DEM",
    "p": "D8 flow direction grid",
    "sd8": "D8 slope grid",
    "ad8": "D8 contributing-area grid",
    "src": "stream source grid from threshold",
    "plen": "grid-network path length",
    "tlen": "grid-network total path length",
    "gord": "grid-network order grid",
    "ord": "stream order grid",
    "tree": "TauDEM StreamNet tree table",
    "coord": "TauDEM StreamNet coordinate table",
    "net": "TauDEM StreamNet vector network",
    "w": "TauDEM watershed/subbasin grid",
    "outlet": "original reviewer-supplied outlet",
    "snapped_outlet": "TauDEM snapped outlet",
    "dem_clipped": "DEM clipped to delineated watershed",
}


@dataclass(frozen=True)
class QaqcThresholds:
    """Thresholds for first-pass TauDEM QAQC attention flags."""

    outlet_attention_fraction: float = 0.50
    low_slope_m_per_m: float = 0.0005
    low_relief_m: float = 5.0
    subbasin_area_mismatch_pct: float = 2.0
    dem_boundary_margin_cells: float = 2.0
    sliver_area_fraction: float = 0.01


def generate_taudem_qaqc_bundle(
    watershed: Any,
    output_dir: Path,
    *,
    detail_level: str = "first_pass",
    source_dem: Optional[Path] = None,
    snap_threshold_m: Optional[float] = None,
    min_stream_area_km2: Optional[float] = None,
    taudem_commands: Optional[Iterable[Any]] = None,
    thresholds: Optional[QaqcThresholds | dict[str, float]] = None,
    notes: Optional[dict[str, Any]] = None,
) -> dict[str, Path]:
    """
    Write a self-contained TauDEM delineation QAQC bundle.

    Args:
        watershed: WatershedResult-like object with basin, streams, subbasins,
            pour_point, characteristics, dem_clipped, and artifacts attributes.
        output_dir: Bundle directory to create.
        detail_level: "first_pass" for automated low-detail review package, or
            "production_review" for the same package generated during promotion.
        source_dem: Original DEM used for TauDEM processing, if available.
        snap_threshold_m: Maximum outlet snapping distance requested.
        min_stream_area_km2: Minimum drainage area used for stream thresholding.
        taudem_commands: TauDEM command result objects to record as provenance.
        thresholds: Optional attention thresholds.
        notes: Optional freeform provenance notes.

    Returns:
        Mapping of artifact keys to paths written in the bundle.
    """
    if detail_level not in {"first_pass", "production_review"}:
        raise ValueError("detail_level must be 'first_pass' or 'production_review'")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    maps_dir = output_dir / "maps"
    tables_dir = output_dir / "tables"
    maps_dir.mkdir(parents=True, exist_ok=True)
    tables_dir.mkdir(parents=True, exist_ok=True)

    threshold_values = _coerce_thresholds(thresholds)
    diagnostics = build_taudem_qaqc_diagnostics(
        watershed,
        detail_level=detail_level,
        source_dem=source_dem,
        snap_threshold_m=snap_threshold_m,
        min_stream_area_km2=min_stream_area_km2,
        thresholds=threshold_values,
    )

    map_paths = _write_maps(watershed, maps_dir)
    table_paths = _write_tables(watershed, diagnostics, tables_dir, output_dir)

    diagnostics_path = output_dir / "diagnostics.json"
    diagnostics_path.write_text(_to_json(diagnostics), encoding="utf-8")

    command_log_path = output_dir / "command_log.json"
    command_log = {
        "schema_version": QAQC_SCHEMA_VERSION,
        "generated_at": _utcnow(),
        "commands": [_serialize_command_result(command) for command in (taudem_commands or [])],
    }
    command_log_path.write_text(_to_json(command_log), encoding="utf-8")

    prompts_path = output_dir / "review_prompts.md"
    prompts_path.write_text(_review_prompts_markdown(diagnostics), encoding="utf-8")

    signoff_path = output_dir / "signoff.json"
    signoff_path.write_text(_to_json(_pending_signoff()), encoding="utf-8")

    manifest = _build_manifest(
        watershed=watershed,
        output_dir=output_dir,
        detail_level=detail_level,
        source_dem=source_dem,
        diagnostics=diagnostics,
        map_paths=map_paths,
        table_paths=table_paths,
        command_log_path=command_log_path,
        prompts_path=prompts_path,
        signoff_path=signoff_path,
        notes=notes or {},
    )
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(_to_json(manifest), encoding="utf-8")

    report_path = output_dir / "qaqc_report.html"
    report_path.write_text(
        _html_report(
            diagnostics=diagnostics,
            manifest=manifest,
            map_paths=map_paths,
            table_paths=table_paths,
        ),
        encoding="utf-8",
    )

    return {
        "bundle_dir": output_dir,
        "manifest": manifest_path,
        "diagnostics": diagnostics_path,
        "report_html": report_path,
        "review_prompts": prompts_path,
        "signoff": signoff_path,
        "command_log": command_log_path,
        **{f"map_{key}": path for key, path in map_paths.items()},
        **{f"table_{key}": path for key, path in table_paths.items()},
    }


def build_taudem_qaqc_diagnostics(
    watershed: Any,
    *,
    detail_level: str = "first_pass",
    source_dem: Optional[Path] = None,
    snap_threshold_m: Optional[float] = None,
    min_stream_area_km2: Optional[float] = None,
    thresholds: Optional[QaqcThresholds] = None,
) -> dict[str, Any]:
    """Build structured TauDEM delineation diagnostics without writing files."""
    thresholds = thresholds or QaqcThresholds()
    generated_at = _utcnow()
    characteristics = getattr(watershed, "characteristics", None)
    extra = getattr(characteristics, "extra", {}) or {}
    artifacts = _artifact_paths(getattr(watershed, "artifacts", {}) or {})

    basin_gdf = getattr(watershed, "basin", None)
    streams_gdf = getattr(watershed, "streams", None)
    subbasins_gdf = getattr(watershed, "subbasins", None)
    basin_geom = _union_geometry(basin_gdf)
    source_bounds = extra.get("source_bounds")
    if source_bounds is None and source_dem is not None and Path(source_dem).exists():
        with rasterio.open(source_dem) as src:
            source_bounds = tuple(src.bounds)

    cell_area_km2 = _float_or_none(extra.get("cell_area_km2"))
    cell_size_m = math.sqrt(cell_area_km2) * 1000.0 if cell_area_km2 and cell_area_km2 > 0 else None
    threshold_cells = _float_or_none(extra.get("threshold_cells"))

    checks = [
        _outlet_snapping_check(watershed, artifacts, snap_threshold_m, thresholds),
        _stream_threshold_check(streams_gdf, threshold_cells, cell_area_km2, min_stream_area_km2),
        _stream_order_check(streams_gdf, artifacts),
        _drainage_area_check(watershed, basin_geom, subbasins_gdf, thresholds),
        _subbasin_geometry_check(subbasins_gdf, basin_geom, thresholds),
        _slope_check(characteristics),
        _low_relief_check(characteristics, thresholds),
        _dem_boundary_check(basin_geom, source_bounds, cell_size_m, thresholds),
    ]

    attention = [
        check for check in checks
        if check["status"] in {"needs_attention", "unknown"}
    ]
    high_attention = [
        check for check in attention
        if check.get("severity") == "high"
    ]

    drainage_area_km2 = _float_or_none(getattr(characteristics, "drainage_area_km2", None))
    stream_count = _safe_len(streams_gdf)
    subbasin_count = _safe_len(subbasins_gdf)

    return {
        "schema_version": QAQC_SCHEMA_VERSION,
        "generated_at": generated_at,
        "detail_level": detail_level,
        "summary": {
            "status": "attention_required" if attention else "reviewer_signoff_required",
            "attention_count": len(attention),
            "high_attention_count": len(high_attention),
            "human_signoff_required": True,
            "production_promotion_allowed": False,
        },
        "metrics": {
            "drainage_area_km2": drainage_area_km2,
            "drainage_area_mi2": _float_or_none(getattr(characteristics, "drainage_area_mi2", None)),
            "stream_reach_count": stream_count,
            "subbasin_count": subbasin_count,
            "main_channel_length_km": _float_or_none(getattr(characteristics, "main_channel_length_km", None)),
            "main_channel_slope_m_per_m": _float_or_none(getattr(characteristics, "main_channel_slope_m_per_m", None)),
            "relief_m": _float_or_none(getattr(characteristics, "relief_m", None)),
            "threshold_cells": threshold_cells,
            "cell_area_km2": cell_area_km2,
            "computed_min_stream_area_km2": (
                threshold_cells * cell_area_km2
                if threshold_cells is not None and cell_area_km2 is not None
                else None
            ),
            "source_dem": str(source_dem) if source_dem else None,
        },
        "checks": checks,
        "review_prompts": _review_prompts(checks),
    }


def record_taudem_qaqc_signoff(
    bundle_dir: Path,
    *,
    reviewer: str,
    decision: str,
    notes: str,
    approved_for_production: bool,
    reviewer_role: Optional[str] = None,
    reviewed_at: Optional[str] = None,
) -> Path:
    """
    Record human signoff metadata for a TauDEM QAQC bundle.

    Production promotion is only represented as allowed when
    approved_for_production is True and decision is "approved".
    """
    decision = decision.lower().strip()
    if decision not in {"approved", "needs_changes", "rejected"}:
        raise ValueError("decision must be approved, needs_changes, or rejected")
    if approved_for_production and decision != "approved":
        raise ValueError("approved_for_production requires decision='approved'")

    bundle_dir = Path(bundle_dir)
    signoff_path = bundle_dir / "signoff.json"
    if not signoff_path.exists():
        raise FileNotFoundError(f"TauDEM QAQC signoff template not found: {signoff_path}")

    payload = json.loads(signoff_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "schema_version": SIGNOFF_SCHEMA_VERSION,
            "status": "signed" if approved_for_production else decision,
            "reviewer": reviewer,
            "reviewer_role": reviewer_role,
            "reviewed_at": reviewed_at or _utcnow(),
            "decision": decision,
            "approved_for_production": bool(approved_for_production),
            "notes": notes,
            "production_promotion": {
                "allowed": bool(approved_for_production),
                "reason": (
                    "Human reviewer approved the TauDEM delineation QAQC bundle."
                    if approved_for_production
                    else "Human reviewer has not approved production promotion."
                ),
            },
        }
    )
    signoff_path.write_text(_to_json(payload), encoding="utf-8")
    return signoff_path


def require_taudem_qaqc_signoff(bundle_dir: Path) -> dict[str, Any]:
    """Return signoff metadata or raise if production promotion is not approved."""
    signoff_path = Path(bundle_dir) / "signoff.json"
    if not signoff_path.exists():
        raise FileNotFoundError(f"TauDEM QAQC signoff file not found: {signoff_path}")
    signoff = json.loads(signoff_path.read_text(encoding="utf-8"))
    if not signoff.get("approved_for_production"):
        raise RuntimeError(
            "TauDEM delineation QAQC bundle has not been approved for production promotion."
        )
    return signoff


def _coerce_thresholds(value: Optional[QaqcThresholds | dict[str, float]]) -> QaqcThresholds:
    if value is None:
        return QaqcThresholds()
    if isinstance(value, QaqcThresholds):
        return value
    return QaqcThresholds(**value)


def _outlet_snapping_check(
    watershed: Any,
    artifacts: dict[str, Path],
    snap_threshold_m: Optional[float],
    thresholds: QaqcThresholds,
) -> dict[str, Any]:
    snapped = getattr(watershed, "pour_point", None)
    target_crs = getattr(getattr(watershed, "basin", None), "crs", None)
    original = None
    if "outlet" in artifacts:
        original = _read_point(artifacts["outlet"], target_crs)

    snap_distance_m = None
    status = "unknown"
    severity = "medium"
    if isinstance(original, Point) and isinstance(snapped, Point):
        snap_distance_m = float(original.distance(snapped))
        attention_threshold = None
        if snap_threshold_m is not None:
            attention_threshold = max(float(snap_threshold_m) * thresholds.outlet_attention_fraction, 0.0)
        if snap_threshold_m is not None and snap_distance_m > float(snap_threshold_m):
            status = "needs_attention"
            severity = "high"
        elif attention_threshold is not None and snap_distance_m >= attention_threshold:
            status = "needs_attention"
            severity = "medium"
        else:
            status = "review"
            severity = "info"

    return _check(
        check_id="outlet_snapping",
        label="Outlet snapping",
        status=status,
        severity=severity,
        metrics={
            "snap_distance_m": snap_distance_m,
            "snap_threshold_m": snap_threshold_m,
            "original_outlet_available": original is not None,
            "snapped_outlet_available": isinstance(snapped, Point),
        },
        prompt=(
            "Confirm the snapped outlet lands on the intended channel and does not "
            "jump across a divide, road embankment, culvert, or parallel drainage path."
        ),
        action=(
            "Adjust the outlet point or stream threshold and rerun TauDEM if the snapped "
            "point is not hydraulically defensible."
        ),
    )


def _stream_threshold_check(
    streams_gdf: Any,
    threshold_cells: Optional[float],
    cell_area_km2: Optional[float],
    min_stream_area_km2: Optional[float],
) -> dict[str, Any]:
    stream_count = _safe_len(streams_gdf)
    computed_area = (
        threshold_cells * cell_area_km2
        if threshold_cells is not None and cell_area_km2 is not None
        else None
    )
    status = "review"
    severity = "info"
    if stream_count == 0:
        status = "needs_attention"
        severity = "high"
    elif stream_count == 1:
        status = "needs_attention"
        severity = "medium"

    return _check(
        check_id="stream_threshold",
        label="Stream threshold",
        status=status,
        severity=severity,
        metrics={
            "stream_reach_count": stream_count,
            "threshold_cells": threshold_cells,
            "cell_area_km2": cell_area_km2,
            "computed_min_stream_area_km2": computed_area,
            "requested_min_stream_area_km2": min_stream_area_km2,
        },
        prompt=(
            "Review whether the drainage-area threshold over- or under-generates the "
            "channel network for this Illinois basin."
        ),
        action=(
            "Compare the stream network against aerial/NHD/context layers and rerun "
            "with a different threshold if the network density is not defensible."
        ),
    )


def _stream_order_check(streams_gdf: Any, artifacts: dict[str, Path]) -> dict[str, Any]:
    orders = _stream_orders_from_vector(streams_gdf)
    order_source = "vector"
    if not orders:
        orders = _stream_orders_from_raster(artifacts.get("ord"))
        order_source = "ord_grid" if orders else None

    status = "unknown"
    severity = "medium"
    metrics: dict[str, Any] = {
        "order_source": order_source,
        "min_order": None,
        "max_order": None,
        "order_values": [],
    }
    if orders:
        order_values = sorted({int(value) for value in orders if value is not None})
        metrics.update(
            {
                "min_order": min(order_values),
                "max_order": max(order_values),
                "order_values": order_values,
            }
        )
        if max(order_values) <= 1 and _safe_len(streams_gdf) > 1:
            status = "needs_attention"
            severity = "medium"
        else:
            status = "review"
            severity = "info"

    return _check(
        check_id="stream_order",
        label="Stream order",
        status=status,
        severity=severity,
        metrics=metrics,
        prompt=(
            "Verify stream order and network hierarchy at confluences before using the "
            "delineation for boundary-condition placement or HMS basin construction."
        ),
        action=(
            "Inspect the order grid/vector attributes and rerun TauDEM if confluences "
            "or headwater reaches are misrepresented."
        ),
    )


def _drainage_area_check(
    watershed: Any,
    basin_geom: Optional[Any],
    subbasins_gdf: Any,
    thresholds: QaqcThresholds,
) -> dict[str, Any]:
    characteristics = getattr(watershed, "characteristics", None)
    reported_area = _float_or_none(getattr(characteristics, "drainage_area_km2", None))
    basin_area = basin_geom.area / 1e6 if basin_geom is not None else None
    subbasin_area = _geometry_area_km2(subbasins_gdf)
    mismatch_pct = None
    if basin_area and subbasin_area is not None:
        mismatch_pct = abs(subbasin_area - basin_area) / basin_area * 100.0

    status = "review"
    severity = "info"
    if reported_area is None or basin_area is None or subbasin_area is None:
        status = "unknown"
        severity = "medium"
    elif mismatch_pct is not None and mismatch_pct > thresholds.subbasin_area_mismatch_pct:
        status = "needs_attention"
        severity = "high"

    return _check(
        check_id="drainage_area",
        label="Drainage area",
        status=status,
        severity=severity,
        metrics={
            "reported_area_km2": reported_area,
            "basin_geometry_area_km2": basin_area,
            "subbasin_area_sum_km2": subbasin_area,
            "subbasin_vs_basin_mismatch_pct": mismatch_pct,
        },
        prompt=(
            "Compare the TauDEM drainage area against expected gauge, NHD, official "
            "basin, or engineering reference areas."
        ),
        action=(
            "Resolve material drainage-area disagreement before using the delineation "
            "as model-build input."
        ),
    )


def _subbasin_geometry_check(
    subbasins_gdf: Any,
    basin_geom: Optional[Any],
    thresholds: QaqcThresholds,
) -> dict[str, Any]:
    count = _safe_len(subbasins_gdf)
    invalid_count = 0
    sliver_count = 0
    min_area_km2 = None
    if _safe_len(subbasins_gdf) > 0:
        invalid_count = int((~subbasins_gdf.geometry.is_valid).sum())
        areas_km2 = np.asarray(subbasins_gdf.geometry.area, dtype=float) / 1e6
        min_area_km2 = float(areas_km2.min()) if areas_km2.size else None
        basin_area = basin_geom.area / 1e6 if basin_geom is not None else float(areas_km2.sum())
        if basin_area > 0:
            sliver_count = int((areas_km2 < basin_area * thresholds.sliver_area_fraction).sum())

    status = "review"
    severity = "info"
    if count == 0 or invalid_count > 0:
        status = "needs_attention"
        severity = "high"
    elif sliver_count > 0:
        status = "needs_attention"
        severity = "medium"

    return _check(
        check_id="subbasin_geometry",
        label="Subbasin geometry",
        status=status,
        severity=severity,
        metrics={
            "subbasin_count": count,
            "invalid_geometry_count": invalid_count,
            "small_subbasin_count": sliver_count,
            "minimum_subbasin_area_km2": min_area_km2,
        },
        prompt=(
            "Inspect subbasin polygons for slivers, holes, invalid geometry, and "
            "unexpected splits near the outlet or DEM boundary."
        ),
        action=(
            "Regenerate or repair the delineation before model promotion if subbasin "
            "geometry is not suitable for engineering review."
        ),
    )


def _slope_check(characteristics: Any) -> dict[str, Any]:
    slope = _float_or_none(getattr(characteristics, "main_channel_slope_m_per_m", None))
    length = _float_or_none(getattr(characteristics, "main_channel_length_km", None))
    relief = _float_or_none(getattr(characteristics, "relief_m", None))

    status = "review"
    severity = "info"
    if slope is None or slope <= 0:
        status = "needs_attention"
        severity = "high"
    elif slope < 0.0005:
        status = "needs_attention"
        severity = "medium"
    elif slope > 0.05:
        status = "needs_attention"
        severity = "medium"

    return _check(
        check_id="slope",
        label="Main-channel slope",
        status=status,
        severity=severity,
        metrics={
            "main_channel_slope_m_per_m": slope,
            "main_channel_length_km": length,
            "relief_m": relief,
        },
        prompt=(
            "Confirm the computed main-channel slope is plausible for the selected "
            "outlet and channel path."
        ),
        action=(
            "Review DEM conditioning and main-channel selection when slope is flat, "
            "negative, or implausibly steep."
        ),
    )


def _low_relief_check(characteristics: Any, thresholds: QaqcThresholds) -> dict[str, Any]:
    relief = _float_or_none(getattr(characteristics, "relief_m", None))
    slope = _float_or_none(getattr(characteristics, "main_channel_slope_m_per_m", None))

    status = "review"
    severity = "info"
    if relief is None or slope is None:
        status = "unknown"
        severity = "medium"
    elif relief <= thresholds.low_relief_m or slope <= thresholds.low_slope_m_per_m:
        status = "needs_attention"
        severity = "medium"

    return _check(
        check_id="low_relief_risk",
        label="Low-relief risk",
        status=status,
        severity=severity,
        metrics={
            "relief_m": relief,
            "main_channel_slope_m_per_m": slope,
            "low_relief_threshold_m": thresholds.low_relief_m,
            "low_slope_threshold_m_per_m": thresholds.low_slope_m_per_m,
        },
        prompt=(
            "Decide whether low relief, agricultural drainage, levees, road fills, or "
            "DEM artifacts could control the divide instead of TauDEM D8 flow paths."
        ),
        action=(
            "Escalate low-relief basins for manual map review and parameter sensitivity "
            "before accepting the delineation."
        ),
    )


def _dem_boundary_check(
    basin_geom: Optional[Any],
    source_bounds: Optional[Any],
    cell_size_m: Optional[float],
    thresholds: QaqcThresholds,
) -> dict[str, Any]:
    margin_m = None
    source_bounds_tuple = _bounds_tuple(source_bounds)
    status = "unknown"
    severity = "medium"

    if basin_geom is not None and source_bounds_tuple is not None:
        minx, miny, maxx, maxy = basin_geom.bounds
        sxmin, symin, sxmax, symax = source_bounds_tuple
        margin_m = min(minx - sxmin, miny - symin, sxmax - maxx, symax - maxy)
        if cell_size_m is None:
            status = "review"
            severity = "info"
        elif margin_m <= cell_size_m * thresholds.dem_boundary_margin_cells:
            status = "needs_attention"
            severity = "high"
        else:
            status = "review"
            severity = "info"

    return _check(
        check_id="dem_boundary_effects",
        label="DEM boundary effects",
        status=status,
        severity=severity,
        metrics={
            "minimum_margin_to_source_dem_m": margin_m,
            "cell_size_m": cell_size_m,
            "margin_threshold_cells": thresholds.dem_boundary_margin_cells,
            "source_bounds": list(source_bounds_tuple) if source_bounds_tuple else None,
        },
        prompt=(
            "Check whether the watershed touches the DEM processing edge or whether "
            "upstream area could be clipped by the terrain request boundary."
        ),
        action=(
            "Expand the terrain request and rerun TauDEM if the basin boundary is "
            "near or clipped by the source DEM edge."
        ),
    )


def _check(
    *,
    check_id: str,
    label: str,
    status: str,
    severity: str,
    metrics: dict[str, Any],
    prompt: str,
    action: str,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "severity": severity,
        "metrics": metrics,
        "review_prompt": prompt,
        "recommended_action": action,
    }


def _review_prompts(checks: list[dict[str, Any]]) -> list[dict[str, str]]:
    prompts = []
    for check in checks:
        prompts.append(
            {
                "topic": check["id"],
                "status": check["status"],
                "question": check["review_prompt"],
                "evidence": _metric_summary(check.get("metrics", {})),
            }
        )
    return prompts


def _write_maps(watershed: Any, maps_dir: Path) -> dict[str, Path]:
    basin_geom = _union_geometry(getattr(watershed, "basin", None))
    streams = list(_geometries(getattr(watershed, "streams", None)))
    subbasins = list(_geometries(getattr(watershed, "subbasins", None)))
    pour_point = getattr(watershed, "pour_point", None)
    target_crs = getattr(getattr(watershed, "basin", None), "crs", None)
    artifacts = _artifact_paths(getattr(watershed, "artifacts", {}) or {})
    original_outlet = _read_point(artifacts["outlet"], target_crs) if "outlet" in artifacts else None

    source_bounds = None
    characteristics = getattr(watershed, "characteristics", None)
    extra = getattr(characteristics, "extra", {}) or {}
    if extra.get("source_bounds") is not None:
        source_bounds_tuple = _bounds_tuple(extra["source_bounds"])
        if source_bounds_tuple is not None:
            source_bounds = box(*source_bounds_tuple)

    maps = {
        "outlet_snapping": _svg_map(
            "Outlet snapping",
            [
                ("Basin", [basin_geom] if basin_geom is not None else [], {"fill": "#eef6f8", "stroke": "#305f72", "stroke_width": 1.2}),
                ("Streams", streams, {"fill": "none", "stroke": "#1f77b4", "stroke_width": 2.0}),
                ("Original outlet", [original_outlet] if original_outlet is not None else [], {"fill": "#d95f02", "stroke": "#7a2f00", "radius": 4.5}),
                ("Snapped outlet", [pour_point] if isinstance(pour_point, Point) else [], {"fill": "#009e73", "stroke": "#00573f", "radius": 4.5}),
            ],
        ),
        "stream_order": _svg_map(
            "Stream network and order review",
            [
                ("Basin", [basin_geom] if basin_geom is not None else [], {"fill": "#f7f7f2", "stroke": "#4d4d4d", "stroke_width": 1.0}),
                ("Streams", streams, {"fill": "none", "stroke": "#0067a0", "stroke_width": 2.2}),
                ("Outlet", [pour_point] if isinstance(pour_point, Point) else [], {"fill": "#c43b32", "stroke": "#6f1f1a", "radius": 4.0}),
            ],
        ),
        "subbasins": _svg_map(
            "Subbasin geometry",
            [
                ("Subbasins", subbasins, {"fill": "#e8d7f1", "stroke": "#6a4c93", "stroke_width": 0.9}),
                ("Streams", streams, {"fill": "none", "stroke": "#1f77b4", "stroke_width": 1.8}),
                ("Outlet", [pour_point] if isinstance(pour_point, Point) else [], {"fill": "#c43b32", "stroke": "#6f1f1a", "radius": 4.0}),
            ],
        ),
        "dem_boundary_effects": _svg_map(
            "DEM boundary effects",
            [
                ("Source DEM bounds", [source_bounds] if source_bounds is not None else [], {"fill": "none", "stroke": "#d95f02", "stroke_width": 1.6, "dash": "5 4"}),
                ("Basin", [basin_geom] if basin_geom is not None else [], {"fill": "#eef6f8", "stroke": "#305f72", "stroke_width": 1.2}),
                ("Streams", streams, {"fill": "none", "stroke": "#1f77b4", "stroke_width": 1.8}),
            ],
        ),
    }

    paths = {}
    for key, svg in maps.items():
        path = maps_dir / f"{key}.svg"
        path.write_text(svg, encoding="utf-8")
        paths[key] = path
    return paths


def _write_tables(
    watershed: Any,
    diagnostics: dict[str, Any],
    tables_dir: Path,
    bundle_dir: Path,
) -> dict[str, Path]:
    paths = {}

    diagnostics_rows = []
    for check in diagnostics["checks"]:
        diagnostics_rows.append(
            {
                "check_id": check["id"],
                "label": check["label"],
                "status": check["status"],
                "severity": check["severity"],
                "metric_summary": _metric_summary(check.get("metrics", {})),
                "recommended_action": check["recommended_action"],
            }
        )
    diagnostics_path = tables_dir / "diagnostics.csv"
    _write_csv(diagnostics_path, diagnostics_rows, [
        "check_id",
        "label",
        "status",
        "severity",
        "metric_summary",
        "recommended_action",
    ])
    paths["diagnostics"] = diagnostics_path

    artifact_rows = _artifact_rows(watershed, bundle_dir)
    artifacts_path = tables_dir / "artifacts.csv"
    _write_csv(artifacts_path, artifact_rows, [
        "key",
        "role",
        "path",
        "relative_to_bundle",
        "exists",
        "size_bytes",
    ])
    paths["artifacts"] = artifacts_path

    subbasin_rows = _subbasin_rows(getattr(watershed, "subbasins", None))
    subbasins_path = tables_dir / "subbasins.csv"
    _write_csv(subbasins_path, subbasin_rows, [
        "subbasin_id",
        "area_km2",
        "is_valid",
        "bounds",
    ])
    paths["subbasins"] = subbasins_path

    return paths


def _build_manifest(
    *,
    watershed: Any,
    output_dir: Path,
    detail_level: str,
    source_dem: Optional[Path],
    diagnostics: dict[str, Any],
    map_paths: dict[str, Path],
    table_paths: dict[str, Path],
    command_log_path: Path,
    prompts_path: Path,
    signoff_path: Path,
    notes: dict[str, Any],
) -> dict[str, Any]:
    artifacts = _artifact_rows(watershed, output_dir)
    bundle_outputs = {
        "qaqc_report_html": "qaqc_report.html",
        "diagnostics_json": "diagnostics.json",
        "command_log_json": _relpath(command_log_path, output_dir),
        "review_prompts_md": _relpath(prompts_path, output_dir),
        "signoff_json": _relpath(signoff_path, output_dir),
        "maps": {key: _relpath(path, output_dir) for key, path in map_paths.items()},
        "tables": {key: _relpath(path, output_dir) for key, path in table_paths.items()},
    }
    return {
        "schema_version": QAQC_SCHEMA_VERSION,
        "generated_at": _utcnow(),
        "detail_level": detail_level,
        "source": {
            "engine": "TauDEM CLI",
            "source_dem": str(source_dem) if source_dem else None,
            "source_dem_exists": Path(source_dem).exists() if source_dem else None,
        },
        "bundle_outputs": bundle_outputs,
        "diagnostic_summary": diagnostics["summary"],
        "artifacts": artifacts,
        "review_topics": REVIEW_TOPICS,
        "production_promotion": {
            "allowed": False,
            "blocked_by": "human_signoff_required",
            "required_signoff": _relpath(signoff_path, output_dir),
        },
        "notes": notes,
    }


def _pending_signoff() -> dict[str, Any]:
    return {
        "schema_version": SIGNOFF_SCHEMA_VERSION,
        "status": "pending",
        "reviewer": None,
        "reviewer_role": None,
        "reviewed_at": None,
        "decision": None,
        "approved_for_production": False,
        "notes": "",
        "checklist": {topic: False for topic in REVIEW_TOPICS},
        "production_promotion": {
            "allowed": False,
            "reason": "Human engineering signoff is required before production promotion.",
        },
    }


def _review_prompts_markdown(diagnostics: dict[str, Any]) -> str:
    lines = [
        "# TauDEM Delineation QAQC Review Prompts",
        "",
        "Production promotion remains blocked until `signoff.json` records human approval.",
        "",
        "## Checklist",
        "",
    ]
    for prompt in diagnostics["review_prompts"]:
        lines.extend(
            [
                f"- [ ] `{prompt['topic']}` ({prompt['status']}): {prompt['question']}",
                f"  Evidence: {prompt['evidence']}",
            ]
        )
    lines.extend(
        [
            "",
            "## Reviewer Signoff",
            "",
            "Record signoff with `record_taudem_qaqc_signoff()` or update `signoff.json` with equivalent metadata after review.",
            "",
        ]
    )
    return "\n".join(lines)


def _html_report(
    *,
    diagnostics: dict[str, Any],
    manifest: dict[str, Any],
    map_paths: dict[str, Path],
    table_paths: dict[str, Path],
) -> str:
    summary = diagnostics["summary"]
    metrics = diagnostics["metrics"]
    check_rows = [
        [
            check["label"],
            check["status"],
            check["severity"],
            _metric_summary(check.get("metrics", {})),
            check["recommended_action"],
        ]
        for check in diagnostics["checks"]
    ]
    map_sections = []
    for key, path in map_paths.items():
        svg = path.read_text(encoding="utf-8")
        map_sections.append(
            f"<section><h2>{_title(key)}</h2>{svg}</section>"
        )

    artifact_table = _read_csv_for_html(table_paths["artifacts"])
    diagnostics_table = _read_csv_for_html(table_paths["diagnostics"])
    subbasins_table = _read_csv_for_html(table_paths["subbasins"])

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TauDEM Delineation QAQC Bundle</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #222; }}
    h1, h2 {{ color: #17324d; }}
    .summary {{ border-left: 5px solid #a35d00; padding: 10px 14px; background: #fff7e8; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 18px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 24px; font-size: 13px; }}
    th, td {{ border: 1px solid #ddd; padding: 7px; vertical-align: top; }}
    th {{ background: #f3f5f7; text-align: left; }}
    code {{ background: #f3f5f7; padding: 1px 4px; }}
    .status {{ font-weight: 700; }}
    svg {{ max-width: 100%; height: auto; border: 1px solid #d7dde2; background: white; }}
  </style>
</head>
<body>
  <h1>TauDEM Delineation QAQC Bundle</h1>
  <div class="summary">
    <p><strong>Status:</strong> {_escape(summary["status"])}; <strong>attention flags:</strong> {summary["attention_count"]}; <strong>human signoff required:</strong> yes.</p>
    <p>Production promotion is blocked until <code>{_escape(manifest["production_promotion"]["required_signoff"])}</code> records reviewer approval.</p>
  </div>

  <h2>Key Metrics</h2>
  {_html_table(["Metric", "Value"], [[_title(key), _format_value(value)] for key, value in metrics.items()])}

  <h2>Diagnostics</h2>
  {_html_table(["Check", "Status", "Severity", "Evidence", "Recommended Action"], check_rows)}

  <div class="grid">
    {''.join(map_sections)}
  </div>

  <h2>Reviewer Prompts</h2>
  {_html_table(["Topic", "Status", "Question", "Evidence"], [[p["topic"], p["status"], p["question"], p["evidence"]] for p in diagnostics["review_prompts"]])}

  <h2>Preserved TauDEM Artifact Inventory</h2>
  {artifact_table}

  <h2>Diagnostic Table Artifact</h2>
  {diagnostics_table}

  <h2>Subbasin Table Artifact</h2>
  {subbasins_table}
</body>
</html>
"""


def _svg_map(title: str, layers: list[tuple[str, list[Any], dict[str, Any]]]) -> str:
    geometries = [geom for _, geoms, _ in layers for geom in geoms if geom is not None and not geom.is_empty]
    if not geometries:
        return _empty_svg(title)
    bounds_geom = unary_union(geometries)
    minx, miny, maxx, maxy = bounds_geom.bounds
    if minx == maxx:
        minx -= 1.0
        maxx += 1.0
    if miny == maxy:
        miny -= 1.0
        maxy += 1.0
    pad_x = (maxx - minx) * 0.06
    pad_y = (maxy - miny) * 0.06
    bounds = (minx - pad_x, miny - pad_y, maxx + pad_x, maxy + pad_y)
    width = 760
    height = 520
    margin = 28

    def xy(x: float, y: float) -> tuple[float, float]:
        bx0, by0, bx1, by1 = bounds
        sx = margin + ((x - bx0) / max(bx1 - bx0, 1e-9)) * (width - 2 * margin)
        sy = height - margin - ((y - by0) / max(by1 - by0, 1e-9)) * (height - 2 * margin)
        return sx, sy

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{_escape(title)}">',
        f'<title>{_escape(title)}</title>',
        '<rect x="0" y="0" width="760" height="520" fill="#ffffff"/>',
    ]
    legend_y = 20
    for name, geoms, style in layers:
        if not geoms:
            continue
        layer_svg = []
        for geom in geoms:
            layer_svg.extend(_geometry_svg(geom, xy, style))
        parts.extend(layer_svg)
        legend_x = 22
        parts.append(
            f'<rect x="{legend_x}" y="{legend_y - 10}" width="12" height="8" '
            f'fill="{_escape(style.get("fill", "none"))}" stroke="{_escape(style.get("stroke", "#333"))}"/>'
        )
        parts.append(
            f'<text x="{legend_x + 18}" y="{legend_y}" font-size="12" fill="#333">{_escape(name)}</text>'
        )
        legend_y += 16
    parts.append("</svg>")
    return "\n".join(parts)


def _geometry_svg(geom: Any, xy, style: dict[str, Any]) -> list[str]:
    if geom is None or geom.is_empty:
        return []
    geom_type = geom.geom_type
    if geom_type == "Point":
        x, y = xy(geom.x, geom.y)
        r = style.get("radius", 3.5)
        return [
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r}" fill="{_escape(style.get("fill", "#333"))}" '
            f'stroke="{_escape(style.get("stroke", "#111"))}" stroke-width="1.2"/>'
        ]
    if geom_type == "LineString":
        points = " ".join(f"{xy(x, y)[0]:.2f},{xy(x, y)[1]:.2f}" for x, y in geom.coords)
        dash = f' stroke-dasharray="{_escape(style["dash"])}"' if "dash" in style else ""
        return [
            f'<polyline points="{points}" fill="none" stroke="{_escape(style.get("stroke", "#333"))}" '
            f'stroke-width="{float(style.get("stroke_width", 1.0)):.2f}"{dash}/>'
        ]
    if geom_type == "Polygon":
        path = _polygon_path(geom, xy)
        dash = f' stroke-dasharray="{_escape(style["dash"])}"' if "dash" in style else ""
        return [
            f'<path d="{path}" fill="{_escape(style.get("fill", "none"))}" '
            f'stroke="{_escape(style.get("stroke", "#333"))}" '
            f'stroke-width="{float(style.get("stroke_width", 1.0)):.2f}" fill-rule="evenodd"{dash}/>'
        ]
    if hasattr(geom, "geoms"):
        out = []
        for part in geom.geoms:
            out.extend(_geometry_svg(part, xy, style))
        return out
    return []


def _polygon_path(poly: Polygon, xy) -> str:
    rings = [poly.exterior, *list(poly.interiors)]
    path_parts = []
    for ring in rings:
        coords = list(ring.coords)
        if not coords:
            continue
        first = xy(coords[0][0], coords[0][1])
        path = [f"M {first[0]:.2f} {first[1]:.2f}"]
        for x, y in coords[1:]:
            sx, sy = xy(x, y)
            path.append(f"L {sx:.2f} {sy:.2f}")
        path.append("Z")
        path_parts.append(" ".join(path))
    return " ".join(path_parts)


def _empty_svg(title: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 180" role="img" aria-label="{_escape(title)}">
  <title>{_escape(title)}</title>
  <rect x="0" y="0" width="760" height="180" fill="#ffffff" stroke="#d7dde2"/>
  <text x="28" y="92" font-size="16" fill="#666">No geometry available for this figure.</text>
</svg>"""


def _artifact_rows(watershed: Any, bundle_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for key, path in sorted(_artifact_paths(getattr(watershed, "artifacts", {}) or {}).items()):
        exists = path.exists()
        rows.append(
            {
                "key": key,
                "role": ARTIFACT_ROLES.get(key, "workflow artifact"),
                "path": str(path),
                "relative_to_bundle": _relpath(path, bundle_dir),
                "exists": exists,
                "size_bytes": path.stat().st_size if exists and path.is_file() else None,
            }
        )
    return rows


def _subbasin_rows(subbasins_gdf: Any) -> list[dict[str, Any]]:
    if _safe_len(subbasins_gdf) == 0:
        return []
    rows = []
    id_column = "wsno" if "wsno" in subbasins_gdf.columns else None
    for idx, row in subbasins_gdf.iterrows():
        geom = row.geometry
        subbasin_id = row[id_column] if id_column else idx
        rows.append(
            {
                "subbasin_id": subbasin_id,
                "area_km2": geom.area / 1e6 if geom is not None else None,
                "is_valid": bool(geom.is_valid) if geom is not None else False,
                "bounds": list(geom.bounds) if geom is not None else None,
            }
        )
    return rows


def _serialize_command_result(command: Any) -> dict[str, Any]:
    outputs = getattr(command, "outputs", {}) or {}
    return {
        "executable": getattr(command, "executable", None),
        "command": [str(value) for value in (getattr(command, "command", None) or [])],
        "outputs": {key: str(path) for key, path in outputs.items()},
        "returncode": getattr(command, "returncode", None),
        "stdout": getattr(command, "stdout", ""),
        "stderr": getattr(command, "stderr", ""),
    }


def _artifact_paths(raw: dict[str, Any]) -> dict[str, Path]:
    paths = {}
    for key, value in raw.items():
        if value is None:
            continue
        try:
            paths[str(key)] = Path(value)
        except TypeError:
            continue
    return paths


def _read_point(path: Path, target_crs: Any) -> Optional[Point]:
    try:
        gdf = gpd.read_file(path)
        if gdf.empty:
            return None
        if target_crs is not None:
            if gdf.crs is None:
                gdf = gdf.set_crs(target_crs)
            else:
                gdf = gdf.to_crs(target_crs)
        geom = gdf.geometry.iloc[0]
        if isinstance(geom, Point):
            return geom
        return geom.centroid
    except Exception:
        return None


def _stream_orders_from_vector(streams_gdf: Any) -> list[int]:
    if _safe_len(streams_gdf) == 0:
        return []
    candidates = [
        "stream_order",
        "StreamOrder",
        "strmOrder",
        "StrmOrder",
        "Order",
        "order",
        "ORD",
        "ord",
    ]
    columns = {str(col).lower(): col for col in streams_gdf.columns}
    for candidate in candidates:
        col = columns.get(candidate.lower())
        if col is None:
            continue
        values = []
        for value in streams_gdf[col].dropna().tolist():
            try:
                values.append(int(value))
            except (TypeError, ValueError):
                continue
        if values:
            return values
    return []


def _stream_orders_from_raster(path: Optional[Path]) -> list[int]:
    if path is None or not Path(path).exists():
        return []
    try:
        with rasterio.open(path) as src:
            data = src.read(1, masked=True)
            values = np.asarray(data.compressed(), dtype=float)
        values = values[np.isfinite(values) & (values > 0)]
        return [int(value) for value in values.tolist()]
    except Exception:
        return []


def _union_geometry(gdf: Any) -> Optional[Any]:
    if _safe_len(gdf) == 0:
        return None
    geoms = [geom for geom in gdf.geometry if geom is not None and not geom.is_empty]
    if not geoms:
        return None
    return unary_union(geoms)


def _geometries(gdf: Any) -> Iterable[Any]:
    if _safe_len(gdf) == 0:
        return []
    return [geom for geom in gdf.geometry if geom is not None and not geom.is_empty]


def _geometry_area_km2(gdf: Any) -> Optional[float]:
    if _safe_len(gdf) == 0:
        return None
    return float(np.asarray(gdf.geometry.area, dtype=float).sum() / 1e6)


def _bounds_tuple(bounds: Any) -> Optional[tuple[float, float, float, float]]:
    if bounds is None:
        return None
    if hasattr(bounds, "left"):
        return (float(bounds.left), float(bounds.bottom), float(bounds.right), float(bounds.top))
    try:
        values = tuple(float(value) for value in bounds)
    except TypeError:
        return None
    if len(values) != 4:
        return None
    return values


def _safe_len(value: Any) -> int:
    try:
        return len(value)
    except Exception:
        return 0


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _metric_summary(metrics: dict[str, Any]) -> str:
    parts = []
    for key, value in metrics.items():
        if value is None:
            continue
        parts.append(f"{key}={_format_value(value)}")
    return "; ".join(parts) if parts else "No numeric evidence available"


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, (list, tuple)):
        return ", ".join(_format_value(item) for item in value)
    return str(value)


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, default=_json_default)
    return value


def _read_csv_for_html(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return "<p>No rows.</p>"
    return _html_table(rows[0], rows[1:])


def _html_table(headers: list[str], rows: list[list[Any]]) -> str:
    header_html = "".join(f"<th>{_escape(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{_escape(_format_value(value))}</td>" for value in row)
        body_rows.append(f"<tr>{cells}</tr>")
    if not body_rows:
        body_rows.append(
            f"<tr><td colspan=\"{len(headers)}\">No rows.</td></tr>"
        )
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _title(value: str) -> str:
    return value.replace("_", " ").title()


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _relpath(path: Path, base: Path) -> str:
    try:
        return os.path.relpath(Path(path), Path(base))
    except ValueError:
        return str(path)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return str(value)
