"""
Tests for the HEC-RAS pre-run readiness gate.

These tests do not require HEC-RAS. They exercise timestamp detection and
safe blocked-state behavior when regeneration cannot run.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import hecras_readiness as gate


def _write_project(project_dir: Path, base: str = "spring_creek") -> dict[str, Path]:
    project_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "prj": project_dir / f"{base}.prj",
        "rasmap": project_dir / f"{base}.rasmap",
        "geom": project_dir / f"{base}.g01",
        "plan": project_dir / f"{base}.p01",
        "flow": project_dir / f"{base}.u01",
        "plan_hdf": project_dir / f"{base}.p01.hdf",
        "geom_hdf": project_dir / f"{base}.g01.hdf",
        "geompre": project_dir / f"{base}.c01",
        "b_file": project_dir / f"{base}.b01",
        "terrain_hdf": project_dir / "Terrain" / "Terrain.hdf",
    }
    paths["terrain_hdf"].parent.mkdir()

    paths["prj"].write_text(
        "Proj Title=spring_creek\n"
        "Geom File=g01\n"
        "Unsteady File=u01\n"
        "Plan File=p01\n",
        encoding="utf-8",
    )
    paths["rasmap"].write_text(
        "<RASMapper><Version>6.6</Version><Terrains Checked=\"True\">"
        "<Layer Name=\"Terrain\" Type=\"TerrainLayer\" Checked=\"True\" "
        "Filename=\".\\Terrain\\Terrain.hdf\" />"
        "</Terrains></RASMapper>",
        encoding="utf-8",
    )
    paths["geom"].write_text("Geom Title=Geometry\nProgram Version=6.60\n", encoding="utf-8")
    paths["plan"].write_text(
        "Plan Title=T100\n"
        "Program Version=6.60\n"
        "Geom File=g01\n"
        "Flow File=u01\n",
        encoding="utf-8",
    )
    paths["flow"].write_text("Flow Title=T100\n", encoding="utf-8")
    for key in ("plan_hdf", "geom_hdf", "geompre", "b_file", "terrain_hdf"):
        paths[key].write_bytes(b"artifact")
    return paths


def test_detects_stale_geometry_hdf(tmp_path):
    paths = _write_project(tmp_path)
    old_time = 1_700_000_000
    new_time = old_time + 100
    os.utime(paths["geom_hdf"], (old_time, old_time))
    os.utime(paths["geom"], (new_time, new_time))

    report = gate.check_hecras_readiness(
        project_dir=tmp_path,
        plan_hdf=paths["plan_hdf"],
        regenerate=False,
        write_report=False,
    )

    assert report.status == "blocked"
    assert report.artifacts["geometry_hdf"].stale is True
    assert "newer source" in report.artifacts["geometry_hdf"].stale_reason
    assert any("geometry_hdf is stale" in blocker for blocker in report.blockers)


def test_safe_failure_when_regeneration_is_unavailable(tmp_path, monkeypatch):
    paths = _write_project(tmp_path)
    paths["geom_hdf"].unlink()
    paths["plan_hdf"].unlink()

    def fake_init(*args, **kwargs):
        raise FileNotFoundError("HEC-RAS 6.6 installation not found")

    def fake_preprocessor(report, context, max_wait):
        try:
            fake_init()
        except FileNotFoundError as exc:
            report.messages.append(f"blocked by environment: {exc}")

    monkeypatch.setattr(gate, "_run_geometry_preprocessor", fake_preprocessor)

    report = gate.check_hecras_readiness(
        project_dir=tmp_path,
        plan_hdf=paths["plan_hdf"],
        regenerate=True,
        write_report=False,
    )

    assert report.status == "blocked"
    assert report.regeneration_attempted is True
    assert report.regeneration_performed is False
    assert any("geometry_hdf is missing" in blocker for blocker in report.blockers)
    assert any("plan_hdf is missing" in blocker for blocker in report.blockers)
    assert any("blocked by environment" in message for message in report.messages)


def test_successful_preprocessor_promotion_records_report(tmp_path, monkeypatch):
    paths = _write_project(tmp_path)
    paths["plan_hdf"].unlink()
    tmp_hdf = tmp_path / "spring_creek.p01.tmp.hdf"

    def fake_preprocessor(report, context, max_wait):
        tmp_hdf.write_bytes(b"fresh tmp hdf")
        paths["geom_hdf"].write_bytes(b"fresh geometry hdf")
        paths["geompre"].write_bytes(b"fresh c file")
        report.preprocessor = {
            "success": True,
            "compute_message_paths": [str(tmp_path / "spring_creek.p01.comp_msgs.txt")],
            "artifact_paths": [str(paths["geom_hdf"]), str(tmp_hdf)],
            "error": None,
        }
        report.regeneration_performed = True
        gate._promote_tmp_plan_hdf(report, context)

    monkeypatch.setattr(gate, "_run_geometry_preprocessor", fake_preprocessor)

    report_path = tmp_path / "readiness" / "report.json"
    report = gate.check_hecras_readiness(
        project_dir=tmp_path,
        plan_hdf=paths["plan_hdf"],
        regenerate=True,
        write_report=True,
        report_path=report_path,
    )

    assert report.status == "regenerated"
    assert paths["plan_hdf"].read_bytes() == b"fresh tmp hdf"
    assert report.preprocessor["success"] is True
    assert report_path.exists()
    assert "spring_creek.p01.hdf" in report_path.read_text(encoding="utf-8")
