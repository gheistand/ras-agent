"""
pilot_channel.py - Conservative LiDAR pilot-channel terrain-mod proposals.

This module builds human-review artifacts for low-detail pilot-channel terrain
modifications from TauDEM stream centerlines and terrain diagnostics. It never
edits the source terrain or a HEC-RAS project. The output is a proposal package
that can be reviewed before any ras-commander terrain-mod application path is
used.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import geopandas as gpd
import numpy as np
import rasterio
from shapely.geometry import LineString
from shapely.ops import linemerge

logger = logging.getLogger(__name__)

HEC_COMMANDER_METHOD_NOTE_URL = (
    "https://github.com/gpt-cmdr/HEC-Commander/blob/main/"
    "Blog/5._Terrain_Mod_Your_LIDAR_defined_Channels.md"
)
ROADMAP_SECTION = (
    "docs/KNOWLEDGE.md"
    "#lidar-defined-channel-terrain-mod-proposal-workflow-2026-05-01"
)
PROPOSAL_SCHEMA_VERSION = "pilot-channel-terrain-mod-proposal/v1"

STREAM_ORDER_FIELDS = (
    "stream_order",
    "strmOrder",
    "StrmOrder",
    "streamorde",
    "StreamOrde",
    "order",
    "Order",
    "ORD",
    "ord",
)
SLOPE_FIELDS = (
    "main_channel_slope_m_per_m",
    "slope_m_per_m",
    "Slope",
    "slope",
    "strmSlope",
    "strm_slope",
)
AREA_KM2_FIELDS = (
    "drainage_area_km2",
    "drainage_km2",
    "area_km2",
    "cont_area_km2",
    "ds_area_km2",
    "us_area_km2",
)
AREA_MI2_FIELDS = (
    "drainage_area_mi2",
    "area_mi2",
    "cont_area_mi2",
)


@dataclass
class PilotChannelConfig:
    """Conservative defaults for first-pass pilot-channel proposal packages."""

    mode: str = "first_pass"
    min_stream_order: int = 1
    min_drainage_area_km2: float = 0.0
    sample_spacing_m: float = 30.0
    min_segment_length_m: float = 15.0
    min_positive_slope_m_per_m: float = 1e-5
    flat_slope_threshold_m_per_m: float = 5e-5
    abrupt_drop_threshold_m: float = 0.45
    excessive_cut_threshold_m: float = 0.75
    uncertain_evidence_min_stream_order: int = 2
    uncertain_evidence_min_drainage_area_km2: float = 1.0
    base_bottom_width_m: float = 0.3048
    width_per_stream_order_m: float = 0.0762
    width_per_sqrt_area_m: float = 0.015
    max_bottom_width_m: float = 1.5
    base_cut_depth_m: float = 0.06
    cut_depth_per_stream_order_m: float = 0.025
    cut_depth_per_log_area_m: float = 0.025
    max_target_cut_depth_m: float = 0.35
    max_profile_figures: int = 12
    write_figures: bool = True
    require_human_review: bool = True
    hash_terrain: bool = False

    def validate(self) -> None:
        if self.mode != "first_pass":
            raise ValueError("Only mode='first_pass' is implemented.")
        if self.sample_spacing_m <= 0:
            raise ValueError("sample_spacing_m must be positive.")
        if self.min_positive_slope_m_per_m <= 0:
            raise ValueError("min_positive_slope_m_per_m must be positive.")
        if self.base_bottom_width_m <= 0:
            raise ValueError("base_bottom_width_m must be positive.")


@dataclass
class PilotChannelProposalResult:
    """Paths and high-level status for a generated proposal package."""

    output_dir: Path
    proposal_json: Path
    profile_csv: Path
    segment_summary_csv: Path
    reviewer_flags_csv: Path
    centerlines_artifact: Path
    report_html: Path
    figure_paths: dict[str, Path] = field(default_factory=dict)
    proposed_segment_count: int = 0
    analyzed_segment_count: int = 0
    production_terrain_mutated: bool = False
    hitl_required: bool = True

    @property
    def artifacts(self) -> dict[str, Path]:
        artifacts = {
            "proposal_json": self.proposal_json,
            "profile_csv": self.profile_csv,
            "segment_summary_csv": self.segment_summary_csv,
            "reviewer_flags_csv": self.reviewer_flags_csv,
            "centerlines": self.centerlines_artifact,
            "report_html": self.report_html,
        }
        artifacts.update(self.figure_paths)
        return artifacts


def build_pilot_channel_proposals(
    watershed: Any,
    output_dir: Path,
    *,
    dem_path: Optional[Path] = None,
    ad8_path: Optional[Path] = None,
    config: Optional[PilotChannelConfig] = None,
) -> PilotChannelProposalResult:
    """
    Generate a conservative pilot-channel terrain-mod proposal package.

    The package samples centerline elevations, enforces an explicitly positive
    proposed longitudinal profile, writes review flags, and records a
    ras-commander handoff payload. It does not modify terrain, RASMapper layers,
    HEC-RAS geometry, or production model files.
    """
    config = config or PilotChannelConfig()
    config.validate()

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    figure_dir = output_dir / "figures"
    if config.write_figures:
        figure_dir.mkdir(parents=True, exist_ok=True)

    dem_path = _resolve_dem_path(watershed, dem_path)
    ad8_path = _resolve_ad8_path(watershed, ad8_path)
    terrain_before = _terrain_identity(dem_path, hash_file=config.hash_terrain)

    with rasterio.open(dem_path) as dem_src:
        dem_crs = dem_src.crs
        if dem_crs is None:
            raise RuntimeError(f"DEM has no CRS: {dem_path}")

        centerlines = _centerlines_from_watershed(watershed, dem_crs)
        basin = _optional_gdf(getattr(watershed, "basin", None), dem_crs)
        streams = _optional_gdf(getattr(watershed, "streams", None), dem_crs)

        ad8_src = rasterio.open(ad8_path) if ad8_path and Path(ad8_path).exists() else None
        try:
            summary_rows, profile_rows, flag_rows, proposal_gdf = _analyze_centerlines(
                centerlines=centerlines,
                dem_src=dem_src,
                ad8_src=ad8_src,
                config=config,
            )
        finally:
            if ad8_src is not None:
                ad8_src.close()

    terrain_after = _terrain_identity(dem_path, hash_file=config.hash_terrain)
    production_terrain_mutated = terrain_before != terrain_after
    if production_terrain_mutated:
        raise RuntimeError(
            "Source terrain changed while generating pilot-channel proposals. "
            f"DEM: {dem_path}"
        )

    profile_csv = output_dir / "pilot_channel_profiles.csv"
    segment_summary_csv = output_dir / "pilot_channel_segment_summary.csv"
    reviewer_flags_csv = output_dir / "pilot_channel_reviewer_flags.csv"
    proposal_json = output_dir / "pilot_channel_proposals.json"
    report_html = output_dir / "pilot_channel_report.html"

    _write_csv(profile_csv, profile_rows, _profile_fields())
    _write_csv(segment_summary_csv, summary_rows, _summary_fields())
    _write_csv(reviewer_flags_csv, flag_rows, _flag_fields())

    centerlines_artifact = _write_vector_artifact(
        proposal_gdf,
        output_dir / "pilot_channel_centerlines.gpkg",
    )

    figure_paths: dict[str, Path] = {}
    if config.write_figures:
        plan_path = figure_dir / "plan_overview.png"
        _plot_plan_figure(basin, streams, proposal_gdf, plan_path)
        figure_paths["plan_overview"] = plan_path

        cut_path = figure_dir / "cut_summary.png"
        _plot_cut_summary(summary_rows, cut_path)
        figure_paths["cut_summary"] = cut_path

        for segment_id, path in _plot_profile_figures(
            profile_rows,
            summary_rows,
            figure_dir,
            max_figures=config.max_profile_figures,
        ).items():
            figure_paths[f"profile_{segment_id}"] = path

    proposed_count = sum(1 for row in summary_rows if row["proposal_recommended"])
    payload = _proposal_payload(
        watershed=watershed,
        dem_path=dem_path,
        ad8_path=ad8_path,
        config=config,
        summary_rows=summary_rows,
        flag_rows=flag_rows,
        artifact_paths={
            "profiles_csv": profile_csv,
            "segment_summary_csv": segment_summary_csv,
            "reviewer_flags_csv": reviewer_flags_csv,
            "centerlines": centerlines_artifact,
            "report_html": report_html,
            **figure_paths,
        },
        terrain_identity_before=terrain_before,
        terrain_identity_after=terrain_after,
    )
    proposal_json.write_text(json.dumps(_jsonable(payload), indent=2), encoding="utf-8")

    _write_html_report(
        report_html,
        payload=payload,
        summary_rows=summary_rows,
        flag_rows=flag_rows,
        figure_paths=figure_paths,
        output_dir=output_dir,
    )

    logger.info(
        "Pilot-channel proposal package written to %s (%d proposed, %d analyzed)",
        output_dir,
        proposed_count,
        len(summary_rows),
    )

    return PilotChannelProposalResult(
        output_dir=output_dir,
        proposal_json=proposal_json,
        profile_csv=profile_csv,
        segment_summary_csv=segment_summary_csv,
        reviewer_flags_csv=reviewer_flags_csv,
        centerlines_artifact=centerlines_artifact,
        report_html=report_html,
        figure_paths=figure_paths,
        proposed_segment_count=proposed_count,
        analyzed_segment_count=len(summary_rows),
        production_terrain_mutated=False,
        hitl_required=config.require_human_review,
    )


def build_pilot_channel_proposals_from_files(
    *,
    dem_path: Path,
    centerlines_path: Path,
    output_dir: Path,
    streams_path: Optional[Path] = None,
    basin_path: Optional[Path] = None,
    ad8_path: Optional[Path] = None,
    config: Optional[PilotChannelConfig] = None,
) -> PilotChannelProposalResult:
    """File-based wrapper for CLI and prepared TauDEM artifact folders."""
    centerlines = gpd.read_file(centerlines_path)
    streams = gpd.read_file(streams_path) if streams_path else centerlines.copy()
    basin = gpd.read_file(basin_path) if basin_path else None
    watershed = SimpleNamespace(
        centerlines=centerlines,
        streams=streams,
        basin=basin,
        artifacts={"ad8": ad8_path} if ad8_path else {},
        dem_clipped=dem_path,
    )
    return build_pilot_channel_proposals(
        watershed,
        output_dir,
        dem_path=dem_path,
        ad8_path=ad8_path,
        config=config,
    )


def _analyze_centerlines(
    *,
    centerlines: gpd.GeoDataFrame,
    dem_src: rasterio.io.DatasetReader,
    ad8_src: Optional[rasterio.io.DatasetReader],
    config: PilotChannelConfig,
) -> tuple[list[dict], list[dict], list[dict], gpd.GeoDataFrame]:
    summary_rows: list[dict] = []
    profile_rows: list[dict] = []
    flag_rows: list[dict] = []
    proposal_records: list[dict] = []
    proposal_geoms: list[LineString] = []

    for ordinal, (_, row) in enumerate(centerlines.iterrows(), start=1):
        for part_index, line in enumerate(_line_parts(row.geometry), start=1):
            if line.length <= 0:
                continue

            segment_id = _segment_id(row, ordinal, part_index)
            stream_order = _stream_order(row)
            native_slope = _native_slope(row)
            profile = _sample_profile(line, dem_src, config.sample_spacing_m)
            drainage_area_km2, drainage_area_source = _drainage_area_km2(
                row,
                line,
                ad8_src,
            )

            summary, rows, flags = _build_segment_proposal(
                segment_id=segment_id,
                line=line,
                profile=profile,
                stream_order=stream_order,
                native_slope=native_slope,
                drainage_area_km2=drainage_area_km2,
                drainage_area_source=drainage_area_source,
                config=config,
            )
            summary_rows.append(summary)
            profile_rows.extend(rows)
            flag_rows.extend(flags)

            record = {
                key: summary[key]
                for key in (
                    "segment_id",
                    "stream_order",
                    "drainage_area_km2",
                    "existing_overall_slope_m_per_m",
                    "min_proposed_slope_m_per_m",
                    "max_cut_m",
                    "estimated_cut_volume_m3",
                    "proposal_status",
                    "review_flags",
                )
            }
            proposal_records.append(record)
            proposal_geoms.append(line)

    if not proposal_records:
        return (
            summary_rows,
            profile_rows,
            flag_rows,
            gpd.GeoDataFrame({"segment_id": []}, geometry=[], crs=centerlines.crs),
        )

    proposal_gdf = gpd.GeoDataFrame(
        proposal_records,
        geometry=proposal_geoms,
        crs=centerlines.crs,
    )
    return summary_rows, profile_rows, flag_rows, proposal_gdf


def _build_segment_proposal(
    *,
    segment_id: str,
    line: LineString,
    profile: dict[str, np.ndarray],
    stream_order: Optional[int],
    native_slope: Optional[float],
    drainage_area_km2: Optional[float],
    drainage_area_source: str,
    config: PilotChannelConfig,
) -> tuple[dict, list[dict], list[dict]]:
    station = profile["station_m"]
    x = profile["x"]
    y = profile["y"]
    existing = profile["elevation_m"]

    flags: list[tuple[str, str, str]] = []
    profile_rows: list[dict] = []

    if line.length < config.min_segment_length_m:
        flags.append((
            "uncertain_channel_evidence",
            "warning",
            f"Segment length {line.length:.1f} m is below the configured minimum.",
        ))

    if len(station) < 2:
        flags.append((
            "insufficient_profile_samples",
            "error",
            "Fewer than two valid DEM samples were available on this centerline.",
        ))
        summary = _empty_summary(
            segment_id,
            line.length,
            stream_order,
            native_slope,
            drainage_area_km2,
            drainage_area_source,
            flags,
        )
        return summary, profile_rows, _flag_rows(segment_id, flags)

    oriented = _orient_profile_downstream(station, x, y, existing)
    station = oriented["station_m"]
    x = oriented["x"]
    y = oriented["y"]
    existing = oriented["elevation_m"]

    dx = np.diff(station)
    existing_drop = existing[:-1] - existing[1:]
    existing_slopes = np.divide(
        existing_drop,
        dx,
        out=np.zeros_like(existing_drop, dtype=float),
        where=dx > 0,
    )

    length_m = float(station[-1] - station[0])
    existing_overall_slope = (
        float((existing[0] - existing[-1]) / length_m) if length_m > 0 else 0.0
    )
    min_existing_slope = float(np.min(existing_slopes)) if existing_slopes.size else 0.0
    max_existing_drop = float(np.max(existing_drop)) if existing_drop.size else 0.0
    nonpositive_existing = int(np.sum(existing_slopes <= 0.0))

    if existing_overall_slope <= config.flat_slope_threshold_m_per_m:
        flags.append((
            "flat_slope",
            "warning",
            (
                "Existing overall profile slope "
                f"{existing_overall_slope:.6f} m/m is at or below "
                f"{config.flat_slope_threshold_m_per_m:.6f} m/m."
            ),
        ))
    if nonpositive_existing > 0:
        flags.append((
            "profile_noise_nonpositive_slopes",
            "warning",
            f"{nonpositive_existing} profile interval(s) are flat or reverse-sloped.",
        ))
    if max_existing_drop >= config.abrupt_drop_threshold_m:
        flags.append((
            "abrupt_drop",
            "warning",
            (
                f"Maximum sampled terrain drop is {max_existing_drop:.2f} m "
                f"over one interval."
            ),
        ))

    evidence_uncertain = _evidence_uncertain(
        stream_order=stream_order,
        drainage_area_km2=drainage_area_km2,
        drainage_area_source=drainage_area_source,
        config=config,
    )
    if evidence_uncertain:
        flags.append((
            "uncertain_channel_evidence",
            "warning",
            "TauDEM stream order/drainage-area evidence is missing or below review thresholds.",
        ))

    bottom_width_m = _target_bottom_width(stream_order, drainage_area_km2, config)
    target_cut_m = _target_cut_depth(stream_order, drainage_area_km2, config)
    proposed = _positive_slope_profile(
        station=station,
        existing=existing,
        target_cut_m=target_cut_m,
        min_slope=config.min_positive_slope_m_per_m,
    )
    cut = existing - proposed

    proposed_slopes = np.divide(
        proposed[:-1] - proposed[1:],
        dx,
        out=np.zeros(max(len(proposed) - 1, 0), dtype=float),
        where=dx > 0,
    )
    min_proposed_slope = float(np.min(proposed_slopes)) if proposed_slopes.size else 0.0
    positive_slope_check_passed = bool(proposed_slopes.size and np.all(proposed_slopes > 0))
    if not positive_slope_check_passed:
        flags.append((
            "positive_profile_slope_failed",
            "error",
            "Proposed invert profile does not maintain positive downstream slope.",
        ))

    max_cut_m = float(np.max(cut)) if cut.size else 0.0
    mean_cut_m = float(np.mean(cut)) if cut.size else 0.0
    if max_cut_m > config.excessive_cut_threshold_m:
        flags.append((
            "excessive_cut",
            "warning",
            (
                f"Maximum proposed cut {max_cut_m:.2f} m exceeds "
                f"{config.excessive_cut_threshold_m:.2f} m review threshold."
            ),
        ))

    estimated_cut_volume_m3 = _estimate_cut_volume(station, cut, bottom_width_m)
    order_ok = (stream_order or 0) >= config.min_stream_order
    area_ok = (
        drainage_area_km2 is None
        or drainage_area_km2 >= config.min_drainage_area_km2
    )
    slope_issue = (
        existing_overall_slope <= config.flat_slope_threshold_m_per_m
        or nonpositive_existing > 0
        or max_existing_drop >= config.abrupt_drop_threshold_m
    )
    proposal_recommended = bool(order_ok and area_ok and slope_issue)
    if proposal_recommended:
        proposal_status = "proposed_requires_human_review"
    elif not order_ok or not area_ok:
        proposal_status = "not_proposed_below_candidate_threshold"
    else:
        proposal_status = "not_proposed_no_low_flow_profile_issue"

    flag_codes = sorted({code for code, _, _ in flags})
    for idx, (station_m, px, py, elev, invert, cut_m) in enumerate(
        zip(station, x, y, existing, proposed, cut)
    ):
        slope_to_next = (
            float(proposed_slopes[idx]) if idx < len(proposed_slopes) else None
        )
        profile_rows.append({
            "segment_id": segment_id,
            "sample_index": idx,
            "station_m": float(station_m),
            "x": float(px),
            "y": float(py),
            "existing_elevation_m": float(elev),
            "proposed_invert_m": float(invert),
            "cut_m": float(cut_m),
            "proposed_slope_to_next_m_per_m": slope_to_next,
            "positive_slope_check_passed": positive_slope_check_passed,
            "proposal_status": proposal_status,
        })

    summary = {
        "segment_id": segment_id,
        "length_m": float(length_m),
        "stream_order": stream_order,
        "drainage_area_km2": drainage_area_km2,
        "drainage_area_source": drainage_area_source,
        "native_slope_m_per_m": native_slope,
        "existing_overall_slope_m_per_m": existing_overall_slope,
        "min_existing_slope_m_per_m": min_existing_slope,
        "max_existing_drop_m": max_existing_drop,
        "nonpositive_existing_slope_intervals": nonpositive_existing,
        "target_cut_depth_m": target_cut_m,
        "bottom_width_m": bottom_width_m,
        "max_cut_m": max_cut_m,
        "mean_cut_m": mean_cut_m,
        "estimated_cut_volume_m3": estimated_cut_volume_m3,
        "estimated_fill_volume_m3": 0.0,
        "min_proposed_slope_m_per_m": min_proposed_slope,
        "positive_profile_slope_check_passed": positive_slope_check_passed,
        "proposal_recommended": proposal_recommended,
        "proposal_status": proposal_status,
        "review_flags": ";".join(flag_codes),
    }
    return summary, profile_rows, _flag_rows(segment_id, flags)


def _positive_slope_profile(
    *,
    station: np.ndarray,
    existing: np.ndarray,
    target_cut_m: float,
    min_slope: float,
) -> np.ndarray:
    proposed = existing.astype(float) - float(target_cut_m)
    for idx in range(1, len(proposed)):
        dx = float(station[idx] - station[idx - 1])
        if dx <= 0:
            continue
        max_allowed = proposed[idx - 1] - min_slope * dx
        if proposed[idx] >= max_allowed:
            proposed[idx] = max_allowed
    return proposed


def _sample_profile(
    line: LineString,
    dem_src: rasterio.io.DatasetReader,
    sample_spacing_m: float,
) -> dict[str, np.ndarray]:
    length = float(line.length)
    sample_count = max(int(math.ceil(length / sample_spacing_m)) + 1, 2)
    distances = np.linspace(0.0, length, sample_count)
    points = [line.interpolate(float(distance)) for distance in distances]
    coords = [(point.x, point.y) for point in points]

    values = []
    for sample in dem_src.sample(coords):
        value = float(sample[0])
        nodata = dem_src.nodata
        is_valid = np.isfinite(value) and (nodata is None or value != nodata)
        values.append(value if is_valid else np.nan)

    values_arr = np.asarray(values, dtype=float)
    valid = np.isfinite(values_arr)
    return {
        "station_m": distances[valid],
        "x": np.asarray([coord[0] for coord in coords], dtype=float)[valid],
        "y": np.asarray([coord[1] for coord in coords], dtype=float)[valid],
        "elevation_m": values_arr[valid],
    }


def _orient_profile_downstream(
    station: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    elevation: np.ndarray,
) -> dict[str, np.ndarray]:
    if len(elevation) >= 2 and elevation[0] < elevation[-1]:
        length = float(station[-1])
        return {
            "station_m": length - station[::-1],
            "x": x[::-1],
            "y": y[::-1],
            "elevation_m": elevation[::-1],
        }
    return {
        "station_m": station,
        "x": x,
        "y": y,
        "elevation_m": elevation,
    }


def _drainage_area_km2(
    row: Any,
    line: LineString,
    ad8_src: Optional[rasterio.io.DatasetReader],
) -> tuple[Optional[float], str]:
    if ad8_src is not None:
        area = _area_from_ad8(line, ad8_src)
        if area is not None:
            return area, "ad8_endpoint_max"

    explicit_km2 = _first_number(row, AREA_KM2_FIELDS)
    if explicit_km2 is not None:
        return explicit_km2, "attribute_km2"

    explicit_mi2 = _first_number(row, AREA_MI2_FIELDS)
    if explicit_mi2 is not None:
        return explicit_mi2 * 2.58999, "attribute_mi2"

    return None, "missing"


def _area_from_ad8(
    line: LineString,
    ad8_src: rasterio.io.DatasetReader,
) -> Optional[float]:
    endpoints = [line.interpolate(0.0), line.interpolate(line.length)]
    coords = [(point.x, point.y) for point in endpoints]
    values = []
    for sample in ad8_src.sample(coords):
        value = float(sample[0])
        nodata = ad8_src.nodata
        if np.isfinite(value) and (nodata is None or value != nodata) and value > 0:
            values.append(value)
    if not values:
        return None
    cell_area_km2 = abs(ad8_src.transform.a * ad8_src.transform.e) / 1_000_000.0
    return float(max(values) * cell_area_km2)


def _target_bottom_width(
    stream_order: Optional[int],
    drainage_area_km2: Optional[float],
    config: PilotChannelConfig,
) -> float:
    order = max(stream_order or 1, 1)
    area = max(drainage_area_km2 or 0.0, 0.0)
    width = (
        config.base_bottom_width_m
        + config.width_per_stream_order_m * max(order - 1, 0)
        + config.width_per_sqrt_area_m * math.sqrt(area)
    )
    return float(min(width, config.max_bottom_width_m))


def _target_cut_depth(
    stream_order: Optional[int],
    drainage_area_km2: Optional[float],
    config: PilotChannelConfig,
) -> float:
    order = max(stream_order or 1, 1)
    area = max(drainage_area_km2 or 0.0, 0.0)
    cut = (
        config.base_cut_depth_m
        + config.cut_depth_per_stream_order_m * max(order - 1, 0)
        + config.cut_depth_per_log_area_m * math.log1p(area)
    )
    return float(min(cut, config.max_target_cut_depth_m))


def _estimate_cut_volume(
    station: np.ndarray,
    cut: np.ndarray,
    bottom_width_m: float,
) -> float:
    if len(station) < 2 or len(cut) < 2:
        return 0.0
    dx = np.diff(station)
    avg_cut = (cut[:-1] + cut[1:]) / 2.0
    volume = np.sum(np.maximum(avg_cut, 0.0) * dx * bottom_width_m)
    return float(volume)


def _evidence_uncertain(
    *,
    stream_order: Optional[int],
    drainage_area_km2: Optional[float],
    drainage_area_source: str,
    config: PilotChannelConfig,
) -> bool:
    if stream_order is None:
        return True
    if drainage_area_km2 is None or drainage_area_source == "missing":
        return True
    return (
        stream_order < config.uncertain_evidence_min_stream_order
        and drainage_area_km2 < config.uncertain_evidence_min_drainage_area_km2
    )


def _empty_summary(
    segment_id: str,
    length_m: float,
    stream_order: Optional[int],
    native_slope: Optional[float],
    drainage_area_km2: Optional[float],
    drainage_area_source: str,
    flags: list[tuple[str, str, str]],
) -> dict:
    return {
        "segment_id": segment_id,
        "length_m": float(length_m),
        "stream_order": stream_order,
        "drainage_area_km2": drainage_area_km2,
        "drainage_area_source": drainage_area_source,
        "native_slope_m_per_m": native_slope,
        "existing_overall_slope_m_per_m": None,
        "min_existing_slope_m_per_m": None,
        "max_existing_drop_m": None,
        "nonpositive_existing_slope_intervals": None,
        "target_cut_depth_m": None,
        "bottom_width_m": None,
        "max_cut_m": None,
        "mean_cut_m": None,
        "estimated_cut_volume_m3": 0.0,
        "estimated_fill_volume_m3": 0.0,
        "min_proposed_slope_m_per_m": None,
        "positive_profile_slope_check_passed": False,
        "proposal_recommended": False,
        "proposal_status": "not_proposed_insufficient_profile",
        "review_flags": ";".join(sorted({code for code, _, _ in flags})),
    }


def _flag_rows(segment_id: str, flags: list[tuple[str, str, str]]) -> list[dict]:
    return [
        {
            "segment_id": segment_id,
            "flag_code": code,
            "severity": severity,
            "message": message,
        }
        for code, severity, message in flags
    ]


def _centerlines_from_watershed(watershed: Any, crs: Any) -> gpd.GeoDataFrame:
    source = getattr(watershed, "centerlines", None)
    if source is None or len(source) == 0:
        source = getattr(watershed, "streams", None)
    if source is None or len(source) == 0:
        raise ValueError("Watershed has no TauDEM centerlines or streams to analyze.")

    gdf = source.copy()
    if gdf.crs is None:
        gdf = gdf.set_crs(crs)
    return gdf.to_crs(crs)


def _optional_gdf(source: Any, crs: Any) -> Optional[gpd.GeoDataFrame]:
    if source is None:
        return None
    if len(source) == 0:
        return None
    gdf = source.copy()
    if gdf.crs is None:
        gdf = gdf.set_crs(crs)
    return gdf.to_crs(crs)


def _line_parts(geometry: Any) -> list[LineString]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if geometry.geom_type == "MultiLineString":
        return [part for part in geometry.geoms if isinstance(part, LineString)]
    try:
        merged = linemerge(geometry)
    except Exception:
        return []
    if isinstance(merged, LineString):
        return [merged]
    return [part for part in getattr(merged, "geoms", []) if isinstance(part, LineString)]


def _segment_id(row: Any, ordinal: int, part_index: int) -> str:
    for field_name in ("segment_id", "centerline_id", "stream_id", "LINKNO", "linkno"):
        value = _row_value(row, field_name)
        if value is not None and str(value) != "":
            base = str(value)
            return base if part_index == 1 else f"{base}_{part_index}"
    return f"segment_{ordinal}" if part_index == 1 else f"segment_{ordinal}_{part_index}"


def _stream_order(row: Any) -> Optional[int]:
    value = _first_number(row, STREAM_ORDER_FIELDS)
    if value is None:
        return None
    return int(round(value))


def _native_slope(row: Any) -> Optional[float]:
    return _first_number(row, SLOPE_FIELDS)


def _first_number(row: Any, field_names: tuple[str, ...]) -> Optional[float]:
    for field_name in field_names:
        value = _row_value(row, field_name)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(number):
            return number
    return None


def _row_value(row: Any, field_name: str) -> Any:
    if hasattr(row, "index") and field_name in row.index:
        return row[field_name]
    lower_lookup = {
        str(name).lower(): name
        for name in getattr(row, "index", [])
    }
    actual = lower_lookup.get(field_name.lower())
    if actual is None:
        return None
    return row[actual]


def _resolve_dem_path(watershed: Any, explicit: Optional[Path]) -> Path:
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    dem_clipped = getattr(watershed, "dem_clipped", None)
    if dem_clipped is not None:
        candidates.append(dem_clipped)
    artifacts = getattr(watershed, "artifacts", {}) or {}
    for key in ("dem_clipped", "fel", "dem"):
        if artifacts.get(key) is not None:
            candidates.append(artifacts[key])

    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    raise FileNotFoundError("No readable DEM path found for pilot-channel proposals.")


def _resolve_ad8_path(watershed: Any, explicit: Optional[Path]) -> Optional[Path]:
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    artifacts = getattr(watershed, "artifacts", {}) or {}
    if artifacts.get("ad8") is not None:
        candidates.append(artifacts["ad8"])
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def _terrain_identity(path: Path, *, hash_file: bool) -> dict[str, Any]:
    stat = Path(path).stat()
    identity = {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if hash_file:
        identity["sha256"] = _sha256(path)
    return identity


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_jsonable(row))


def _write_vector_artifact(gdf: gpd.GeoDataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        gdf.to_file(path, driver="GPKG")
        return path
    except Exception as exc:
        logger.warning("Could not write %s as GPKG (%s); writing WKT CSV fallback.", path, exc)
        fallback = path.with_suffix(".wkt.csv")
        rows = []
        for _, row in gdf.iterrows():
            rows.append({
                "segment_id": row.get("segment_id"),
                "proposal_status": row.get("proposal_status"),
                "review_flags": row.get("review_flags"),
                "wkt": row.geometry.wkt if row.geometry is not None else "",
            })
        _write_csv(fallback, rows, ["segment_id", "proposal_status", "review_flags", "wkt"])
        return fallback


def _proposal_payload(
    *,
    watershed: Any,
    dem_path: Path,
    ad8_path: Optional[Path],
    config: PilotChannelConfig,
    summary_rows: list[dict],
    flag_rows: list[dict],
    artifact_paths: dict[str, Path],
    terrain_identity_before: dict[str, Any],
    terrain_identity_after: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": config.mode,
        "status": "requires_human_review" if config.require_human_review else "proposal_only",
        "hitl_required": config.require_human_review,
        "production_terrain_mutated": False,
        "source_inputs": {
            "dem_path": dem_path,
            "ad8_path": ad8_path,
            "watershed_artifact_keys": sorted(
                (getattr(watershed, "artifacts", {}) or {}).keys()
            ),
        },
        "terrain_identity_before": terrain_identity_before,
        "terrain_identity_after": terrain_identity_after,
        "config": asdict(config),
        "summary": {
            "analyzed_segment_count": len(summary_rows),
            "proposed_segment_count": sum(
                1 for row in summary_rows if row["proposal_recommended"]
            ),
            "flagged_segment_count": len({
                row["segment_id"]
                for row in flag_rows
            }),
            "positive_profile_slope_checks": {
                "passed": sum(
                    1
                    for row in summary_rows
                    if row["positive_profile_slope_check_passed"]
                ),
                "failed": sum(
                    1
                    for row in summary_rows
                    if not row["positive_profile_slope_check_passed"]
                ),
            },
        },
        "references": {
            "hec_commander_lidar_terrain_mod_method_note": HEC_COMMANDER_METHOD_NOTE_URL,
            "ras_agent_roadmap_section": ROADMAP_SECTION,
        },
        "ras_commander_handoff": {
            "status": "not_applied_pending_upstream_validation_and_human_signoff",
            "centerline_artifact": artifact_paths.get("centerlines"),
            "profile_table": artifact_paths.get("profiles_csv"),
            "terrain_mod_type": "low_detail_pilot_channel",
            "requires_validation": [
                "human engineering review",
                "positive longitudinal slope review",
                "ras-commander terrain-mod application validation",
                "HEC-RAS/RASMapper terrain-mod review before production terrain export",
            ],
        },
        "artifacts": artifact_paths,
        "segments": summary_rows,
    }


def _plot_plan_figure(
    basin: Optional[gpd.GeoDataFrame],
    streams: Optional[gpd.GeoDataFrame],
    proposals: gpd.GeoDataFrame,
    path: Path,
) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9.2, 7.0))
    if basin is not None and not basin.empty:
        basin.boundary.plot(ax=ax, color="#1f3552", linewidth=1.4)
    if streams is not None and not streams.empty:
        streams.plot(ax=ax, color="#9fb3c8", linewidth=0.7, alpha=0.75)
    if not proposals.empty:
        proposed = proposals[proposals["proposal_status"] == "proposed_requires_human_review"]
        other = proposals[proposals["proposal_status"] != "proposed_requires_human_review"]
        if not other.empty:
            other.plot(ax=ax, color="#6c757d", linewidth=1.0, alpha=0.65)
        if not proposed.empty:
            proposed.plot(ax=ax, color="#c43b32", linewidth=2.0, alpha=0.95)
    ax.set_title("Pilot-Channel Proposal Plan View")
    ax.set_xlabel("Easting")
    ax.set_ylabel("Northing")
    ax.grid(True, linestyle="--", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _plot_cut_summary(summary_rows: list[dict], path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plotted = [
        row for row in summary_rows
        if row.get("proposal_recommended") and row.get("max_cut_m") is not None
    ]
    plotted = sorted(plotted, key=lambda row: row["max_cut_m"], reverse=True)[:20]

    fig, ax = plt.subplots(figsize=(9.2, 5.4))
    if plotted:
        labels = [str(row["segment_id"]) for row in plotted]
        values = [float(row["max_cut_m"]) for row in plotted]
        ax.bar(range(len(values)), values, color="#c43b32", alpha=0.85)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=45, ha="right")
    else:
        ax.text(0.5, 0.5, "No proposed candidate segments", ha="center", va="center")
        ax.set_xticks([])
    ax.set_ylabel("Maximum Proposed Cut (m)")
    ax.set_title("Pilot-Channel Cut Summary")
    ax.grid(True, axis="y", linestyle="--", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _plot_profile_figures(
    profile_rows: list[dict],
    summary_rows: list[dict],
    figure_dir: Path,
    *,
    max_figures: int,
) -> dict[str, Path]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    proposed_ids = [
        row["segment_id"]
        for row in summary_rows
        if row.get("proposal_recommended")
    ][:max_figures]
    rows_by_segment: dict[str, list[dict]] = {}
    for row in profile_rows:
        rows_by_segment.setdefault(str(row["segment_id"]), []).append(row)

    paths: dict[str, Path] = {}
    for segment_id in proposed_ids:
        rows = rows_by_segment.get(str(segment_id), [])
        if len(rows) < 2:
            continue
        station = [float(row["station_m"]) for row in rows]
        existing = [float(row["existing_elevation_m"]) for row in rows]
        proposed = [float(row["proposed_invert_m"]) for row in rows]

        fig, ax = plt.subplots(figsize=(9.2, 5.2))
        ax.plot(station, existing, color="#1f5f8b", linewidth=1.5, label="Existing terrain")
        ax.plot(station, proposed, color="#c43b32", linewidth=1.6, label="Proposed pilot-channel invert")
        ax.fill_between(station, existing, proposed, color="#c43b32", alpha=0.18, label="Proposed cut")
        ax.set_title(f"Before/After Profile - {segment_id}")
        ax.set_xlabel("Station (m)")
        ax.set_ylabel("Elevation (m)")
        ax.grid(True, linestyle="--", alpha=0.25)
        ax.legend(loc="best")
        fig.tight_layout()
        path = figure_dir / f"profile_{_safe_name(segment_id)}.png"
        fig.savefig(path, dpi=130)
        plt.close(fig)
        paths[str(segment_id)] = path
    return paths


def _write_html_report(
    path: Path,
    *,
    payload: dict[str, Any],
    summary_rows: list[dict],
    flag_rows: list[dict],
    figure_paths: dict[str, Path],
    output_dir: Path,
) -> None:
    proposed_count = payload["summary"]["proposed_segment_count"]
    analyzed_count = payload["summary"]["analyzed_segment_count"]
    flags_by_segment = {}
    for row in flag_rows:
        flags_by_segment.setdefault(row["segment_id"], []).append(row["flag_code"])

    summary_table = "".join(
        "<tr>"
        f"<td>{html.escape(str(row['segment_id']))}</td>"
        f"<td>{html.escape(str(row['proposal_status']))}</td>"
        f"<td>{_fmt(row.get('stream_order'))}</td>"
        f"<td>{_fmt(row.get('drainage_area_km2'), 3)}</td>"
        f"<td>{_fmt(row.get('existing_overall_slope_m_per_m'), 6)}</td>"
        f"<td>{_fmt(row.get('min_proposed_slope_m_per_m'), 6)}</td>"
        f"<td>{_fmt(row.get('max_cut_m'), 3)}</td>"
        f"<td>{html.escape(row.get('review_flags') or '')}</td>"
        "</tr>"
        for row in summary_rows
    )
    flag_table = "".join(
        "<tr>"
        f"<td>{html.escape(str(row['segment_id']))}</td>"
        f"<td>{html.escape(str(row['flag_code']))}</td>"
        f"<td>{html.escape(str(row['severity']))}</td>"
        f"<td>{html.escape(str(row['message']))}</td>"
        "</tr>"
        for row in flag_rows
    )

    def img_tag(key: str, title: str) -> str:
        img_path = figure_paths.get(key)
        if img_path is None:
            return ""
        rel = img_path.relative_to(output_dir).as_posix()
        return (
            f"<section><h3>{html.escape(title)}</h3>"
            f'<img src="{html.escape(rel)}" alt="{html.escape(title)}" /></section>'
        )

    profile_sections = ""
    for key, img_path in figure_paths.items():
        if not key.startswith("profile_"):
            continue
        rel = img_path.relative_to(output_dir).as_posix()
        profile_sections += (
            f"<section><h3>{html.escape(key.replace('_', ' '))}</h3>"
            f'<img src="{html.escape(rel)}" alt="{html.escape(key)}" /></section>'
        )

    html_text = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Pilot-Channel Terrain-Mod Proposal</title>
  <style>
    body {{ font-family: Arial, sans-serif; max-width: 1180px; margin: 0 auto; padding: 24px; color: #26333f; }}
    h1, h2, h3 {{ color: #173a5e; }}
    .notice {{ border: 1px solid #c43b32; background: #fff5f3; padding: 12px 14px; border-radius: 6px; }}
    .meta {{ color: #5d6b78; }}
    table {{ border-collapse: collapse; width: 100%; margin: 14px 0 24px; font-size: 0.92rem; }}
    th {{ background: #173a5e; color: white; text-align: left; padding: 7px 9px; }}
    td {{ border-bottom: 1px solid #d9e2ec; padding: 6px 9px; vertical-align: top; }}
    tr:nth-child(even) {{ background: #f7f9fb; }}
    img {{ max-width: 100%; border: 1px solid #d9e2ec; border-radius: 4px; }}
    code {{ background: #eef3f7; padding: 1px 4px; border-radius: 3px; }}
  </style>
</head>
<body>
  <h1>Pilot-Channel Terrain-Mod Proposal</h1>
  <p class="notice">
    This is a proposal package only. Human engineering signoff is required before
    production terrain, RASMapper terrain modifications, or HEC-RAS project files
    are changed. Source terrain mutation check: <strong>passed</strong>.
  </p>
  <p class="meta">
    Generated: {html.escape(str(payload["generated_at"]))}<br />
    Schema: <code>{html.escape(PROPOSAL_SCHEMA_VERSION)}</code><br />
    Proposed segments: {proposed_count} of {analyzed_count} analyzed<br />
    HEC-Commander method note:
    <a href="{html.escape(HEC_COMMANDER_METHOD_NOTE_URL)}">{html.escape(HEC_COMMANDER_METHOD_NOTE_URL)}</a><br />
    ras-agent roadmap section: <code>{html.escape(ROADMAP_SECTION)}</code>
  </p>
  {img_tag("plan_overview", "Plan Overview")}
  {img_tag("cut_summary", "Cut Summary")}
  <h2>Segment Slope Checks And Cut/Fill Summary</h2>
  <table>
    <thead>
      <tr>
        <th>Segment</th><th>Status</th><th>Order</th><th>Drainage area km2</th>
        <th>Existing slope</th><th>Min proposed slope</th><th>Max cut m</th><th>Flags</th>
      </tr>
    </thead>
    <tbody>{summary_table}</tbody>
  </table>
  <h2>Reviewer Flags</h2>
  <table>
    <thead><tr><th>Segment</th><th>Flag</th><th>Severity</th><th>Message</th></tr></thead>
    <tbody>{flag_table}</tbody>
  </table>
  <h2>Before/After Profiles</h2>
  {profile_sections or '<p class="meta">No profile figures were written.</p>'}
</body>
</html>
"""
    path.write_text(html_text, encoding="utf-8")


