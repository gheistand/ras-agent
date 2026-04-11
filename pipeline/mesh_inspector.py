"""
mesh_inspector.py — HEC-RAS 2D mesh geometry validation

Pre-flight check for HEC-RAS geometry HDF files before model build or
template use. Validates mesh cells against HEC-RAS's five geometric rules:

  Rule 1: Each cell has 3–8 faces
  Rule 2: Each cell is strictly convex
  Rule 3: No two adjacent faces are collinear
  Rule 4: No duplicate cell centres or facepoints
  Rule 5: Every cell centre lies inside the 2D flow area boundary

Logic adapted from rivia (https://github.com/gyanz/rivia), Apache 2.0.
Vendored inline to avoid a hard dependency on the rivia package.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class MeshAreaReport:
    """Validation results for a single 2D flow area.

    Attributes:
        area_name:           Name of the 2D flow area.
        n_cells:             Number of cells checked.
        n_facepoints:        Number of facepoints in the area.
        n_violations:        Total number of rule violations found.
        violations_by_rule:  Mapping of rule label to violation count.
        bad_cell_ids:        Cell indices with any violation.
        passed:              True if n_violations == 0.
    """
    area_name: str
    n_cells: int
    n_facepoints: int
    n_violations: int
    violations_by_rule: dict
    bad_cell_ids: list
    passed: bool


@dataclass
class MeshInspectionResult:
    """Top-level result from inspect_geometry_hdf.

    Attributes:
        hdf_path:          Path to the inspected geometry HDF file.
        areas:             Per-area validation reports.
        total_violations:  Sum of violations across all areas.
        passed:            True if ALL areas passed.
        elapsed_sec:       Wall-clock time for the inspection.
        notes:             Warnings, skipped areas, or informational messages.
    """
    hdf_path: Path
    areas: list
    total_violations: int
    passed: bool
    elapsed_sec: float
    notes: list


# ── Validation logic vendored from rivia (Apache 2.0) ────────────────────────
# Source: https://github.com/gyanz/rivia/blob/main/src/rivia/geo/mesh_validation.py

from collections import defaultdict

# (rivia uses its own logger; we route through mesh_inspector's logger below)
_rivia_logger = logging.getLogger(__name__)


def _reconstruct_polygon(
    cell_idx: int,
    cell_face_info: np.ndarray,
    cell_face_values: np.ndarray,
    face_facepoint_indexes: np.ndarray,
    facepoint_coordinates: np.ndarray,
    face_perimeter_info: Optional[np.ndarray] = None,
    face_perimeter_values: Optional[np.ndarray] = None,
) -> Optional[tuple]:
    """Return ``(polygon_xy, ordered_fp_indices)`` for *cell_idx*.

    Recover the ordered polygon for a single mesh cell by walking the
    facepoint adjacency graph.  Each face contributes its two corner
    facepoint endpoints plus any interior perimeter points along curved
    faces; because every corner facepoint in a valid cell belongs to
    exactly two faces, the edges form a single closed cycle.  The function
    follows that cycle to produce vertices in polygon order, independent of
    the orientation flags stored in the HDF file.

    When *face_perimeter_info* and *face_perimeter_values* are supplied
    (from ``Faces Perimeter Info`` and ``Faces Perimeter Values``), interior
    points along curved faces are inserted between the corner facepoints in
    the correct traversal direction.  This is needed for correct convexity
    checking of cells that have curved face edges.

    Returns ``None`` for malformed cells where the graph cannot be
    traversed as a closed cycle.

    Args:
        cell_idx:               0-based cell index.
        cell_face_info:         ``[start_index, count]`` array into cell_face_values.
        cell_face_values:       ``[face_index, orientation]`` array.
        face_facepoint_indexes: ``[fp0, fp1]`` per face.
        facepoint_coordinates:  ``(m, 2)`` x/y coordinates of facepoints.
        face_perimeter_info:    Optional ``[start, count]`` into face_perimeter_values.
        face_perimeter_values:  Optional interior perimeter point coordinates.

    Returns:
        ``(polygon_xy, fp_order)`` tuple, or None if the cell is malformed.
    """
    start = int(cell_face_info[cell_idx, 0])
    count = int(cell_face_info[cell_idx, 1])
    face_idxs = cell_face_values[start : start + count, 0]

    # Build facepoint adjacency graph and edge→face lookup.
    # Each corner facepoint appears in exactly two faces.
    adj: dict = defaultdict(list)
    edge_to_face: dict = {}
    for f in face_idxs:
        f = int(f)
        fp0, fp1 = int(face_facepoint_indexes[f, 0]), int(face_facepoint_indexes[f, 1])
        adj[fp0].append(fp1)
        adj[fp1].append(fp0)
        edge_to_face[(fp0, fp1)] = f
        edge_to_face[(fp1, fp0)] = f

    # Validate: every node must have exactly 2 neighbours.
    if any(len(v) != 2 for v in adj.values()):
        return None

    use_perimeter = (
        face_perimeter_info is not None and face_perimeter_values is not None
    )

    # Follow the cycle, inserting interior perimeter points for curved faces.
    fp_order: list = []
    all_coords: list = []
    all_fps = list(adj.keys())
    current = all_fps[0]
    prev: Optional[int] = None
    for _ in range(count):
        fp_order.append(current)
        all_coords.append(facepoint_coordinates[current])
        nb = adj[current]
        nxt = nb[1] if nb[0] == prev else nb[0]

        # Insert interior perimeter points between current and nxt.
        if use_perimeter:
            face_idx = edge_to_face.get((current, nxt))
            if face_idx is not None:
                peri_start = int(face_perimeter_info[face_idx, 0])
                n_interior = int(face_perimeter_info[face_idx, 1])
                if n_interior > 0:
                    interior = face_perimeter_values[
                        peri_start : peri_start + n_interior
                    ]
                    # Perimeter points are stored fp0→fp1 in canonical HDF order.
                    # Reverse if we are traversing fp1→fp0.
                    fp0_canonical = int(face_facepoint_indexes[face_idx, 0])
                    if current != fp0_canonical:
                        interior = interior[::-1]
                    all_coords.extend(interior)

        prev = current
        current = nxt

    if current != fp_order[0]:          # cycle did not close
        return None

    return np.array(all_coords), fp_order


def _cross_products(polygon: np.ndarray) -> np.ndarray:
    """Signed cross product at every vertex of *polygon* (n, 2).

    Positive → left turn (CCW), negative → right turn (CW), zero → collinear.

    Example
    -------
    A unit square traversed counter-clockwise produces all-positive cross
    products (all left turns). The turn at vertex ``i`` is the signed rotation
    from incoming edge ``(i-1 -> i)`` to outgoing edge ``(i -> i+1)`` with
    wraparound. For ``i=0``, incoming is ``(n-1 -> 0)`` and outgoing is
    ``(0 -> 1)``::

        >>> sq = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
        >>> _cross_products(sq)
        array([1., 1., 1., 1.])

    A collinear triplet yields a zero at the middle vertex::

        >>> tri = np.array([[0, 0], [1, 0], [2, 0], [2, 1], [0, 1]], dtype=float)
        >>> _cross_products(tri)   # vertex 1 is collinear
        array([ 1.,  0.,  2.,  2.,  1.])

    Args:
        polygon: ``(n, 2)`` array of vertex coordinates.

    Returns:
        ``(n,)`` array of signed cross products.
    """
    n = len(polygon)
    v1 = polygon - np.roll(polygon, 1, axis=0)  # edge arriving at vertex i
    v2 = np.roll(polygon, -1, axis=0) - polygon  # edge leaving vertex i
    return v1[:, 0] * v2[:, 1] - v1[:, 1] * v2[:, 0]


def _point_in_polygon(point: np.ndarray, polygon: np.ndarray) -> bool:
    """Ray-casting point-in-polygon test.  Works for any simple polygon.

    Example
    -------
    ::

        >>> sq = np.array([[0, 0], [4, 0], [4, 4], [0, 4]], dtype=float)
        >>> _point_in_polygon(np.array([2.0, 2.0]), sq)   # inside
        True
        >>> _point_in_polygon(np.array([5.0, 5.0]), sq)   # outside
        False

    Args:
        point:   ``(2,)`` x/y coordinate to test.
        polygon: ``(n, 2)`` array of polygon vertices.

    Returns:
        True if the point is inside the polygon.
    """
    x, y = float(point[0]), float(point[1])
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(polygon[i, 0]), float(polygon[i, 1])
        xj, yj = float(polygon[j, 0]), float(polygon[j, 1])
        if ((yi > y) != (yj > y)) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _find_exact_duplicates(
    coords: np.ndarray,
) -> list:
    """Return pairs ``(i, j)`` where ``coords[i] == coords[j]`` exactly.

    Example
    -------
    Given::

        coords = np.array([
            [0.0, 0.0],  # idx 0
            [1.0, 2.0],  # idx 1
            [3.0, 4.0],  # idx 2
            [1.0, 2.0],  # idx 3 (duplicate of idx 1)
            [0.0, 0.0],  # idx 4 (duplicate of idx 0)
        ])

    the function returns pairs equivalent to ``[(0, 4), (1, 3)]``
    (ordering may vary). Comparisons are exact, not tolerance-based.

    Args:
        coords: ``(n, 2)`` coordinate array.

    Returns:
        List of ``(i, j)`` index pairs for duplicate rows.
    """
    n = len(coords)
    order = np.lexsort(coords[:, ::-1].T)
    sorted_c = coords[order]
    pairs: list = []
    for k in range(n - 1):
        if np.array_equal(sorted_c[k], sorted_c[k + 1]):
            pairs.append((int(order[k]), int(order[k + 1])))
    return pairs


def check_mesh_cells(
    cell_centers: np.ndarray,
    facepoint_coordinates: np.ndarray,
    face_facepoint_indexes: np.ndarray,
    cell_face_info: np.ndarray,
    cell_face_values: np.ndarray,
    *,
    face_perimeter_info: Optional[np.ndarray] = None,
    face_perimeter_values: Optional[np.ndarray] = None,
    boundary_polygon: Optional[np.ndarray] = None,
    tol: float = 1e-10,
) -> dict:
    """Check HEC-RAS 2D mesh cells against geometric validity rules.

    Parameters
    ----------
    cell_centers : ndarray, shape ``(n_cells, 2)``
        Cell-centre x, y coordinates.
    facepoint_coordinates : ndarray, shape ``(n_facepoints, 2)``
        Mesh vertex x, y coordinates.
    face_facepoint_indexes : ndarray, shape ``(n_faces, 2)``
        Start/end facepoint index for each face.
    face_perimeter_info : ndarray, shape ``(n_faces, 2)``, optional
        ``[start_index, count]`` into *face_perimeter_values* for interior
        perimeter points on curved faces (``Faces Perimeter Info`` in HDF).
        When supplied together with *face_perimeter_values*, interior points
        are included in the polygon used for convexity checking.
    face_perimeter_values : ndarray, shape ``(total, 2)``, optional
        x, y coordinates of interior perimeter points along curved faces
        (``Faces Perimeter Values`` in HDF).
    cell_face_info : ndarray, shape ``(>= n_cells, 2)``
        ``[start_index, count]`` into *cell_face_values* for each cell.
    cell_face_values : ndarray, shape ``(total, 2)``
        ``[face_index, orientation]`` for each cell-face association.
    boundary_polygon : ndarray, shape ``(n_vertices, 2)``, optional
        Ordered vertices of the 2D flow area outer boundary.  When supplied,
        each cell centre is also tested against this perimeter (rule 5).
        Omitting it skips the boundary test.
    tol : float
        Absolute tolerance used only for near-collinear edge detection
        (``|cross_product| < tol * edge_length²``).  Exact-duplicate checks
        use bitwise equality.

    Returns
    -------
    dict with keys:

    ``n_cells``, ``n_facepoints``
        Dataset sizes.
    ``duplicate_cell_centers`` : list of ``(i, j)`` index pairs
        Cell centre pairs that share identical coordinates (rule 4).
    ``duplicate_facepoints`` : list of ``(i, j)`` index pairs
        Facepoint pairs that share identical coordinates (rule 4).
    ``cells`` : list of per-cell dicts
        One entry per cell.  Keys:

        - ``cell_idx`` — 0-based cell index.
        - ``n_faces`` — number of bounding faces.
        - ``face_count_ok`` — ``3 <= n_faces <= 8`` (rule 1).
        - ``strictly_convex`` — all cross products share the same sign and are
          all non-zero (rules 2 & 3).
        - ``collinear_vertex_indices`` — vertex positions in the ordered
          polygon where the cross product is ~zero (rule 3).
        - ``reflex_vertex_indices`` — vertex positions where the polygon turns
          the wrong way (breaks strict convexity, rule 2).
        - ``center_inside_polygon`` — cell centre is inside its own polygon.
        - ``center_inside_boundary`` — cell centre is inside *boundary_polygon*
          (only present when *boundary_polygon* is supplied; rule 5).
        - ``malformed`` — ``True`` when the face adjacency graph could not be
          traversed as a closed cycle.
    ``summary`` : dict
        Aggregate violation counts.
    """
    cell_centers = np.asarray(cell_centers, dtype=np.float64)
    facepoint_coordinates = np.asarray(facepoint_coordinates, dtype=np.float64)
    face_facepoint_indexes = np.asarray(face_facepoint_indexes, dtype=np.int64)
    cell_face_info = np.asarray(cell_face_info, dtype=np.int64)
    cell_face_values = np.asarray(cell_face_values, dtype=np.int64)

    n_cells = len(cell_centers)
    n_facepoints = len(facepoint_coordinates)

    # ── Global: duplicate point checks (rule 4) ────────────────────────────
    dup_cc = _find_exact_duplicates(cell_centers)
    dup_fp = _find_exact_duplicates(facepoint_coordinates)

    # ── Per-cell checks ────────────────────────────────────────────────────
    cell_results: list = []

    n_face_count_bad = 0
    n_non_convex = 0
    n_collinear = 0
    n_center_outside_polygon = 0
    n_center_outside_boundary = 0
    n_malformed = 0

    for c in range(n_cells):
        count = int(cell_face_info[c, 1])
        result: dict = {"cell_idx": c, "n_faces": count}

        # Rule 1: face count
        face_count_ok = 3 <= count <= 8
        result["face_count_ok"] = face_count_ok
        if not face_count_ok:
            n_face_count_bad += 1

        # Reconstruct ordered polygon (includes curved-face interior points
        # when face_perimeter_info/values are provided).
        poly_result = _reconstruct_polygon(
            c, cell_face_info, cell_face_values,
            face_facepoint_indexes, facepoint_coordinates,
            face_perimeter_info, face_perimeter_values,
        )

        if poly_result is None:
            result["malformed"] = True
            result["strictly_convex"] = False
            result["collinear_vertex_indices"] = []
            result["reflex_vertex_indices"] = []
            result["center_inside_polygon"] = False
            if boundary_polygon is not None:
                result["center_inside_boundary"] = False
            n_malformed += 1
            cell_results.append(result)
            continue

        result["malformed"] = False
        polygon, _ = poly_result

        # Rules 2 & 3: strict convexity and collinearity
        cross = _cross_products(polygon)

        # Normalize |cross| by local edge size before comparing with tol.
        arriving = polygon - np.roll(polygon, 1, axis=0)
        edge_sq = np.einsum("ij,ij->i", arriving, arriving)
        near_zero = edge_sq > 0
        normalised_cross = np.where(near_zero, np.abs(cross) / np.maximum(edge_sq, 1e-300), 0.0)
        is_collinear = normalised_cross < tol

        collinear_verts = [int(i) for i, c_ in enumerate(is_collinear) if c_]
        strictly_convex_signs = cross[~is_collinear]
        if len(strictly_convex_signs) == 0:
            # All edges are collinear — degenerate polygon
            strictly_convex = False
            reflex_verts = []
        else:
            dominant_sign = np.sign(strictly_convex_signs.mean())
            reflex_mask = (np.sign(cross) != dominant_sign) & ~is_collinear
            reflex_verts = [int(i) for i, r in enumerate(reflex_mask) if r]
            strictly_convex = len(collinear_verts) == 0 and len(reflex_verts) == 0

        result["strictly_convex"] = strictly_convex
        result["collinear_vertex_indices"] = collinear_verts
        result["reflex_vertex_indices"] = reflex_verts

        if not strictly_convex:
            n_non_convex += 1
        if collinear_verts:
            n_collinear += 1

        # Rule 5a: cell centre inside its own polygon
        centre = cell_centers[c]
        inside_poly = _point_in_polygon(centre, polygon)
        result["center_inside_polygon"] = inside_poly
        if not inside_poly:
            n_center_outside_polygon += 1

        # Rule 5b: cell centre inside the 2D area boundary (optional)
        if boundary_polygon is not None:
            inside_bnd = _point_in_polygon(centre, np.asarray(boundary_polygon))
            result["center_inside_boundary"] = inside_bnd
            if not inside_bnd:
                n_center_outside_boundary += 1

        cell_results.append(result)

    # ── Summary ────────────────────────────────────────────────────────────
    summary: dict = {
        "n_face_count_violations": n_face_count_bad,
        "n_non_convex": n_non_convex,
        "n_collinear_edges": n_collinear,
        "n_center_outside_polygon": n_center_outside_polygon,
        "n_duplicate_cell_centers": len(dup_cc),
        "n_duplicate_facepoints": len(dup_fp),
        "n_malformed": n_malformed,
    }
    if boundary_polygon is not None:
        summary["n_center_outside_boundary"] = n_center_outside_boundary

    summary["n_total_violations"] = sum(
        v for k, v in summary.items() if k != "n_total_violations"
    )

    return {
        "n_cells": n_cells,
        "n_facepoints": n_facepoints,
        "duplicate_cell_centers": dup_cc,
        "duplicate_facepoints": dup_fp,
        "cells": cell_results,
        "summary": summary,
    }


def _cell_has_violation(r: dict) -> bool:
    """Return ``True`` if the per-cell result dict contains any validity violation.

    Example
    -------
    ::

        >>> good = {
        ...     'face_count_ok': True, 'malformed': False,
        ...     'strictly_convex': True, 'center_inside_polygon': True,
        ... }
        >>> _cell_has_violation(good)
        False
        >>> _cell_has_violation({**good, 'strictly_convex': False})
        True

    Args:
        r: Per-cell result dict from ``check_mesh_cells``.

    Returns:
        True if any rule is violated.
    """
    return (
        not r["face_count_ok"]
        or r.get("malformed", False)
        or not r["strictly_convex"]
        or not r["center_inside_polygon"]
        or not r.get("center_inside_boundary", True)
    )

# ── End vendored section ──────────────────────────────────────────────────────


# ── Public API ────────────────────────────────────────────────────────────────

def inspect_geometry_hdf(
    hdf_path: "Path | str",
    *,
    max_cells_per_area: Optional[int] = None,
    tol: float = 1e-10,
    verbose: bool = False,
) -> MeshInspectionResult:
    """Open a HEC-RAS geometry HDF file and validate all 2D flow area meshes.

    Args:
        hdf_path:            Path to .g01.hdf or similar geometry HDF file.
        max_cells_per_area:  If set, validate only the first N cells per area
                             (useful for fast preflight on large models).
        tol:                 Collinearity tolerance (see check_mesh_cells).
        verbose:             If True, log per-area summaries at INFO level.

    Returns:
        MeshInspectionResult summarising all areas.

    Raises:
        FileNotFoundError: If the HDF file does not exist.
    """
    try:
        import h5py
    except ImportError:
        logger.warning("h5py not available; mesh inspection skipped")
        return MeshInspectionResult(
            hdf_path=Path(hdf_path),
            areas=[],
            total_violations=0,
            passed=True,
            elapsed_sec=0.0,
            notes=["h5py not installed; inspection skipped"],
        )

    hdf_path = Path(hdf_path)
    if not hdf_path.exists():
        raise FileNotFoundError(f"Geometry HDF not found: {hdf_path}")

    t0 = time.monotonic()
    areas: list = []
    notes: list = []

    with h5py.File(hdf_path, "r") as hf:
        areas_group = hf.get("Geometry/2D Flow Areas")
        if areas_group is None:
            notes.append("No 2D flow areas found in HDF; not a 2D geometry file")
            return MeshInspectionResult(
                hdf_path=hdf_path,
                areas=[],
                total_violations=0,
                passed=True,
                elapsed_sec=time.monotonic() - t0,
                notes=notes,
            )

        for area_name in areas_group.keys():
            area = areas_group[area_name]

            # Required datasets
            try:
                cell_centers = area["Cells Center Coordinate"][:]
                facepoint_coords = area["FacePoints Coordinate"][:]
                face_fp_indexes = area["Faces FacePoint Indexes"][:]
                cell_face_info = area["Cells Face and Orientation Info"][:]
                cell_face_values = area["Cells Face and Orientation Values"][:]
            except KeyError as exc:
                notes.append(
                    f"Area '{area_name}': skipped — missing dataset {exc}"
                )
                continue

            # Optional curved face perimeter points
            face_peri_info_ds = area.get("Faces Perimeter Info")
            face_peri_vals_ds = area.get("Faces Perimeter Values")
            face_peri_info = face_peri_info_ds[:] if face_peri_info_ds is not None else None
            face_peri_vals = face_peri_vals_ds[:] if face_peri_vals_ds is not None else None

            # Optional boundary polygon
            perimeter_ds = area.get("Perimeter")
            boundary_polygon = perimeter_ds[:] if perimeter_ds is not None else None

            # Slice arrays if max_cells_per_area is set
            if max_cells_per_area is not None:
                n = min(max_cells_per_area, len(cell_centers))
                cell_centers = cell_centers[:n]
                cell_face_info = cell_face_info[:n]

            report = check_mesh_cells(
                cell_centers=cell_centers,
                facepoint_coordinates=facepoint_coords,
                face_facepoint_indexes=face_fp_indexes,
                cell_face_info=cell_face_info,
                cell_face_values=cell_face_values,
                face_perimeter_info=face_peri_info,
                face_perimeter_values=face_peri_vals,
                boundary_polygon=boundary_polygon,
                tol=tol,
            )

            s = report["summary"]
            n_violations = s["n_total_violations"]
            bad_cell_ids = [
                r["cell_idx"] for r in report["cells"] if _cell_has_violation(r)
            ]

            violations_by_rule = {
                "rule_1_face_count":   s["n_face_count_violations"],
                "rule_2_convexity":    s["n_non_convex"],
                "rule_3_collinear":    s["n_collinear_edges"],
                "rule_4_dup_centers":  s["n_duplicate_cell_centers"],
                "rule_4_dup_fpts":     s["n_duplicate_facepoints"],
                "rule_5_center_poly":  s["n_center_outside_polygon"],
                "malformed":           s["n_malformed"],
            }
            if "n_center_outside_boundary" in s:
                violations_by_rule["rule_5_center_boundary"] = s["n_center_outside_boundary"]

            area_report = MeshAreaReport(
                area_name=area_name,
                n_cells=report["n_cells"],
                n_facepoints=report["n_facepoints"],
                n_violations=n_violations,
                violations_by_rule=violations_by_rule,
                bad_cell_ids=bad_cell_ids,
                passed=(n_violations == 0),
            )
            areas.append(area_report)

            if verbose:
                status = "PASSED" if area_report.passed else f"FAILED ({n_violations} violations)"
                logger.info(
                    "Mesh area '%s': %d cells, %d facepoints — %s",
                    area_name, area_report.n_cells, area_report.n_facepoints, status,
                )

    total_violations = sum(a.n_violations for a in areas)
    passed = all(a.passed for a in areas)
    elapsed = time.monotonic() - t0

    return MeshInspectionResult(
        hdf_path=hdf_path,
        areas=areas,
        total_violations=total_violations,
        passed=passed,
        elapsed_sec=elapsed,
        notes=notes,
    )


def preflight_template(
    geom_hdf_path: "Path | str",
    *,
    max_cells: int = 500,
    raise_on_fail: bool = False,
) -> MeshInspectionResult:
    """Fast pre-flight check for a template geometry HDF.

    Checks only the first `max_cells` cells per area to keep latency low.
    Logs a WARNING if violations are found.
    If raise_on_fail=True, raises RuntimeError on any violation.

    Args:
        geom_hdf_path:  Path to .g01.hdf geometry HDF file.
        max_cells:      Maximum cells to check per 2D flow area.
        raise_on_fail:  If True, raise RuntimeError when violations are found.

    Returns:
        MeshInspectionResult.

    Raises:
        RuntimeError: If raise_on_fail=True and violations are found.
        FileNotFoundError: If the HDF file does not exist.
    """
    result = inspect_geometry_hdf(
        geom_hdf_path,
        max_cells_per_area=max_cells,
        verbose=False,
    )

    if not result.passed:
        logger.warning(
            "Template mesh pre-flight FAILED: %d violation(s) in %d area(s).\n%s",
            result.total_violations,
            len(result.areas),
            format_report(result),
        )
        if raise_on_fail:
            raise RuntimeError(
                f"Mesh pre-flight failed: {result.total_violations} violation(s). "
                f"See log for details."
            )
    else:
        total_cells = sum(a.n_cells for a in result.areas)
        logger.debug(
            "Template mesh pre-flight PASSED (%d cells sampled, 0 violations).",
            total_cells,
        )

    return result


def format_report(result: MeshInspectionResult) -> str:
    """Return a human-readable multi-line string summarising the result.

    Args:
        result: MeshInspectionResult from inspect_geometry_hdf or preflight_template.

    Returns:
        Formatted report string.
    """
    lines = []
    status = "PASSED" if result.passed else "FAILED"
    lines.append(f"Mesh Inspection Report — {status}")
    lines.append(f"  HDF: {result.hdf_path}")
    lines.append(f"  Elapsed: {result.elapsed_sec:.2f}s")
    lines.append(f"  Total violations: {result.total_violations}")

    if result.notes:
        lines.append("  Notes:")
        for note in result.notes:
            lines.append(f"    • {note}")

    for ar in result.areas:
        area_status = "PASSED" if ar.passed else f"FAILED ({ar.n_violations} violations)"
        lines.append(f"  Area '{ar.area_name}': {ar.n_cells} cells, {ar.n_facepoints} facepoints — {area_status}")
        if not ar.passed:
            for rule, count in ar.violations_by_rule.items():
                if count > 0:
                    lines.append(f"    {rule}: {count}")
            if ar.bad_cell_ids:
                shown = ar.bad_cell_ids[:10]
                lines.append(f"    Bad cell IDs (first {len(shown)}): {shown}")
                if len(ar.bad_cell_ids) > 10:
                    lines.append(f"    ... and {len(ar.bad_cell_ids) - 10} more")

    return "\n".join(lines)


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Validate HEC-RAS geometry HDF mesh cells"
    )
    parser.add_argument("hdf", help="Path to .g01.hdf geometry file")
    parser.add_argument(
        "--max-cells", type=int, default=None,
        help="Limit cells checked per area (default: all)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    result = inspect_geometry_hdf(
        args.hdf, max_cells_per_area=args.max_cells, verbose=args.verbose
    )
    print(format_report(result))
    sys.exit(0 if result.passed else 1)
