"""
test_mesh_inspector.py — Tests for pipeline/mesh_inspector.py

Tests cover the vendored validation logic directly (no real HDF files needed
for the pure-geometry tests) plus HDF I/O using synthetic h5py fixtures.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

from mesh_inspector import (
    MeshInspectionResult,
    _cell_has_violation,
    _cross_products,
    _find_exact_duplicates,
    _point_in_polygon,
    check_mesh_cells,
    format_report,
    inspect_geometry_hdf,
    preflight_template,
)


# ── Helpers: minimal toy mesh fixtures ───────────────────────────────────────

def _make_square_mesh():
    """
    A single convex square cell.

    Facepoints (counter-clockwise):
      0: (0,0)   1: (1,0)   2: (1,1)   3: (0,1)

    Faces:
      face 0: fp 0 → 1  (bottom)
      face 1: fp 1 → 2  (right)
      face 2: fp 2 → 3  (top)
      face 3: fp 3 → 0  (left)

    Cell 0: uses faces [0, 1, 2, 3], centre (0.5, 0.5)
    """
    facepoint_coordinates = np.array(
        [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    )
    face_facepoint_indexes = np.array([[0, 1], [1, 2], [2, 3], [3, 0]])
    cell_centers = np.array([[0.5, 0.5]])
    # cell_face_info: [start_idx_in_values, count]
    cell_face_info = np.array([[0, 4]])
    # cell_face_values: [face_idx, orientation]
    cell_face_values = np.array([[0, 1], [1, 1], [2, 1], [3, 1]])
    return dict(
        cell_centers=cell_centers,
        facepoint_coordinates=facepoint_coordinates,
        face_facepoint_indexes=face_facepoint_indexes,
        cell_face_info=cell_face_info,
        cell_face_values=cell_face_values,
    )


def _make_two_cell_mesh():
    """
    Two adjacent square cells sharing one face.

    Facepoints:
      0: (0,0)  1: (1,0)  2: (2,0)
      3: (2,1)  4: (1,1)  5: (0,1)

    Faces:
      0: (0,1) bottom-left cell bottom
      1: (1,4) shared vertical face
      2: (4,5) top-left cell top
      3: (5,0) left-left cell left
      4: (1,2) bottom-right cell bottom
      5: (2,3) right-right cell right
      6: (3,4) top-right cell top

    Cell 0: faces [0,1,2,3], centre (0.5, 0.5)
    Cell 1: faces [4,5,6,1], centre (1.5, 0.5)
    """
    facepoint_coordinates = np.array([
        [0.0, 0.0], [1.0, 0.0], [2.0, 0.0],
        [2.0, 1.0], [1.0, 1.0], [0.0, 1.0],
    ])
    face_facepoint_indexes = np.array([
        [0, 1], [1, 4], [4, 5], [5, 0],   # faces 0-3
        [1, 2], [2, 3], [3, 4],            # faces 4-6
    ])
    cell_centers = np.array([[0.5, 0.5], [1.5, 0.5]])
    cell_face_info = np.array([[0, 4], [4, 4]])
    cell_face_values = np.array([
        [0, 1], [1, 1], [2, 1], [3, 1],   # cell 0
        [4, 1], [5, 1], [6, 1], [1, 1],   # cell 1
    ])
    return dict(
        cell_centers=cell_centers,
        facepoint_coordinates=facepoint_coordinates,
        face_facepoint_indexes=face_facepoint_indexes,
        cell_face_info=cell_face_info,
        cell_face_values=cell_face_values,
    )


# ── Tests: _cross_products ────────────────────────────────────────────────────

def test_cross_products_square():
    """Unit square CCW gives all-positive cross products (all left turns)."""
    sq = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    cp = _cross_products(sq)
    assert cp.shape == (4,)
    assert np.all(cp > 0), f"Expected all positive, got {cp}"


def test_cross_products_collinear():
    """Collinear triplet yields zero cross product at the middle vertex."""
    # Vertices: (0,0), (1,0) [collinear on x-axis], (2,0), (2,1), (0,1)
    poly = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [2.0, 1.0], [0.0, 1.0]])
    cp = _cross_products(poly)
    assert cp[1] == pytest.approx(0.0), f"Expected 0 at index 1, got {cp[1]}"
    assert cp[0] > 0
    assert cp[2] > 0


# ── Tests: _point_in_polygon ──────────────────────────────────────────────────

def test_point_in_polygon_inside():
    """Centre of unit square is inside the square."""
    sq = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]])
    assert _point_in_polygon(np.array([2.0, 2.0]), sq) is True


def test_point_in_polygon_outside():
    """Point well outside unit square returns False."""
    sq = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]])
    assert _point_in_polygon(np.array([5.0, 5.0]), sq) is False


# ── Tests: _find_exact_duplicates ─────────────────────────────────────────────

def test_find_duplicates_none():
    """Array with all unique rows returns empty list."""
    coords = np.array([[0.0, 0.0], [1.0, 2.0], [3.0, 4.0]])
    assert _find_exact_duplicates(coords) == []


def test_find_duplicates_found():
    """Duplicate rows are detected and returned as index pairs."""
    coords = np.array([
        [0.0, 0.0],   # idx 0
        [1.0, 2.0],   # idx 1
        [3.0, 4.0],   # idx 2
        [1.0, 2.0],   # idx 3 — duplicate of 1
        [0.0, 0.0],   # idx 4 — duplicate of 0
    ])
    pairs = _find_exact_duplicates(coords)
    # Normalise: each pair should be a set for comparison
    pair_sets = {frozenset(p) for p in pairs}
    assert frozenset({0, 4}) in pair_sets
    assert frozenset({1, 3}) in pair_sets
    assert len(pairs) == 2


# ── Tests: check_mesh_cells ───────────────────────────────────────────────────

def test_check_mesh_cells_valid_square():
    """Single valid square cell passes all rules."""
    mesh = _make_square_mesh()
    report = check_mesh_cells(**mesh)
    assert report["n_cells"] == 1
    s = report["summary"]
    assert s["n_total_violations"] == 0
    cell = report["cells"][0]
    assert cell["face_count_ok"] is True
    assert cell["strictly_convex"] is True
    assert cell["center_inside_polygon"] is True
    assert cell.get("malformed", False) is False


def test_check_mesh_cells_valid_two_cells():
    """Two-cell mesh passes all rules."""
    mesh = _make_two_cell_mesh()
    report = check_mesh_cells(**mesh)
    assert report["n_cells"] == 2
    assert report["summary"]["n_total_violations"] == 0
    for cell in report["cells"]:
        assert cell["face_count_ok"] is True
        assert cell["strictly_convex"] is True
        assert cell["center_inside_polygon"] is True


def test_check_mesh_cells_bad_face_count():
    """Cell with 9 faces (> 8) triggers Rule 1 violation."""
    mesh = _make_square_mesh()
    # Override cell_face_info to claim 9 faces for cell 0
    mesh["cell_face_info"] = np.array([[0, 9]])
    # Pad cell_face_values so index arithmetic doesn't error
    extra = np.array([[0, 1]] * 9)
    mesh["cell_face_values"] = extra
    report = check_mesh_cells(**mesh)
    assert report["summary"]["n_face_count_violations"] >= 1
    assert report["cells"][0]["face_count_ok"] is False


def test_check_mesh_cells_nonconvex():
    """Concave quadrilateral flags Rule 2 (non-strictly-convex).

    Quadrilateral: (0,0), (4,0), (4,4), (2,1)
    The vertex at (2,1) creates a reflex angle, making the polygon concave.

    Facepoints: 0:(0,0) 1:(4,0) 2:(4,4) 3:(2,1)
    Faces: 0→(0,1)  1→(1,2)  2→(2,3)  3→(3,0)
    """
    facepoint_coordinates = np.array([
        [0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [2.0, 1.0]
    ])
    face_facepoint_indexes = np.array([[0, 1], [1, 2], [2, 3], [3, 0]])
    # Centre is inside the convex hull; use centroid of actual polygon
    # Centroid of a non-convex quad — approximate centre: (2.5, 1.5)
    cell_centers = np.array([[2.5, 1.5]])
    cell_face_info = np.array([[0, 4]])
    cell_face_values = np.array([[0, 1], [1, 1], [2, 1], [3, 1]])

    report = check_mesh_cells(
        cell_centers=cell_centers,
        facepoint_coordinates=facepoint_coordinates,
        face_facepoint_indexes=face_facepoint_indexes,
        cell_face_info=cell_face_info,
        cell_face_values=cell_face_values,
    )
    cell = report["cells"][0]
    assert cell["strictly_convex"] is False, "Expected non-convex cell to fail Rule 2"
    assert len(cell["reflex_vertex_indices"]) >= 1


# ── Tests: inspect_geometry_hdf ───────────────────────────────────────────────

def test_inspect_geometry_hdf_not_found():
    """FileNotFoundError raised for a missing HDF path."""
    with pytest.raises(FileNotFoundError):
        inspect_geometry_hdf("/nonexistent/path/model.g01.hdf")


def test_inspect_geometry_hdf_no_2d_areas(tmp_path):
    """HDF with no 2D flow areas returns passed=True with an informational note."""
    import h5py

    hdf = tmp_path / "empty.g01.hdf"
    with h5py.File(hdf, "w") as f:
        f.create_group("Geometry")  # no "2D Flow Areas" child

    result = inspect_geometry_hdf(hdf)
    assert result.passed is True
    assert result.total_violations == 0
    assert len(result.areas) == 0
    assert any("2d" in n.lower() or "no 2d" in n.lower() or "flow area" in n.lower()
               for n in result.notes), f"Expected informational note, got: {result.notes}"


# ── Tests: format_report ──────────────────────────────────────────────────────

def test_format_report_passed():
    """format_report returns a string containing 'PASSED' when no violations."""
    result = MeshInspectionResult(
        hdf_path=Path("model.g01.hdf"),
        areas=[],
        total_violations=0,
        passed=True,
        elapsed_sec=0.01,
        notes=[],
    )
    text = format_report(result)
    assert isinstance(text, str)
    assert "PASSED" in text


def test_format_report_failed():
    """format_report returns a string containing 'FAILED' and violation counts."""
    from mesh_inspector import MeshAreaReport

    area = MeshAreaReport(
        area_name="Perimeter 1",
        n_cells=10,
        n_facepoints=20,
        n_violations=3,
        violations_by_rule={"rule_2_convexity": 3},
        bad_cell_ids=[0, 2, 7],
        passed=False,
    )
    result = MeshInspectionResult(
        hdf_path=Path("model.g01.hdf"),
        areas=[area],
        total_violations=3,
        passed=False,
        elapsed_sec=0.05,
        notes=[],
    )
    text = format_report(result)
    assert "FAILED" in text
    assert "3" in text


# ── Tests: _cell_has_violation ────────────────────────────────────────────────

def test_cell_has_violation_clean():
    """Clean cell dict returns False (no violations)."""
    good = {
        "face_count_ok": True,
        "malformed": False,
        "strictly_convex": True,
        "center_inside_polygon": True,
    }
    assert _cell_has_violation(good) is False


def test_cell_has_violation_dirty():
    """Cell with strictly_convex=False returns True."""
    bad = {
        "face_count_ok": True,
        "malformed": False,
        "strictly_convex": False,
        "center_inside_polygon": True,
    }
    assert _cell_has_violation(bad) is True
