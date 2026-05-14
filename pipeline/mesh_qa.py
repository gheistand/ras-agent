"""
mesh_qa.py - Geometry-first mesh QA packaging.

This module summarizes the proposed plain-text .g## mesh inputs and, when a
regenerated geometry HDF is available, compares that RASMapper/HEC-RAS readback
against the proposed mesh points and breaklines.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

_MESH_QA_SCHEMA_VERSION = "ras-agent-mesh-qa/v1"
_FLOAT_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")


@dataclass
class ProposedMeshReadback:
    metrics: dict[str, Any]
    perimeter_geometry: Any = None
    breakline_geometries: list[Any] = field(default_factory=list)
    mesh_points: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))


@dataclass
class HdfMeshReadback:
    metrics: dict[str, Any]
    cell_points: np.ndarray = field(default_factory=lambda: np.empty((0, 2)))
    cell_polygons: Any = None
    cell_faces: Any = None
    mesh_areas: Any = None
    cell_quality_rows: list[dict[str, Any]] = field(default_factory=list)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        if np.isfinite(value):
            return float(value)
        return None
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        result = float(value)
        if math.isfinite(result):
            return result
    except (TypeError, ValueError):
        return None
    return None


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _summary(values: Any) -> dict[str, Any]:
    arr = np.asarray(values, dtype=float).reshape(-1)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {"count": 0}
    return {
        "count": int(arr.size),
        "min": float(np.min(arr)),
        "p10": float(np.percentile(arr, 10)),
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "p90": float(np.percentile(arr, 90)),
        "max": float(np.max(arr)),
    }


def _value_after_equals(line: str) -> str:
    return line.split("=", 1)[1].strip() if "=" in line else ""


def _parse_coord_values(line: str) -> list[float]:
    raw = line.rstrip("\r\n")

    if "," not in raw and len(raw) >= 16:
        fixed_values = []
        fixed_ok = True
        for start in range(0, len(raw), 16):
            chunk = raw[start:start + 16].strip()
            if not chunk:
                continue
            try:
                fixed_values.append(float(chunk))
            except ValueError:
                fixed_ok = False
                break
        if fixed_ok and len(fixed_values) >= 2 and len(fixed_values) % 2 == 0:
            return fixed_values

    return [float(match.group(0)) for match in _FLOAT_RE.finditer(raw)]


def _parse_counted_coords(
    lines: list[str],
    start_idx: int,
    coord_count: int,
) -> tuple[list[tuple[float, float]], int]:
    coords: list[tuple[float, float]] = []
    idx = start_idx
    while idx < len(lines) and len(coords) < coord_count:
        line = lines[idx]
        if "=" in line:
            break
        values = _parse_coord_values(line)
        for value_idx in range(0, len(values) - 1, 2):
            coords.append((values[value_idx], values[value_idx + 1]))
            if len(coords) >= coord_count:
                break
        idx += 1
    return coords, idx


def _parse_point_generation_data(line: str) -> dict[str, Any]:
    raw = _value_after_equals(line)
    parts = [part.strip() for part in raw.split(",")]
    numeric_parts = [_as_float(part) for part in parts]
    cell_sizes = [value for value in numeric_parts[-2:] if value is not None]
    target = float(np.mean(cell_sizes)) if cell_sizes else None
    return {
        "raw": raw,
        "cell_size_x_m": cell_sizes[0] if len(cell_sizes) >= 1 else None,
        "cell_size_y_m": cell_sizes[1] if len(cell_sizes) >= 2 else None,
        "target_cell_size_m": target,
    }


def _storage_area_name(line: str) -> str:
    return _value_after_equals(line).split(",", 1)[0].strip()


def _parse_storage_block(block_lines: list[str]) -> dict[str, Any]:
    name = _storage_area_name(block_lines[0])
    info: dict[str, Any] = {
        "name": name,
        "is_2d": False,
        "surface_line": [],
        "mesh_points": [],
        "point_generation_data": {},
    }

    idx = 1
    while idx < len(block_lines):
        line = block_lines[idx]
        if line.startswith("Storage Area Is2D="):
            info["is_2d"] = _value_after_equals(line).strip() == "-1"
        elif line.startswith("Storage Area Surface Line="):
            count = _as_int(_value_after_equals(line)) or 0
            coords, next_idx = _parse_counted_coords(block_lines, idx + 1, count)
            info["surface_line"] = coords
            idx = next_idx
            continue
        elif line.startswith("Storage Area Point Generation Data="):
            info["point_generation_data"] = _parse_point_generation_data(line)
        elif line.startswith("Storage Area 2D Points="):
            count = _as_int(_value_after_equals(line)) or 0
            coords, next_idx = _parse_counted_coords(block_lines, idx + 1, count)
            info["mesh_points"] = coords
            idx = next_idx
            continue
        idx += 1

    return info


def _parse_breakline_block(block_lines: list[str]) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": _value_after_equals(block_lines[0]),
        "cell_size_min_m": None,
        "cell_size_max_m": None,
        "near_repeats": None,
        "protection_radius": None,
        "coords": [],
    }

    idx = 1
    while idx < len(block_lines):
        line = block_lines[idx]
        if line.startswith("BreakLine CellSize Min="):
            info["cell_size_min_m"] = _as_float(_value_after_equals(line))
        elif line.startswith("BreakLine CellSize Max="):
            info["cell_size_max_m"] = _as_float(_value_after_equals(line))
        elif line.startswith("BreakLine Near Repeats="):
            info["near_repeats"] = _as_int(_value_after_equals(line))
        elif line.startswith("BreakLine Protection Radius="):
            info["protection_radius"] = _as_int(_value_after_equals(line))
        elif line.startswith("BreakLine Polyline="):
            count = _as_int(_value_after_equals(line)) or 0
            coords, next_idx = _parse_counted_coords(block_lines, idx + 1, count)
            info["coords"] = coords
            idx = next_idx
            continue
        idx += 1

    return info


def _parse_geometry_text(geom_file: Path, area_name: Optional[str] = None) -> dict[str, Any]:
    lines = Path(geom_file).read_text(encoding="utf-8", errors="replace").splitlines(
        keepends=True
    )

    storage_areas = []
    idx = 0
    while idx < len(lines):
        if not lines[idx].startswith("Storage Area="):
            idx += 1
            continue
        start_idx = idx
        idx += 1
        while idx < len(lines) and not lines[idx].startswith(("Storage Area=", "River Reach=")):
            idx += 1
        storage_areas.append(_parse_storage_block(lines[start_idx:idx]))

    selected = None
    if area_name:
        selected = next((area for area in storage_areas if area["name"] == area_name), None)
    if selected is None:
        selected = next((area for area in storage_areas if area["is_2d"]), None)
    if selected is None and storage_areas:
        selected = storage_areas[0]
    if selected is None:
        selected = {
            "name": area_name,
            "is_2d": False,
            "surface_line": [],
            "mesh_points": [],
            "point_generation_data": {},
        }

    breaklines = []
    idx = 0
    while idx < len(lines):
        if not lines[idx].startswith("BreakLine Name="):
            idx += 1
            continue
        start_idx = idx
        idx += 1
        stop_prefixes = (
            "BreakLine Name=",
            "BC Line Name=",
            "Connection=",
            "LCMann Time=",
            "Storage Area=",
            "River Reach=",
        )
        while idx < len(lines) and not lines[idx].startswith(stop_prefixes):
            idx += 1
        breaklines.append(_parse_breakline_block(lines[start_idx:idx]))

    return {
        "path": str(geom_file),
        "area_name": selected.get("name"),
        "storage_area_count": len(storage_areas),
        "selected_storage_area": selected,
        "breaklines": breaklines,
    }


def _coord_array(coords: list[tuple[float, float]]) -> np.ndarray:
    if not coords:
        return np.empty((0, 2), dtype=float)
    arr = np.asarray(coords, dtype=float)
    if arr.ndim != 2 or arr.shape[1] != 2:
        return np.empty((0, 2), dtype=float)
    return arr


def _nearest_neighbor_summary(coords: np.ndarray) -> dict[str, Any]:
    coords = _coord_array(coords.tolist() if isinstance(coords, np.ndarray) else coords)
    if len(coords) < 2:
        return {"count": int(len(coords))}
    try:
        from scipy.spatial import cKDTree

        distances, _ = cKDTree(coords).query(coords, k=2)
        return _summary(distances[:, 1])
    except Exception:
        sample = coords
        if len(sample) > 5000:
            sample = sample[np.linspace(0, len(sample) - 1, 5000, dtype=int)]
        deltas = sample[:, None, :] - sample[None, :, :]
        distances = np.sqrt(np.sum(deltas * deltas, axis=2))
        distances[distances == 0.0] = np.nan
        return _summary(np.nanmin(distances, axis=1))


def _line_sample_coords(line_geometries: list[Any], spacing: float, max_samples: int = 10000) -> np.ndarray:
    samples: list[tuple[float, float]] = []
    spacing = max(float(spacing), 1.0)
    for line in line_geometries:
        length = _as_float(getattr(line, "length", None)) or 0.0
        if length <= 0.0:
            continue
        count = max(2, int(math.ceil(length / spacing)) + 1)
        distances = np.linspace(0.0, length, count)
        for distance in distances:
            point = line.interpolate(float(distance))
            samples.append((float(point.x), float(point.y)))
    if not samples:
        return np.empty((0, 2), dtype=float)
    arr = np.asarray(samples, dtype=float)
    if len(arr) > max_samples:
        arr = arr[np.linspace(0, len(arr) - 1, max_samples, dtype=int)]
    return arr


def _nearest_distances(query_coords: np.ndarray, target_coords: np.ndarray) -> np.ndarray:
    query_coords = _coord_array(query_coords.tolist() if isinstance(query_coords, np.ndarray) else query_coords)
    target_coords = _coord_array(target_coords.tolist() if isinstance(target_coords, np.ndarray) else target_coords)
    if len(query_coords) == 0 or len(target_coords) == 0:
        return np.asarray([], dtype=float)
    try:
        from scipy.spatial import cKDTree

        distances, _ = cKDTree(target_coords).query(query_coords, k=1)
        return np.asarray(distances, dtype=float)
    except Exception:
        sample = query_coords
        if len(sample) > 5000:
            sample = sample[np.linspace(0, len(sample) - 1, 5000, dtype=int)]
        distances = []
        for coord in sample:
            delta = target_coords - coord
            distances.append(float(np.min(np.sqrt(np.sum(delta * delta, axis=1)))))
        return np.asarray(distances, dtype=float)


def _distances_to_breaklines(
    point_coords: np.ndarray,
    breakline_geometries: list[Any],
    *,
    sample_limit: int = 100000,
) -> tuple[np.ndarray, bool, int]:
    if len(point_coords) == 0 or not breakline_geometries:
        return np.asarray([], dtype=float), False, 0

    from shapely.geometry import Point
    from shapely.ops import unary_union

    coords = point_coords
    sampled = False
    if len(coords) > sample_limit:
        coords = coords[np.linspace(0, len(coords) - 1, sample_limit, dtype=int)]
        sampled = True

    line_union = unary_union(breakline_geometries)
    distances = np.asarray(
        [Point(float(x), float(y)).distance(line_union) for x, y in coords],
        dtype=float,
    )
    return distances, sampled, len(coords)


def _breakline_adherence_metrics(
    point_coords: np.ndarray,
    breakline_geometries: list[Any],
    target_cell_size_m: Optional[float],
) -> dict[str, Any]:
    if len(point_coords) == 0 or not breakline_geometries:
        return {
            "available": False,
            "reason": "mesh points or breaklines missing",
        }

    target = target_cell_size_m or 50.0
    line_spacing = min(max(target * 0.5, 1.0), 100.0)
    line_samples = _line_sample_coords(breakline_geometries, line_spacing)
    sample_to_point_distances = _nearest_distances(line_samples, point_coords)

    point_to_breakline_distances, sampled, sample_size = _distances_to_breaklines(
        point_coords,
        breakline_geometries,
    )

    thresholds = {
        "within_1m": 1.0,
        "within_half_cell": target * 0.5,
        "within_one_cell": target,
        "within_two_cells": target * 2.0,
    }
    threshold_counts = {}
    for name, threshold in thresholds.items():
        count = int(np.sum(point_to_breakline_distances <= threshold))
        if sampled and sample_size:
            count = int(round(count / sample_size * len(point_coords)))
        threshold_counts[name] = count

    far_threshold = target
    far_samples = int(np.sum(sample_to_point_distances > far_threshold))

    return {
        "available": True,
        "line_sample_count": int(len(line_samples)),
        "line_sample_spacing_m": float(line_spacing),
        "line_sample_nearest_mesh_point_distance_m": _summary(sample_to_point_distances),
        "line_samples_farther_than_target_cell": far_samples,
        "mesh_point_distance_to_breakline_sampled": bool(sampled),
        "mesh_point_distance_sample_count": int(sample_size),
        "mesh_point_distance_to_breakline_m": _summary(point_to_breakline_distances),
        "mesh_point_distance_threshold_counts": threshold_counts,
    }


def _build_proposed_readback(
    parsed: dict[str, Any],
    *,
    target_cell_size_m: Optional[float] = None,
) -> ProposedMeshReadback:
    from shapely.geometry import LineString, Polygon

    selected = parsed["selected_storage_area"]
    point_generation = selected.get("point_generation_data") or {}
    target = target_cell_size_m or point_generation.get("target_cell_size_m")
    perimeter_coords = list(selected.get("surface_line") or [])
    mesh_points = _coord_array(list(selected.get("mesh_points") or []))

    perimeter_geometry = None
    perimeter_metrics: dict[str, Any] = {
        "vertex_count": len(perimeter_coords),
        "area_m2": None,
        "perimeter_m": None,
        "is_valid": None,
    }
    if len(perimeter_coords) >= 3:
        coords = perimeter_coords
        if coords[0] != coords[-1]:
            coords = coords + [coords[0]]
        perimeter_geometry = Polygon(coords)
        perimeter_metrics = {
            "vertex_count": len(coords),
            "area_m2": float(abs(perimeter_geometry.area)),
            "perimeter_m": float(perimeter_geometry.length),
            "is_valid": bool(perimeter_geometry.is_valid),
        }

    breakline_geometries = []
    for breakline in parsed.get("breaklines", []):
        coords = breakline.get("coords") or []
        if len(coords) >= 2:
            breakline_geometries.append(LineString(coords))

    breakline_lengths = [line.length for line in breakline_geometries]
    cell_size_min_values = [
        value for value in (
            _as_float(bl.get("cell_size_min_m")) for bl in parsed.get("breaklines", [])
        )
        if value is not None
    ]
    cell_size_max_values = [
        value for value in (
            _as_float(bl.get("cell_size_max_m")) for bl in parsed.get("breaklines", [])
        )
        if value is not None
    ]

    metrics = {
        "source": "geometry_text",
        "geometry_file": parsed["path"],
        "area_name": parsed.get("area_name"),
        "storage_area_count": parsed.get("storage_area_count", 0),
        "target_cell_size_m": target,
        "point_generation_data": point_generation,
        "perimeter": perimeter_metrics,
        "mesh_point_count": int(len(mesh_points)),
        "mesh_point_spacing_m": _nearest_neighbor_summary(mesh_points),
        "breaklines": {
            "count": len(parsed.get("breaklines", [])),
            "valid_geometry_count": len(breakline_geometries),
            "total_length_m": float(np.sum(breakline_lengths)) if breakline_lengths else 0.0,
            "length_m": _summary(breakline_lengths),
            "cell_size_min_m": _summary(cell_size_min_values),
            "cell_size_max_m": _summary(cell_size_max_values),
        },
    }
    metrics["breakline_adherence"] = _breakline_adherence_metrics(
        mesh_points,
        breakline_geometries,
        target,
    )

    return ProposedMeshReadback(
        metrics=metrics,
        perimeter_geometry=perimeter_geometry,
        breakline_geometries=breakline_geometries,
        mesh_points=mesh_points,
    )


def _decode_hdf_scalar(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip("\x00").strip()
    if isinstance(value, np.generic):
        return value.item()
    return value


def _direct_hdf_arrays(hdf_path: Path, area_name: Optional[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "area_names": [],
        "attributes": {},
        "cell_points": np.empty((0, 2), dtype=float),
        "face_lengths": np.asarray([], dtype=float),
        "face_count_per_cell": np.asarray([], dtype=float),
    }
    try:
        import h5py

        with h5py.File(hdf_path, "r") as hdf:
            attrs_path = "Geometry/2D Flow Areas/Attributes"
            if attrs_path in hdf:
                attrs = hdf[attrs_path][()]
                dtype_names = attrs.dtype.names or ()
                rows = np.atleast_1d(attrs)
                for row in rows:
                    row_data = {
                        name: _decode_hdf_scalar(row[name])
                        for name in dtype_names
                    }
                    row_name = str(row_data.get("Name", "")).strip()
                    if row_name:
                        result["area_names"].append(row_name)
                        result["attributes"][row_name] = row_data

            selected = area_name or (result["area_names"][0] if result["area_names"] else None)
            base = f"Geometry/2D Flow Areas/{selected}" if selected else None
            if base and f"{base}/Cells Center Coordinate" in hdf:
                result["cell_points"] = np.asarray(
                    hdf[f"{base}/Cells Center Coordinate"][()],
                    dtype=float,
                )
            elif "Geometry/2D Flow Areas/Cell Points" in hdf:
                result["cell_points"] = np.asarray(
                    hdf["Geometry/2D Flow Areas/Cell Points"][()],
                    dtype=float,
                )

            if base and f"{base}/Faces NormalUnitVector and Length" in hdf:
                values = np.asarray(hdf[f"{base}/Faces NormalUnitVector and Length"][()])
                if values.ndim == 2 and values.shape[1] >= 3:
                    result["face_lengths"] = values[:, 2].astype(float)

            if base and f"{base}/Cells Face and Orientation Info" in hdf:
                values = np.asarray(hdf[f"{base}/Cells Face and Orientation Info"][()])
                if values.ndim == 2 and values.shape[1] >= 2:
                    result["face_count_per_cell"] = values[:, 1].astype(float)
    except Exception as exc:
        result["direct_hdf_error"] = str(exc)
    return result


def _filter_mesh_gdf(gdf: Any, area_name: Optional[str]) -> Any:
    if gdf is None or getattr(gdf, "empty", True):
        return gdf
    if area_name and "mesh_name" in getattr(gdf, "columns", []):
        filtered = gdf[gdf["mesh_name"] == area_name]
        return filtered if not filtered.empty else gdf
    return gdf


def _coords_from_point_gdf(gdf: Any) -> np.ndarray:
    if gdf is None or getattr(gdf, "empty", True):
        return np.empty((0, 2), dtype=float)
    return np.asarray([(geom.x, geom.y) for geom in gdf.geometry], dtype=float)


def _cell_quality_from_polygons(polygons_gdf: Any, target_cell_size_m: Optional[float]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if polygons_gdf is None or getattr(polygons_gdf, "empty", True):
        return {"available": False, "reason": "cell polygons missing"}, []

    areas = np.asarray([geom.area for geom in polygons_gdf.geometry], dtype=float)
    perimeters = np.asarray([geom.length for geom in polygons_gdf.geometry], dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        compactness = 4.0 * math.pi * areas / (perimeters * perimeters)
    equivalent_size = np.sqrt(np.maximum(areas, 0.0))
    median_area = float(np.nanmedian(areas)) if len(areas) else 0.0
    target_area = float(target_cell_size_m ** 2) if target_cell_size_m else median_area
    min_area_threshold = max(target_area * 0.05, median_area * 0.05, 0.0)

    rows: list[dict[str, Any]] = []
    sliver_count = 0
    invalid_count = 0
    for row_idx, (_, row) in enumerate(polygons_gdf.iterrows()):
        geom = row.geometry
        area = float(areas[row_idx])
        perimeter = float(perimeters[row_idx])
        comp = float(compactness[row_idx]) if np.isfinite(compactness[row_idx]) else None
        is_invalid = not bool(getattr(geom, "is_valid", True))
        is_sliver = bool(
            is_invalid
            or area <= min_area_threshold
            or (comp is not None and comp < 0.05)
        )
        invalid_count += int(is_invalid)
        sliver_count += int(is_sliver)
        rows.append({
            "mesh_name": row.get("mesh_name", ""),
            "cell_id": row.get("cell_id", row_idx),
            "area_m2": area,
            "perimeter_m": perimeter,
            "equivalent_cell_size_m": float(equivalent_size[row_idx]),
            "compactness": comp,
            "is_valid": not is_invalid,
            "sliver_flag": is_sliver,
        })

    metrics = {
        "available": True,
        "cell_count": int(len(rows)),
        "area_m2": _summary(areas),
        "equivalent_cell_size_m": _summary(equivalent_size),
        "compactness": _summary(compactness),
        "sliver_count": int(sliver_count),
        "invalid_polygon_count": int(invalid_count),
        "min_area_threshold_m2": float(min_area_threshold),
    }
    return metrics, rows


def _read_hdf_mesh(
    hdf_path: Path,
    *,
    area_name: Optional[str],
    target_cell_size_m: Optional[float],
    breakline_geometries: list[Any],
) -> HdfMeshReadback:
    hdf_path = Path(hdf_path)
    if not hdf_path.exists():
        return HdfMeshReadback(metrics={
            "available": False,
            "status": "missing",
            "hdf_path": str(hdf_path),
            "message": "Regenerated geometry HDF not found.",
        })

    direct = _direct_hdf_arrays(hdf_path, area_name)
    cell_points_gdf = None
    cell_polygons_gdf = None
    cell_faces_gdf = None
    mesh_areas_gdf = None
    hdfmesh_errors: list[str] = []

    try:
        from ras_commander.hdf import HdfMesh

        mesh_areas_gdf = _filter_mesh_gdf(HdfMesh.get_mesh_areas(hdf_path), area_name)
        cell_points_gdf = _filter_mesh_gdf(HdfMesh.get_mesh_cell_points(hdf_path), area_name)
        cell_faces_gdf = _filter_mesh_gdf(HdfMesh.get_mesh_cell_faces(hdf_path), area_name)
        cell_polygons_gdf = _filter_mesh_gdf(HdfMesh.get_mesh_cell_polygons(hdf_path), area_name)
    except Exception as exc:
        hdfmesh_errors.append(str(exc))

    point_coords = _coords_from_point_gdf(cell_points_gdf)
    if len(point_coords) == 0:
        point_coords = _coord_array(direct.get("cell_points", np.empty((0, 2))).tolist())

    face_lengths = np.asarray(direct.get("face_lengths", np.asarray([])), dtype=float)
    if face_lengths.size == 0 and cell_faces_gdf is not None and not getattr(cell_faces_gdf, "empty", True):
        face_lengths = np.asarray([geom.length for geom in cell_faces_gdf.geometry], dtype=float)

    face_count_per_cell = np.asarray(direct.get("face_count_per_cell", np.asarray([])), dtype=float)

    area_name_selected = area_name
    if not area_name_selected and direct.get("area_names"):
        area_name_selected = direct["area_names"][0]

    attr = direct.get("attributes", {}).get(area_name_selected or "", {})
    attr_cell_count = _as_int(attr.get("Cell Count"))
    attr_face_count = _as_int(attr.get("Face Count"))
    cell_count = int(len(point_coords)) if len(point_coords) else attr_cell_count
    face_count = int(len(cell_faces_gdf)) if cell_faces_gdf is not None and not getattr(cell_faces_gdf, "empty", True) else attr_face_count
    if face_count is None and face_lengths.size:
        face_count = int(face_lengths.size)

    short_face_threshold = float(target_cell_size_m * 0.05) if target_cell_size_m else None
    short_face_count = (
        int(np.sum(face_lengths < short_face_threshold))
        if short_face_threshold is not None and face_lengths.size
        else 0
    )

    cell_quality, quality_rows = _cell_quality_from_polygons(
        cell_polygons_gdf,
        target_cell_size_m,
    )
    adherence = _breakline_adherence_metrics(
        point_coords,
        breakline_geometries,
        target_cell_size_m,
    )

    metrics = {
        "available": True,
        "status": "read",
        "hdf_path": str(hdf_path),
        "area_name": area_name_selected,
        "reader": "ras_commander.hdf.HdfMesh",
        "hdfmesh_errors": hdfmesh_errors,
        "area_names": direct.get("area_names", []),
        "attributes": attr,
        "cell_count": cell_count,
        "face_count": face_count,
        "cell_point_spacing_m": _nearest_neighbor_summary(point_coords),
        "cell_quality": cell_quality,
        "face_length_m": _summary(face_lengths),
        "short_face_threshold_m": short_face_threshold,
        "short_face_count": short_face_count,
        "faces_per_cell": _summary(face_count_per_cell),
        "max_faces_per_cell_exceeded_count": int(np.sum(face_count_per_cell > 8)) if face_count_per_cell.size else 0,
        "breakline_adherence": adherence,
        "direct_hdf_error": direct.get("direct_hdf_error"),
    }

    return HdfMeshReadback(
        metrics=metrics,
        cell_points=point_coords,
        cell_polygons=cell_polygons_gdf,
        cell_faces=cell_faces_gdf,
        mesh_areas=mesh_areas_gdf,
        cell_quality_rows=quality_rows,
    )


def _mesh_result_to_dict(mesh_result: Any) -> Optional[dict[str, Any]]:
    if mesh_result is None:
        return None
    fields = (
        "ok",
        "status",
        "cell_count",
        "face_count",
        "error_message",
        "fixes_applied",
    )
    return {
        field_name: getattr(mesh_result, field_name)
        for field_name in fields
        if hasattr(mesh_result, field_name)
    }


def _delta_metrics(before: Optional[float], after: Optional[float]) -> dict[str, Any]:
    if before is None or after is None:
        return {"available": False}
    before = float(before)
    after = float(after)
    delta = after - before
    pct = (delta / before * 100.0) if before else None
    return {
        "available": True,
        "proposed": before,
        "regenerated": after,
        "delta": delta,
        "delta_pct": pct,
    }


def _build_comparison_metrics(
    proposed: ProposedMeshReadback,
    hdf_readback: HdfMeshReadback,
    mesh_result: Optional[dict[str, Any]],
) -> dict[str, Any]:
    hdf_metrics = hdf_readback.metrics
    regenerated_available = bool(hdf_metrics.get("available"))
    proposed_cell_count = proposed.metrics.get("mesh_point_count")
    hdf_cell_count = hdf_metrics.get("cell_count") if regenerated_available else None

    comparison = {
        "available": regenerated_available,
        "proposed_vs_regenerated_cell_count": _delta_metrics(
            proposed_cell_count,
            hdf_cell_count,
        ),
        "geommesh_result_vs_regenerated_cell_count": _delta_metrics(
            (mesh_result or {}).get("cell_count"),
            hdf_cell_count,
        ),
        "geommesh_result_vs_regenerated_face_count": _delta_metrics(
            (mesh_result or {}).get("face_count"),
            hdf_metrics.get("face_count") if regenerated_available else None,
        ),
    }

    proposed_perimeter_area = proposed.metrics.get("perimeter", {}).get("area_m2")
    hdf_area = None
    if hdf_readback.mesh_areas is not None and not getattr(hdf_readback.mesh_areas, "empty", True):
        hdf_area = float(hdf_readback.mesh_areas.geometry.iloc[0].area)
    comparison["perimeter_area_m2"] = _delta_metrics(proposed_perimeter_area, hdf_area)

    return comparison


def _add_flag(
    flags: list[dict[str, Any]],
    flag_id: str,
    severity: str,
    message: str,
    details: Optional[dict[str, Any]] = None,
) -> None:
    flags.append({
        "id": flag_id,
        "severity": severity,
        "message": message,
        "details": details or {},
    })


def _pct_abs(delta_metrics: dict[str, Any]) -> Optional[float]:
    pct = delta_metrics.get("delta_pct")
    if pct is None:
        return None
    return abs(float(pct))


def _build_reviewer_flags(
    proposed: ProposedMeshReadback,
    hdf_readback: HdfMeshReadback,
    comparison: dict[str, Any],
    mesh_result: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    proposed_metrics = proposed.metrics
    hdf_metrics = hdf_readback.metrics

    if mesh_result and mesh_result.get("ok") is False:
        _add_flag(
            flags,
            "geommesh_generation_incomplete",
            "high",
            "Headless GeomMesh generation did not complete cleanly.",
            {
                "status": mesh_result.get("status"),
                "error_message": mesh_result.get("error_message"),
                "fixes_applied": mesh_result.get("fixes_applied", []),
            },
        )

    if proposed_metrics.get("mesh_point_count", 0) == 0:
        _add_flag(
            flags,
            "text_mesh_points_missing",
            "high",
            "The proposed .g## file has no Storage Area 2D Points mesh seed block.",
            {"geometry_file": proposed_metrics.get("geometry_file")},
        )

    if proposed_metrics.get("breaklines", {}).get("count", 0) == 0:
        _add_flag(
            flags,
            "breaklines_missing",
            "medium",
            "No breakline blocks were found in the geometry text.",
        )

    if not hdf_metrics.get("available"):
        _add_flag(
            flags,
            "regenerated_hdf_missing",
            "medium",
            "RASMapper/HEC-RAS regenerated geometry HDF readback is not available for comparison.",
            {"hdf_path": hdf_metrics.get("hdf_path")},
        )

    proposed_adherence = proposed_metrics.get("breakline_adherence", {})
    threshold_counts = proposed_adherence.get("mesh_point_distance_threshold_counts", {})
    near_centerline = threshold_counts.get("within_1m", 0)
    mesh_point_count = max(int(proposed_metrics.get("mesh_point_count") or 0), 1)
    near_threshold = max(10, int(mesh_point_count * 0.001))
    if near_centerline > near_threshold:
        severity = "high" if near_centerline > mesh_point_count * 0.01 else "medium"
        _add_flag(
            flags,
            "near_centerline_mesh_points",
            severity,
            "Mesh points are concentrated within 1 m of breaklines; this matches a known RASMapper divergence mode.",
            {
                "within_1m_count": near_centerline,
                "threshold_count": near_threshold,
                "sampled": proposed_adherence.get("mesh_point_distance_to_breakline_sampled"),
            },
        )

    if hdf_metrics.get("available"):
        quality = hdf_metrics.get("cell_quality", {})
        sliver_count = int(quality.get("sliver_count") or 0)
        if sliver_count:
            _add_flag(
                flags,
                "sliver_cells_detected",
                "high" if sliver_count > 25 else "medium",
                "Regenerated mesh contains cells flagged by area/compactness sliver checks.",
                {"sliver_count": sliver_count},
            )

        short_face_count = int(hdf_metrics.get("short_face_count") or 0)
        if short_face_count:
            _add_flag(
                flags,
                "short_faces_detected",
                "medium",
                "Regenerated mesh contains faces shorter than 5 percent of the target cell size.",
                {
                    "short_face_count": short_face_count,
                    "threshold_m": hdf_metrics.get("short_face_threshold_m"),
                },
            )

        max_face_exceeded = int(hdf_metrics.get("max_faces_per_cell_exceeded_count") or 0)
        if max_face_exceeded:
            _add_flag(
                flags,
                "max_faces_per_cell_exceeded",
                "high",
                "Regenerated mesh contains cells with more than 8 faces.",
                {"cell_count": max_face_exceeded},
            )

        hdf_adherence = hdf_metrics.get("breakline_adherence", {})
        far_samples = int(hdf_adherence.get("line_samples_farther_than_target_cell") or 0)
        sample_count = max(int(hdf_adherence.get("line_sample_count") or 0), 1)
        if far_samples:
            severity = "high" if far_samples / sample_count > 0.10 else "medium"
            _add_flag(
                flags,
                "breakline_underresolved",
                severity,
                "Some sampled breakline locations are farther than one target cell from a regenerated cell center.",
                {"far_sample_count": far_samples, "sample_count": sample_count},
            )

    for metric_name in (
        "proposed_vs_regenerated_cell_count",
        "geommesh_result_vs_regenerated_cell_count",
        "geommesh_result_vs_regenerated_face_count",
    ):
        delta = comparison.get(metric_name, {})
        delta_pct_abs = _pct_abs(delta)
        if delta_pct_abs is not None and delta_pct_abs > 5.0:
            _add_flag(
                flags,
                f"{metric_name}_delta",
                "high" if delta_pct_abs > 10.0 else "medium",
                "Regenerated mesh count differs from the proposed/headless count beyond the QA tolerance.",
                delta,
            )

    return flags


def _flatten_metrics(data: dict[str, Any], prefix: str = "") -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            rows.extend(_flatten_metrics(value, full_key))
        elif isinstance(value, list):
            if value and isinstance(value[0], dict):
                rows.append({"metric": full_key, "value": f"{len(value)} row(s)"})
            else:
                rows.append({"metric": full_key, "value": json.dumps(value, default=_json_default)})
        else:
            rows.append({"metric": full_key, "value": "" if value is None else str(value)})
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: json.dumps(value, default=_json_default)
                if isinstance(value, (dict, list))
                else value
                for key, value in row.items()
            })


def _write_cell_quality_csv(path: Path, rows: list[dict[str, Any]]) -> Optional[str]:
    if not rows:
        return None
    fieldnames = [
        "mesh_name",
        "cell_id",
        "area_m2",
        "perimeter_m",
        "equivalent_cell_size_m",
        "compactness",
        "is_valid",
        "sliver_flag",
    ]
    _write_csv(path, rows, fieldnames)
    return str(path)


def _plot_overview(
    path: Path,
    proposed: ProposedMeshReadback,
    hdf_readback: HdfMeshReadback,
) -> Optional[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(10, 8), constrained_layout=True)

        if hdf_readback.cell_polygons is not None and not getattr(hdf_readback.cell_polygons, "empty", True):
            polygons = hdf_readback.cell_polygons
            if len(polygons) > 2000:
                polygons = polygons.iloc[np.linspace(0, len(polygons) - 1, 2000, dtype=int)]
            polygons.boundary.plot(ax=ax, color="#9e9e9e", linewidth=0.25, alpha=0.6, label="Regenerated cells")
        elif hdf_readback.cell_faces is not None and not getattr(hdf_readback.cell_faces, "empty", True):
            faces = hdf_readback.cell_faces
            if len(faces) > 4000:
                faces = faces.iloc[np.linspace(0, len(faces) - 1, 4000, dtype=int)]
            faces.plot(ax=ax, color="#bdbdbd", linewidth=0.25, alpha=0.6, label="Regenerated faces")

        if proposed.perimeter_geometry is not None:
            x, y = proposed.perimeter_geometry.exterior.xy
            ax.plot(x, y, color="#1f2937", linewidth=1.2, label="2D perimeter")

        for idx, line in enumerate(proposed.breakline_geometries):
            x, y = line.xy
            ax.plot(
                x,
                y,
                color="#d62728",
                linewidth=0.8,
                alpha=0.8,
                label="Breaklines" if idx == 0 else None,
            )

        if len(proposed.mesh_points):
            pts = proposed.mesh_points
            if len(pts) > 6000:
                pts = pts[np.linspace(0, len(pts) - 1, 6000, dtype=int)]
            ax.scatter(
                pts[:, 0],
                pts[:, 1],
                s=2,
                color="#1f77b4",
                alpha=0.35,
                linewidths=0,
                label="Proposed mesh points",
            )

        if len(hdf_readback.cell_points):
            pts = hdf_readback.cell_points
            if len(pts) > 6000:
                pts = pts[np.linspace(0, len(pts) - 1, 6000, dtype=int)]
            ax.scatter(
                pts[:, 0],
                pts[:, 1],
                s=2,
                color="#2ca02c",
                alpha=0.35,
                linewidths=0,
                label="Regenerated cell centers",
            )

        ax.set_aspect("equal", adjustable="box")
        ax.set_title("Mesh QA Overview")
        ax.set_xlabel("X")
        ax.set_ylabel("Y")
        handles, labels = ax.get_legend_handles_labels()
        if labels:
            deduped = dict(zip(labels, handles))
            ax.legend(deduped.values(), deduped.keys(), loc="best", fontsize=8)
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=180)
        plt.close(fig)
        return str(path)
    except Exception as exc:
        logger.warning("Mesh QA figure generation failed: %s", exc)
        return None


def build_mesh_qa_package(
    geom_file: Path,
    *,
    output_dir: Path,
    area_name: Optional[str] = None,
    regenerated_hdf_path: Optional[Path] = None,
    target_cell_size_m: Optional[float] = None,
    mesh_result: Any = None,
) -> dict[str, Any]:
    """
    Generate mesh QA metrics, tables, reviewer flags, and overview figures.

    The package is intentionally tolerant of missing regenerated HDF readback so
    geometry-first builds can still emit proposed-geometry QA and a clear review
    flag before the Windows/RASMapper regeneration step has happened.
    """
    geom_file = Path(geom_file)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    regenerated_hdf_path = (
        Path(regenerated_hdf_path)
        if regenerated_hdf_path is not None
        else geom_file.with_suffix(geom_file.suffix + ".hdf")
    )

    parsed = _parse_geometry_text(geom_file, area_name=area_name)
    proposed = _build_proposed_readback(
        parsed,
        target_cell_size_m=target_cell_size_m,
    )
    target = target_cell_size_m or proposed.metrics.get("target_cell_size_m")
    hdf_readback = _read_hdf_mesh(
        regenerated_hdf_path,
        area_name=area_name or proposed.metrics.get("area_name"),
        target_cell_size_m=target,
        breakline_geometries=proposed.breakline_geometries,
    )
    mesh_result_dict = _mesh_result_to_dict(mesh_result)
    comparison = _build_comparison_metrics(proposed, hdf_readback, mesh_result_dict)
    flags = _build_reviewer_flags(proposed, hdf_readback, comparison, mesh_result_dict)

    metrics: dict[str, Any] = {
        "schema_version": _MESH_QA_SCHEMA_VERSION,
        "generated_at": _utc_timestamp(),
        "geometry_file": str(geom_file),
        "area_name": proposed.metrics.get("area_name"),
        "target_cell_size_m": target,
        "proposed": proposed.metrics,
        "regenerated": hdf_readback.metrics,
        "geommesh_result": mesh_result_dict,
        "comparison": comparison,
        "reviewer_flags": flags,
        "status": "review_flags" if flags else "ok",
    }

    flags_csv = output_dir / "mesh_qa_flags.csv"
    summary_csv = output_dir / "mesh_qa_summary.csv"
    cell_quality_csv = output_dir / "mesh_cell_quality.csv"
    figure_path = output_dir / "mesh_qa_overview.png"
    metrics_json = output_dir / "mesh_qa_metrics.json"

    cell_quality_path = _write_cell_quality_csv(
        cell_quality_csv,
        hdf_readback.cell_quality_rows,
    )
    overview_path = _plot_overview(figure_path, proposed, hdf_readback)
    if overview_path is None:
        _add_flag(
            flags,
            "mesh_overview_figure_missing",
            "low",
            "Mesh QA overview figure could not be generated.",
            {"figure_path": str(figure_path)},
        )
        metrics["reviewer_flags"] = flags
        metrics["status"] = "review_flags"

    _write_csv(
        flags_csv,
        flags,
        ["id", "severity", "message", "details"],
    )
    _write_csv(
        summary_csv,
        _flatten_metrics({
            key: value
            for key, value in metrics.items()
            if key not in {"reviewer_flags"}
        }),
        ["metric", "value"],
    )

    artifacts = {
        "metrics_json": str(metrics_json),
        "summary_csv": str(summary_csv),
        "flags_csv": str(flags_csv),
        "overview_png": overview_path,
        "cell_quality_csv": cell_quality_path,
    }
    metrics["artifacts"] = artifacts

    metrics_json.write_text(
        json.dumps(metrics, indent=2, default=_json_default),
        encoding="utf-8",
    )

    logger.info(
        "Mesh QA package written: %s (%d reviewer flags)",
        output_dir,
        len(flags),
    )
    return {
        "status": metrics["status"],
        "metrics": metrics,
        "artifacts": artifacts,
        "flags": flags,
    }
