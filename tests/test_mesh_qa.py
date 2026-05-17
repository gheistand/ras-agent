"""Tests for pipeline/mesh_qa.py."""

import json
import os
import sys
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import mesh_qa


def _write_sample_geom(path, *, points=4):
    point_lines = {
        0: "",
        4: "  2,2 8,2\n  2,8 8,8\n",
        5: "  2,2 8,2\n  2,8 8,8\n  5,5\n",
    }[points]
    path.write_text(
        "Geom Title=QA Test\n"
        "Program Version=6.60\n"
        "Storage Area=MainArea,5,5\n"
        "Storage Area Surface Line= 5\n"
        "  0,0\n"
        "  10,0\n"
        "  10,10\n"
        "  0,10\n"
        "  0,0\n"
        "Storage Area Type=0\n"
        "Storage Area Area=\n"
        "Storage Area Min Elev=\n"
        "Storage Area Is2D=-1\n"
        "Storage Area Point Generation Data=,,10,10\n"
        f"Storage Area 2D Points= {points}\n"
        f"{point_lines}"
        "Storage Area 2D PointsPerimeterTime=01Jan2026 0000\n"
        "BreakLine Name=Stream1\n"
        "BreakLine CellSize Min=5\n"
        "BreakLine CellSize Max=10\n"
        "BreakLine Near Repeats=1\n"
        "BreakLine Protection Radius=0\n"
        "BreakLine Polyline= 2\n"
        "  5,0 5,10\n",
        encoding="utf-8",
    )


def test_mesh_qa_package_writes_metrics_and_flags_for_missing_hdf(tmp_path):
    geom_file = tmp_path / "test.g01"
    _write_sample_geom(geom_file, points=4)

    result = mesh_qa.build_mesh_qa_package(
        geom_file,
        output_dir=tmp_path / "qa",
        area_name="MainArea",
        target_cell_size_m=10.0,
        mesh_result=SimpleNamespace(ok=True, status="ok", cell_count=4, face_count=8),
    )

    artifacts = result["artifacts"]
    assert result["metrics"]["proposed"]["mesh_point_count"] == 4
    assert result["metrics"]["proposed"]["breaklines"]["count"] == 1
    assert "regenerated_hdf_missing" in {flag["id"] for flag in result["flags"]}
    assert os.path.exists(artifacts["metrics_json"])
    assert os.path.exists(artifacts["summary_csv"])
    assert os.path.exists(artifacts["flags_csv"])

    metrics = json.loads(open(artifacts["metrics_json"], encoding="utf-8").read())
    assert metrics["schema_version"] == "ras-agent-mesh-qa/v1"
    assert metrics["status"] == "review_flags"


def test_mesh_qa_compares_mocked_regenerated_hdf(tmp_path, monkeypatch):
    geom_file = tmp_path / "test.g01"
    _write_sample_geom(geom_file, points=4)

    def fake_read_hdf_mesh(hdf_path, *, area_name, target_cell_size_m, breakline_geometries):
        return mesh_qa.HdfMeshReadback(
            metrics={
                "available": True,
                "status": "read",
                "hdf_path": str(hdf_path),
                "area_name": area_name,
                "cell_count": 5,
                "face_count": 9,
                "cell_quality": {"available": True, "sliver_count": 1},
                "short_face_count": 0,
                "max_faces_per_cell_exceeded_count": 0,
                "breakline_adherence": {
                    "available": True,
                    "line_samples_farther_than_target_cell": 0,
                    "line_sample_count": 10,
                },
            },
            cell_points=np.asarray([[2, 2], [8, 2], [2, 8], [8, 8], [5, 5]], dtype=float),
            cell_quality_rows=[
                {
                    "mesh_name": "MainArea",
                    "cell_id": 4,
                    "area_m2": 0.1,
                    "perimeter_m": 10.0,
                    "equivalent_cell_size_m": 0.316,
                    "compactness": 0.01,
                    "is_valid": True,
                    "sliver_flag": True,
                }
            ],
        )

    monkeypatch.setattr(mesh_qa, "_read_hdf_mesh", fake_read_hdf_mesh)

    result = mesh_qa.build_mesh_qa_package(
        geom_file,
        output_dir=tmp_path / "qa",
        area_name="MainArea",
        target_cell_size_m=10.0,
        regenerated_hdf_path=tmp_path / "test.g01.hdf",
        mesh_result=SimpleNamespace(ok=True, status="ok", cell_count=4, face_count=8),
    )

    comparison = result["metrics"]["comparison"]
    assert comparison["proposed_vs_regenerated_cell_count"]["delta"] == 1.0
    assert comparison["geommesh_result_vs_regenerated_face_count"]["delta"] == 1.0
    assert "sliver_cells_detected" in {flag["id"] for flag in result["flags"]}
    assert os.path.exists(result["artifacts"]["cell_quality_csv"])