def _fmt(value: Any, decimals: int = 2) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return html.escape(str(value))


def _safe_name(value: Any) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _profile_fields() -> list[str]:
    return [
        "segment_id",
        "sample_index",
        "station_m",
        "x",
        "y",
        "existing_elevation_m",
        "proposed_invert_m",
        "cut_m",
        "proposed_slope_to_next_m_per_m",
        "positive_slope_check_passed",
        "proposal_status",
    ]


def _summary_fields() -> list[str]:
    return [
        "segment_id",
        "length_m",
        "stream_order",
        "drainage_area_km2",
        "drainage_area_source",
        "native_slope_m_per_m",
        "existing_overall_slope_m_per_m",
        "min_existing_slope_m_per_m",
        "max_existing_drop_m",
        "nonpositive_existing_slope_intervals",
        "target_cut_depth_m",
        "bottom_width_m",
        "max_cut_m",
        "mean_cut_m",
        "estimated_cut_volume_m3",
        "estimated_fill_volume_m3",
        "min_proposed_slope_m_per_m",
        "positive_profile_slope_check_passed",
        "proposal_recommended",
        "proposal_status",
        "review_flags",
    ]


def _flag_fields() -> list[str]:
    return ["segment_id", "flag_code", "severity", "message"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate conservative pilot-channel terrain-mod proposal artifacts."
    )
    parser.add_argument("--dem", required=True, type=Path, help="DEM GeoTIFF to sample")
    parser.add_argument("--centerlines", required=True, type=Path, help="TauDEM centerline/stream vector")
    parser.add_argument("--output", required=True, type=Path, help="Proposal package output directory")
    parser.add_argument("--streams", type=Path, help="Optional TauDEM stream network vector")
    parser.add_argument("--basin", type=Path, help="Optional basin polygon vector")
    parser.add_argument("--ad8", type=Path, help="Optional TauDEM D8 contributing-area raster")
    parser.add_argument("--sample-spacing-m", type=float, default=30.0)
    parser.add_argument("--min-stream-order", type=int, default=1)
    parser.add_argument("--min-drainage-area-km2", type=float, default=0.0)
    parser.add_argument("--no-figures", action="store_true")
    parser.add_argument("--hash-terrain", action="store_true")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    args = _build_parser().parse_args()
    config = PilotChannelConfig(
        sample_spacing_m=args.sample_spacing_m,
        min_stream_order=args.min_stream_order,
        min_drainage_area_km2=args.min_drainage_area_km2,
        write_figures=not args.no_figures,
        hash_terrain=args.hash_terrain,
    )
    result = build_pilot_channel_proposals_from_files(
        dem_path=args.dem,
        centerlines_path=args.centerlines,
        streams_path=args.streams,
        basin_path=args.basin,
        ad8_path=args.ad8,
        output_dir=args.output,
        config=config,
    )
    print(json.dumps({key: str(path) for key, path in result.artifacts.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
