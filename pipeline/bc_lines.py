"""
Automatic 2D Boundary Condition Line generation for geometry_first models.

Generates BC Lines from watershed boundary, terrain DEM, and TauDEM stream
network intersections. Produces .g01 text blocks and .u01 Boundary Location
entries for HEC-RAS 2D simulations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from shapely.geometry import LineString, MultiPoint, Point, Polygon
from shapely.ops import nearest_points, snap

logger = logging.getLogger(__name__)

BC_NAME_WIDTH = 32
AREA_NAME_WIDTH = 16
COORD_FIELD_WIDTH = 16
COORDS_PER_LINE = 4

DEFAULT_THRESHOLD_FT = 30
DEFAULT_OFFSET_FT = 500
DEFAULT_PERIMETER_SLOPE = 0.00033
BOUNDARY_SNAP_TOLERANCE_M = 10.0
WALK_STEP_M = 10.0


@dataclass
class BCLineSpec:
    """Single BC Line specification."""

    name: str
    storage_area: str
    coords: list[tuple[float, float]]
    bc_type: str  # "normal_depth" or "flow_hydrograph"
    slope: Optional[float] = None
    flow_count: Optional[int] = None
    boundary_t_start: float = 0.0
    boundary_t_end: float = 0.0


@dataclass
class BCLineSet:
    """Complete set of BC Lines for a 2D model."""

    bc_lines: list[BCLineSpec] = field(default_factory=list)
    area_name: str = "MainArea"

    @property
    def outlet(self) -> Optional[BCLineSpec]:
        return next((bc for bc in self.bc_lines if bc.name == "DSOutflow"), None)

    @property
    def inflows(self) -> list[BCLineSpec]:
        return [bc for bc in self.bc_lines if bc.name.startswith("USInflow")]

    @property
    def normal_depth_perimeter(self) -> list[BCLineSpec]:
        return [bc for bc in self.bc_lines if bc.name.startswith("NormDepth")]


# ── Formatters ────────────────────────────────────────────────────────────────


def _format_bc_arc_coords(coords: list[tuple[float, float]]) -> str:
    """
    Format BC Line arc coordinates in HEC-RAS 16-char fixed-width format.

    4 values per line (x1, y1, x2, y2), each right-justified to 16 chars.
    """
    values = []
    for x, y in coords:
        values.append(x)
        values.append(y)

    lines = []
    for i in range(0, len(values), COORDS_PER_LINE):
        chunk = values[i : i + COORDS_PER_LINE]
        line = "".join(f"{v:>{COORD_FIELD_WIDTH}}" for v in chunk)
        lines.append(line)
    return "\n".join(lines)


def format_bc_line_block(spec: BCLineSpec) -> str:
    """
    Produce .g01 text block for one BC Line.

    Format:
        BC Line Name={name:32s}
        BC Line Storage Area={area:16s}
        BC Line Start Position= {x0} , {y0}
        BC Line Middle Position= {xm} , {ym}
        BC Line End Position= {xn} , {yn}
        BC Line Arc= {N}
        {16-char fixed-width coords}
        BC Line Text Position= {xm} , {ym}
    """
    name_padded = spec.name.ljust(BC_NAME_WIDTH)
    area_padded = spec.storage_area.ljust(AREA_NAME_WIDTH)

    coords = spec.coords
    n = len(coords)
    start = coords[0]
    end = coords[-1]
    mid_idx = n // 2
    mid = coords[mid_idx]

    arc_block = _format_bc_arc_coords(coords)

    lines = [
        f"BC Line Name={name_padded}",
        f"BC Line Storage Area={area_padded}",
        f"BC Line Start Position= {start[0]} , {start[1]} ",
        f"BC Line Middle Position= {mid[0]} , {mid[1]} ",
        f"BC Line End Position= {end[0]} , {end[1]} ",
        f"BC Line Arc= {n} ",
        arc_block,
        f"BC Line Text Position= {mid[0]} , {mid[1]} ",
    ]
    return "\n".join(lines)


def format_2d_boundary_location(
    area_name: str,
    bc_name: str,
    bc_type: str,
    slope: Optional[float] = None,
    flow_count: Optional[int] = None,
    interval: str = "15MIN",
) -> str:
    """
    Produce .u01 Boundary Location block for one 2D BC.

    Format:
        Boundary Location={16},{16},{8},{8},{16},{area:16},{16},{bc_name:32}
        Friction Slope={slope},0
        -or-
        Interval={interval}
        Flow Hydrograph= {N}
    """
    f1 = " " * 16  # River (blank for 2D)
    f2 = " " * 16  # Reach (blank for 2D)
    f3 = " " * 8   # RS upstream (blank for 2D)
    f4 = " " * 8   # RS downstream (blank for 2D)
    f5 = " " * 16  # Structure (blank for 2D)
    f6 = area_name.ljust(AREA_NAME_WIDTH)
    f7 = " " * 16  # (blank)
    f8 = bc_name.ljust(BC_NAME_WIDTH)

    header = f"Boundary Location={f1},{f2},{f3},{f4},{f5},{f6},{f7},{f8}"

    if bc_type == "normal_depth":
        return f"{header}\nFriction Slope={slope},0\n"
    elif bc_type == "flow_hydrograph":
        return f"{header}\nInterval={interval}\nFlow Hydrograph= {flow_count}\n"
    else:
        raise ValueError(f"Unknown bc_type: {bc_type}")


# ── DEM Sampling ──────────────────────────────────────────────────────────────


def _sample_dem_elevation(dem_path: Path, point: Point) -> Optional[float]:
    """Sample DEM elevation at a point. Returns None if nodata."""
    import rasterio

    with rasterio.open(dem_path) as src:
        row, col = src.index(point.x, point.y)
        if 0 <= row < src.height and 0 <= col < src.width:
            val = src.read(1)[row, col]
            if val == src.nodata:
                return None
            return float(val)
    return None


def _sample_dem_along_boundary(
    dem_path: Path, boundary: LineString, t_start: float, direction: int, step_m: float
) -> list[tuple[float, float]]:
    """
    Walk along boundary from t_start in given direction, sampling DEM.

    Returns list of (t, elevation) pairs. direction: +1 or -1.
    """
    import rasterio

    boundary_length = boundary.length
    results = []

    with rasterio.open(dem_path) as src:
        raster_data = src.read(1)
        nodata = src.nodata

        t = t_start
        while True:
            t += direction * (step_m / boundary_length)
            # Wrap around [0, 1]
            t = t % 1.0
            if abs(t - t_start) < 1e-9:
                break

            pt = boundary.interpolate(t, normalized=True)
            row, col = src.index(pt.x, pt.y)
            if 0 <= row < src.height and 0 <= col < src.width:
                val = raster_data[row, col]
                if nodata is not None and val == nodata:
                    results.append((t, None))
                else:
                    results.append((t, float(val)))
            else:
                results.append((t, None))

            if len(results) > 5000:
                break

    return results


# ── Core Algorithm ────────────────────────────────────────────────────────────


def _find_stream_boundary_intersections(
    streams: list[LineString], basin: Polygon
) -> list[dict]:
    """
    Find all intersection points between streams and basin boundary.

    Returns list of dicts with keys: point, t (normalized boundary position),
    stream_index.
    """
    boundary = basin.boundary
    intersections = []

    for idx, stream in enumerate(streams):
        if stream is None or stream.is_empty:
            continue
        snapped = snap(stream, boundary, BOUNDARY_SNAP_TOLERANCE_M)
        isect = snapped.intersection(boundary)

        if isect.is_empty:
            continue

        points = []
        if isect.geom_type == "Point":
            points = [isect]
        elif isect.geom_type == "MultiPoint":
            points = list(isect.geoms)
        elif isect.geom_type == "GeometryCollection":
            points = [g for g in isect.geoms if g.geom_type == "Point"]

        for pt in points:
            t = boundary.project(pt, normalized=True)
            intersections.append({
                "point": pt,
                "t": t,
                "stream_index": idx,
            })

    intersections.sort(key=lambda x: x["t"])
    return intersections


def _classify_outlet(
    intersections: list[dict], pour_point: Point, max_dist_m: float = 5000.0
) -> tuple[Optional[dict], list[dict]]:
    """
    Classify which intersection is the outlet (closest to pour_point).

    The pour point may be downstream of the basin boundary (e.g., snapped to
    a stream cell outside the watershed). Uses generous max_dist_m default
    and falls back to closest intersection if only one is much closer than
    the others.

    Returns (outlet, inflows) tuple.
    """
    if not intersections:
        return None, []

    dists = [(isect, isect["point"].distance(pour_point)) for isect in intersections]
    dists.sort(key=lambda x: x[1])

    outlet_candidate, min_dist = dists[0]

    if min_dist > max_dist_m:
        # Even with generous threshold, no intersection is close — still pick
        # the closest one if it's clearly the nearest (2× closer than next)
        if len(dists) >= 2 and dists[0][1] < dists[1][1] * 0.5:
            outlet = dists[0][0]
        else:
            return None, intersections
    else:
        outlet = outlet_candidate

    inflows = [i for i in intersections if i is not outlet]
    return outlet, inflows


def _find_bc_extent_along_boundary(
    boundary: LineString,
    crossing_t: float,
    dem_path: Optional[Path],
    crossing_elev: Optional[float],
    threshold_ft: float = DEFAULT_THRESHOLD_FT,
    step_m: float = WALK_STEP_M,
) -> tuple[float, float]:
    """
    Walk along boundary in both directions from crossing_t until terrain
    rises threshold_ft above crossing elevation.

    Returns (t_left, t_right) — boundary parameter extents for BC Line.
    Falls back to fixed angular extent if no DEM.
    """
    threshold_m = threshold_ft * 0.3048

    if dem_path is None or crossing_elev is None:
        # Fallback: ~10° arc from centroid ≈ 0.028 of boundary
        half_extent = 0.028
        t_left = (crossing_t - half_extent) % 1.0
        t_right = (crossing_t + half_extent) % 1.0
        return t_left, t_right

    target_elev = crossing_elev + threshold_m

    # Walk left (negative direction)
    t_left = crossing_t
    samples_left = _sample_dem_along_boundary(dem_path, boundary, crossing_t, -1, step_m)
    for t, elev in samples_left:
        if elev is not None and elev >= target_elev:
            t_left = t
            break
    else:
        if samples_left:
            t_left = samples_left[-1][0]

    # Walk right (positive direction)
    t_right = crossing_t
    samples_right = _sample_dem_along_boundary(dem_path, boundary, crossing_t, +1, step_m)
    for t, elev in samples_right:
        if elev is not None and elev >= target_elev:
            t_right = t
            break
    else:
        if samples_right:
            t_right = samples_right[-1][0]

    return t_left, t_right


def _extract_boundary_subarc(
    boundary: LineString, t_start: float, t_end: float, n_points: int = 20
) -> list[tuple[float, float]]:
    """Extract sub-arc from boundary between t_start and t_end."""
    if t_start < t_end:
        ts = np.linspace(t_start, t_end, n_points)
    else:
        # Wraps around 0
        ts = np.concatenate([
            np.linspace(t_start, 1.0, n_points // 2),
            np.linspace(0.0, t_end, n_points // 2),
        ])

    points = []
    for t in ts:
        pt = boundary.interpolate(t, normalized=True)
        points.append((pt.x, pt.y))
    return points


def _build_offset_polyline(
    boundary: LineString,
    basin_poly: Polygon,
    t_start: float,
    t_end: float,
    offset_ft: float = DEFAULT_OFFSET_FT,
    simplify_n: int = 6,
) -> list[tuple[float, float]]:
    """
    Build BC Line polyline offset outward from basin boundary.

    Extracts sub-arc, offsets outward, simplifies to target vertex count.
    """
    offset_m = offset_ft * 0.3048
    subarc_coords = _extract_boundary_subarc(boundary, t_start, t_end)
    subarc = LineString(subarc_coords)

    # Determine which side is outward
    offset_left = subarc.offset_curve(offset_m)
    offset_right = subarc.offset_curve(-offset_m)

    # The one farther from basin centroid is outward
    centroid = basin_poly.centroid
    if offset_left.is_empty and offset_right.is_empty:
        return subarc_coords

    if offset_left.is_empty:
        offset_line = offset_right
    elif offset_right.is_empty:
        offset_line = offset_left
    else:
        d_left = offset_left.distance(centroid)
        d_right = offset_right.distance(centroid)
        offset_line = offset_left if d_left > d_right else offset_right

    # Simplify to target vertex count
    tolerance = offset_line.length / max(simplify_n, 2)
    simplified = offset_line.simplify(tolerance)

    coords = list(simplified.coords)
    # Ensure at least 2 points
    if len(coords) < 2:
        coords = list(offset_line.coords[:2])

    return coords


def _fill_normal_depth_gaps(
    boundary: LineString,
    basin_poly: Polygon,
    stream_bcs: list[BCLineSpec],
    area_name: str,
    slope: float,
    offset_ft: float = DEFAULT_OFFSET_FT,
    gap_fraction: float = 0.005,
) -> list[BCLineSpec]:
    """
    Fill boundary gaps between stream BCs with Normal Depth BC Lines.

    Leaves a gap of gap_fraction (normalized) between each stream BC and
    the adjacent Normal Depth BC.
    """
    if not stream_bcs:
        # Full perimeter normal depth
        coords = _build_offset_polyline(boundary, basin_poly, 0.0, 0.99)
        return [BCLineSpec(
            name="NormDepth1",
            storage_area=area_name,
            coords=coords,
            bc_type="normal_depth",
            slope=slope,
            boundary_t_start=0.0,
            boundary_t_end=0.99,
        )]

    # Sort stream BCs by boundary position
    sorted_bcs = sorted(stream_bcs, key=lambda bc: bc.boundary_t_start)

    normal_depth_bcs = []
    nd_idx = 1

    for i in range(len(sorted_bcs)):
        current_end = sorted_bcs[i].boundary_t_end + gap_fraction
        if i + 1 < len(sorted_bcs):
            next_start = sorted_bcs[i + 1].boundary_t_start - gap_fraction
        else:
            next_start = sorted_bcs[0].boundary_t_start - gap_fraction + 1.0

        # Normalize
        current_end = current_end % 1.0
        next_start = next_start % 1.0

        # Skip if gap is too small
        if current_end == next_start:
            continue
        gap_size = (next_start - current_end) % 1.0
        if gap_size < 0.01:
            continue

        coords = _build_offset_polyline(
            boundary, basin_poly, current_end, next_start
        )

        if len(coords) >= 2:
            name = f"NormDepth{nd_idx}"
            if len(name) > 16:
                name = f"ND{nd_idx}"
            normal_depth_bcs.append(BCLineSpec(
                name=name,
                storage_area=area_name,
                coords=coords,
                bc_type="normal_depth",
                slope=slope,
                boundary_t_start=current_end,
                boundary_t_end=next_start,
            ))
            nd_idx += 1

    return normal_depth_bcs


# ── Main Entry Point ──────────────────────────────────────────────────────────


def generate_bc_lines(
    basin: Polygon,
    streams: list[LineString],
    pour_point: Point,
    dem_path: Optional[Path] = None,
    area_name: str = "MainArea",
    channel_slope: float = 0.005,
    threshold_ft: float = DEFAULT_THRESHOLD_FT,
    offset_ft: float = DEFAULT_OFFSET_FT,
    perimeter_slope: float = DEFAULT_PERIMETER_SLOPE,
) -> BCLineSet:
    """
    Generate complete BC Line set from watershed data.

    Args:
        basin: Watershed polygon (projected CRS, e.g. EPSG:5070)
        streams: TauDEM stream network LineStrings
        pour_point: Outlet point
        dem_path: Path to clipped DEM (optional; uses fallback extents if None)
        area_name: 2D flow area name in .g01
        channel_slope: Main channel slope (m/m) for outlet normal depth
        threshold_ft: Elevation rise threshold for BC extent (20-40 ft)
        offset_ft: BC Line offset distance from boundary (ft)
        perimeter_slope: Friction slope for perimeter normal depth BCs

    Returns:
        BCLineSet with all generated BC Lines
    """
    boundary = basin.boundary
    bc_set = BCLineSet(area_name=area_name)

    # Step 1: Find stream-boundary intersections
    intersections = _find_stream_boundary_intersections(streams, basin)
    logger.info("Found %d stream-boundary intersections", len(intersections))

    if not intersections:
        # Fallback: single outlet near pour_point + single inflow at farthest point
        return _fallback_no_intersections(basin, pour_point, dem_path, area_name,
                                          channel_slope, perimeter_slope, offset_ft)

    # Step 2: Classify outlet vs. inflow
    outlet, inflows = _classify_outlet(intersections, pour_point)

    # Step 3-4: Generate BC Lines for each stream crossing
    stream_bcs = []

    if outlet is not None:
        crossing_elev = None
        if dem_path is not None:
            crossing_elev = _sample_dem_elevation(dem_path, outlet["point"])

        t_left, t_right = _find_bc_extent_along_boundary(
            boundary, outlet["t"], dem_path, crossing_elev, threshold_ft
        )
        coords = _build_offset_polyline(boundary, basin, t_left, t_right, offset_ft)

        outlet_bc = BCLineSpec(
            name="DSOutflow",
            storage_area=area_name,
            coords=coords,
            bc_type="normal_depth",
            slope=channel_slope,
            boundary_t_start=t_left,
            boundary_t_end=t_right,
        )
        stream_bcs.append(outlet_bc)
        bc_set.bc_lines.append(outlet_bc)

    for idx, inflow in enumerate(inflows, start=1):
        crossing_elev = None
        if dem_path is not None:
            crossing_elev = _sample_dem_elevation(dem_path, inflow["point"])

        t_left, t_right = _find_bc_extent_along_boundary(
            boundary, inflow["t"], dem_path, crossing_elev, threshold_ft
        )
        coords = _build_offset_polyline(boundary, basin, t_left, t_right, offset_ft)

        name = f"USInflow{idx}"
        inflow_bc = BCLineSpec(
            name=name,
            storage_area=area_name,
            coords=coords,
            bc_type="flow_hydrograph",
            boundary_t_start=t_left,
            boundary_t_end=t_right,
        )
        stream_bcs.append(inflow_bc)
        bc_set.bc_lines.append(inflow_bc)

    # Step 6: Fill gaps with Normal Depth
    normal_depth_bcs = _fill_normal_depth_gaps(
        boundary, basin, stream_bcs, area_name, perimeter_slope, offset_ft
    )
    bc_set.bc_lines.extend(normal_depth_bcs)

    logger.info(
        "Generated %d BC Lines: 1 outlet, %d inflows, %d normal depth",
        len(bc_set.bc_lines), len(inflows), len(normal_depth_bcs),
    )
    return bc_set


def _fallback_no_intersections(
    basin: Polygon,
    pour_point: Point,
    dem_path: Optional[Path],
    area_name: str,
    channel_slope: float,
    perimeter_slope: float,
    offset_ft: float,
) -> BCLineSet:
    """Fallback when no stream-boundary intersections found."""
    boundary = basin.boundary
    bc_set = BCLineSet(area_name=area_name)

    # Outlet near pour_point
    t_outlet = boundary.project(pour_point, normalized=True)
    t_left = (t_outlet - 0.028) % 1.0
    t_right = (t_outlet + 0.028) % 1.0
    coords = _build_offset_polyline(boundary, basin, t_left, t_right, offset_ft)

    bc_set.bc_lines.append(BCLineSpec(
        name="DSOutflow",
        storage_area=area_name,
        coords=coords,
        bc_type="normal_depth",
        slope=channel_slope,
        boundary_t_start=t_left,
        boundary_t_end=t_right,
    ))

    # Inflow at farthest boundary point from pour_point
    t_inflow = (t_outlet + 0.5) % 1.0
    t_left_in = (t_inflow - 0.028) % 1.0
    t_right_in = (t_inflow + 0.028) % 1.0
    coords_in = _build_offset_polyline(boundary, basin, t_left_in, t_right_in, offset_ft)

    bc_set.bc_lines.append(BCLineSpec(
        name="USInflow1",
        storage_area=area_name,
        coords=coords_in,
        bc_type="flow_hydrograph",
        boundary_t_start=t_left_in,
        boundary_t_end=t_right_in,
    ))

    # Fill remaining with normal depth
    stream_bcs = list(bc_set.bc_lines)
    nd_bcs = _fill_normal_depth_gaps(
        boundary, basin, stream_bcs, area_name, perimeter_slope, offset_ft
    )
    bc_set.bc_lines.extend(nd_bcs)

    logger.info("Fallback BC Lines: 1 outlet + 1 inflow + %d normal depth", len(nd_bcs))
    return bc_set


# ── File Integration Helpers ──────────────────────────────────────────────────


def append_bc_lines_to_geom(geom_file: Path, bc_set: BCLineSet) -> None:
    """
    Insert BC Line blocks into .g01 text file before LCMann Time= line.

    If LCMann Time= not found, appends at end of file.
    """
    text = geom_file.read_text(encoding="utf-8")
    lines = text.splitlines()

    bc_blocks = []
    for spec in bc_set.bc_lines:
        bc_blocks.append(format_bc_line_block(spec))

    bc_text = "\n".join(bc_blocks)

    # Find insertion point (before LCMann Time=)
    insert_idx = None
    for i, line in enumerate(lines):
        if line.startswith("LCMann Time="):
            insert_idx = i
            break

    if insert_idx is not None:
        lines.insert(insert_idx, bc_text)
    else:
        lines.append(bc_text)

    geom_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Appended %d BC Lines to %s", len(bc_set.bc_lines), geom_file.name)


def write_unsteady_flow_file_2d(
    flow_file_path: Path,
    hydro_set,
    return_period: int,
    bc_set: BCLineSet,
    channel_slope: float,
    interval: str = "15MIN",
) -> None:
    """
    Write .u## file with 2D Boundary Location entries (replaces 1D refs).

    Each BC Line gets a Boundary Location block. Flow hydrograph BCs get the
    full hydrograph data. Normal depth BCs get friction slope.
    """
    hydro = hydro_set.get(return_period)
    if hydro is None:
        raise ValueError(
            f"Return period {return_period} not found in HydrographSet. "
            f"Available: {sorted(hydro_set.hydrographs.keys())}"
        )

    flows = hydro.flows_cfs
    n_points = len(flows)
    title = f"T={return_period}yr Hydrograph"

    parts = [
        f"Flow Title={title}\n",
        "Program Version=6.60\n",
        "BEGIN FILE DESCRIPTION:\n",
        "END FILE DESCRIPTION:\n",
        "\n",
    ]

    # Count inflow BCs to split hydrograph
    inflow_bcs = [bc for bc in bc_set.bc_lines if bc.bc_type == "flow_hydrograph"]
    n_inflows = max(len(inflow_bcs), 1)

    for spec in bc_set.bc_lines:
        if spec.bc_type == "normal_depth":
            parts.append(format_2d_boundary_location(
                bc_set.area_name, spec.name, "normal_depth", slope=spec.slope,
            ))
        elif spec.bc_type == "flow_hydrograph":
            # Split hydrograph equally among inflows
            split_flows = [f / n_inflows for f in flows]
            flow_lines = []
            for i in range(0, n_points, 10):
                chunk = split_flows[i : i + 10]
                flow_lines.append("".join(f"{v:10.2f}" for v in chunk))
            flow_block = "\n".join(flow_lines)

            parts.append(format_2d_boundary_location(
                bc_set.area_name, spec.name, "flow_hydrograph",
                flow_count=n_points, interval=interval,
            ))
            parts.append(f"{flow_block}\n\n")

    flow_file_path.write_text("".join(parts), encoding="utf-8")
    logger.info("Wrote 2D unsteady flow file: %s (%d BCs)", flow_file_path, len(bc_set.bc_lines))
