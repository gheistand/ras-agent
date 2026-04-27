"""Compare two HEC-RAS geometry HDF meshes and report convergence metrics.

Usage:
    python benchmarks/compare_meshes.py <our_g01.hdf> <reference_g02.hdf> [--area MainArea]

Reads mesh topology directly from HDF and computes:
  - Cell / face / facepoint count differences
  - Faces-per-cell distribution
  - Cell area distribution (via facepoint polygons)
  - Face length distribution
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import h5py
import numpy as np


def _read_mesh(hdf_path: str | Path, area_name: str = "MainArea") -> dict:
    """Extract mesh arrays from a geometry HDF file."""
    prefix = f"Geometry/2D Flow Areas/{area_name}"
    with h5py.File(hdf_path, "r") as f:
        cells_xy = f[f"{prefix}/Cells Center Coordinate"][:]
        cells_face_info = f[f"{prefix}/Cells Face and Orientation Info"][:]
        fp_xy = f[f"{prefix}/FacePoints Coordinate"][:]
        faces_fp_idx = f[f"{prefix}/Faces FacePoint Indexes"][:]
        faces_nuvl = f[f"{prefix}/Faces NormalUnitVector and Length"][:]
        cell_fp_info = f[f"{prefix}/Cells FacePoint Indexes"][:]

    face_lengths = faces_nuvl[:, 2].astype(np.float64)
    n_cells = cells_xy.shape[0]
    faces_per_cell = cells_face_info[:, 1].astype(np.int32)
    cell_areas = _compute_cell_areas(cell_fp_info, fp_xy, n_cells)

    return {
        "cells_xy": cells_xy,
        "n_cells": n_cells,
        "n_faces": faces_fp_idx.shape[0],
        "n_facepoints": fp_xy.shape[0],
        "faces_per_cell": faces_per_cell,
        "face_lengths": face_lengths,
        "cell_areas": cell_areas,
    }


def _compute_cell_areas(cell_fp_info, fp_xy, n_cells) -> np.ndarray:
    """Compute cell areas from facepoint index rings using the shoelace formula."""
    areas = np.zeros(n_cells, dtype=np.float64)
    for i in range(n_cells):
        idx_row = cell_fp_info[i]
        valid = idx_row[idx_row >= 0]
        if len(valid) < 3:
            continue
        pts = fp_xy[valid]
        x = pts[:, 0]
        y = pts[:, 1]
        areas[i] = 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    return areas


def _pct_diff(ours, ref):
    if ref == 0:
        return float("inf")
    return (ours - ref) / ref * 100.0


def _dist_stats(arr: np.ndarray) -> dict:
    return {
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "std": float(np.std(arr)),
        "p5": float(np.percentile(arr, 5)),
        "p95": float(np.percentile(arr, 95)),
        "max": float(np.max(arr)),
        "min": float(np.min(arr)),
    }


def compare(ours_path: str, ref_path: str, area_name: str = "MainArea") -> dict:
    """Run comparison and return metrics dict."""
    ours = _read_mesh(ours_path, area_name)
    ref = _read_mesh(ref_path, area_name)

    result = {
        "ours_cells": ours["n_cells"],
        "ref_cells": ref["n_cells"],
        "cell_diff_pct": _pct_diff(ours["n_cells"], ref["n_cells"]),
        "ours_faces": ours["n_faces"],
        "ref_faces": ref["n_faces"],
        "face_diff_pct": _pct_diff(ours["n_faces"], ref["n_faces"]),
        "ours_facepoints": ours["n_facepoints"],
        "ref_facepoints": ref["n_facepoints"],
        "fp_diff_pct": _pct_diff(ours["n_facepoints"], ref["n_facepoints"]),
    }

    for label, mesh in [("ours", ours), ("ref", ref)]:
        fpc = mesh["faces_per_cell"]
        result[f"{label}_fpc_max"] = int(np.max(fpc))
        result[f"{label}_fpc_gt8"] = int(np.sum(fpc > 8))
        result[f"{label}_fpc_stats"] = _dist_stats(fpc)

        result[f"{label}_area_stats"] = _dist_stats(mesh["cell_areas"])
        result[f"{label}_facelen_stats"] = _dist_stats(mesh["face_lengths"])

    ref_areas = ref["cell_areas"]
    ours_areas = ours["cell_areas"]
    n_common = min(len(ref_areas), len(ours_areas))
    if n_common > 100:
        ref_sorted = np.sort(ref_areas)[:n_common]
        ours_sorted = np.sort(ours_areas)[:n_common]
        result["area_dist_corr"] = float(np.corrcoef(ref_sorted, ours_sorted)[0, 1])
    else:
        result["area_dist_corr"] = 0.0

    converged = (
        abs(result["cell_diff_pct"]) < 1.0
        and abs(result["face_diff_pct"]) < 2.0
        and result["ours_fpc_gt8"] == 0
        and result["area_dist_corr"] > 0.95
    )
    result["converged"] = converged
    return result


def _print_report(m: dict):
    print("=" * 65)
    print("  HEC-RAS Mesh Comparison Report")
    print("=" * 65)

    print(f"\n{'Metric':<30} {'Ours':>12} {'Reference':>12} {'Diff %':>10}")
    print("-" * 65)
    print(f"{'Cells':<30} {m['ours_cells']:>12,} {m['ref_cells']:>12,} {m['cell_diff_pct']:>+9.2f}%")
    print(f"{'Faces':<30} {m['ours_faces']:>12,} {m['ref_faces']:>12,} {m['face_diff_pct']:>+9.2f}%")
    print(f"{'FacePoints':<30} {m['ours_facepoints']:>12,} {m['ref_facepoints']:>12,} {m['fp_diff_pct']:>+9.2f}%")

    print(f"\n{'Faces-per-cell':<30} {'Ours':>12} {'Reference':>12}")
    print("-" * 65)
    print(f"{'  Max':<30} {m['ours_fpc_max']:>12} {m['ref_fpc_max']:>12}")
    print(f"{'  Cells > 8 faces':<30} {m['ours_fpc_gt8']:>12} {m['ref_fpc_gt8']:>12}")

    for label, tag in [("ours", "Our mesh"), ("ref", "Reference")]:
        s = m[f"{label}_area_stats"]
        print(f"\n  {tag} — Cell area: mean={s['mean']:.1f}  med={s['median']:.1f}  "
              f"std={s['std']:.1f}  [p5={s['p5']:.1f}, p95={s['p95']:.1f}]")
        s = m[f"{label}_facelen_stats"]
        print(f"  {tag} — Face length: mean={s['mean']:.1f}  med={s['median']:.1f}  "
              f"std={s['std']:.1f}  [p5={s['p5']:.1f}, p95={s['p95']:.1f}]")

    print(f"\n  Area distribution correlation: {m['area_dist_corr']:.4f}")

    print("\n" + "=" * 65)
    if m["converged"]:
        print("  CONVERGED — mesh matches reference within tolerance")
    else:
        reasons = []
        if abs(m["cell_diff_pct"]) >= 1.0:
            reasons.append(f"cell count diff {m['cell_diff_pct']:+.2f}% (need <1%)")
        if abs(m["face_diff_pct"]) >= 2.0:
            reasons.append(f"face count diff {m['face_diff_pct']:+.2f}% (need <2%)")
        if m["ours_fpc_gt8"] > 0:
            reasons.append(f"{m['ours_fpc_gt8']} cells with >8 faces")
        if m["area_dist_corr"] <= 0.95:
            reasons.append(f"area corr {m['area_dist_corr']:.4f} (need >0.95)")
        print("  NOT CONVERGED:")
        for r in reasons:
            print(f"    - {r}")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="Compare two HEC-RAS mesh HDF files")
    parser.add_argument("ours", help="Path to our generated .g01.hdf")
    parser.add_argument("reference", help="Path to reference .g02.hdf")
    parser.add_argument("--area", default="MainArea", help="2D flow area name")
    args = parser.parse_args()

    m = compare(args.ours, args.reference, args.area)
    _print_report(m)

    sys.exit(0 if m["converged"] else 1)


if __name__ == "__main__":
    main()
