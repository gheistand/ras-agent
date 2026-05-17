"""
Dedicated tests for pipeline/water_source.py.

These tests use synthetic .p##/.u## text fixtures only; no HEC-RAS project or
binary installation is required.
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import water_source as ws


def _plan_text(flow_ext: str = "u01", extra: str = "") -> str:
    return (
        "Plan Title=T100\n"
        "Program Version=6.60\n"
        "Geom File=g01\n"
        f"Flow File={flow_ext}\n"
        "Simulation Date=01JAN2000,0000,02JAN2000,0000\n"
        f"{extra}"
    )


def _external_hydrograph_flow_text(values: str = "0.0 12.5\n20.0 1.0\n") -> str:
    return (
        "Flow Title=T100\n"
        "Program Version=6.60\n"
        "Boundary Location=MainArea,Perimeter 1,1.0\n"
        "Normal Depth=0.001\n"
        "Interval=15MIN\n"
        "Flow Hydrograph= 4\n"
        f"{values}"
        "Stage Hydrograph TW Check=0\n"
    )


def _dry_flow_text() -> str:
    return (
        "Flow Title=T100\n"
        "Program Version=6.60\n"
        "Boundary Location=MainArea,Perimeter 1,1.0\n"
        "Normal Depth=0.001\n"
        "Interval=15MIN\n"
    )


def _make_project(
    tmp_path: Path,
    *,
    plan_text: str | None = None,
    flow_text: str | None = None,
    include_plan: bool = True,
    include_flow: bool = True,
):
    project_dir = tmp_path / "ras_project"
    project_dir.mkdir(parents=True)

    plan_file = project_dir / "ras_project.p01"
    flow_file = project_dir / "ras_project.u01"
    if include_plan:
        plan_file.write_text(plan_text or _plan_text(), encoding="utf-8")
    if include_flow:
        flow_file.write_text(
            flow_text if flow_text is not None else _external_hydrograph_flow_text(),
            encoding="utf-8",
        )

    return SimpleNamespace(
        project_dir=project_dir,
        plan_file=plan_file if include_plan else None,
        flow_file=flow_file if include_flow else None,
        metadata={},
    )


@pytest.mark.parametrize(
    ("raw_mode", "kwargs", "expected"),
    [
        (None, {}, "auto"),
        ("", {}, "auto"),
        ("  ", {"allow_low_detail_screening": True}, "mock_screening"),
        ("AUTO", {"mock": True}, "mock_screening"),
        ("dry", {}, "none"),
        ("no-water", {}, "none"),
        ("rog", {}, "rain_on_grid"),
        ("AORC", {}, "rain_on_grid"),
        ("boundary-hydrograph", {}, "external_hydrograph"),
        ("first-pass", {}, "mock_screening"),
    ],
)
def test_normalize_water_source_mode_edge_cases(raw_mode, kwargs, expected):
    assert ws.normalize_water_source_mode(raw_mode, **kwargs) == expected


def test_normalize_water_source_mode_rejects_unknown_mode():
    with pytest.raises(ValueError, match="Unknown water_source_mode"):
        ws.normalize_water_source_mode("spring_melt")


def test_inspect_hec_ras_water_source_files_reads_synthetic_plan_and_flow(tmp_path):
    project = _make_project(tmp_path)

    evidence = ws.inspect_hec_ras_water_source_files(project)

    assert evidence["diagnostics"] == []
    assert evidence["has_external_hydrograph"] is True
    assert evidence["has_rain_on_grid_marker"] is False

    plan_check = evidence["plan_files"][0]
    assert plan_check["exists"] is True
    assert plan_check["referenced_flow_exists"] is True
    assert plan_check["has_geom_file"] is True
    assert plan_check["has_simulation_date"] is True
    assert plan_check["flow_file"].endswith("ras_project.u01")

    flow_check = evidence["flow_files"][0]
    assert flow_check["boundary_location_count"] == 1
    assert flow_check["normal_depth_count"] == 1
    assert flow_check["flow_hydrograph_count"] == 1
    assert flow_check["flow_hydrograph_points"] == [4]
    assert flow_check["has_positive_flow_hydrograph"] is True

    block = flow_check["flow_hydrograph_blocks"][0]
    assert block["declared_points"] == 4
    assert block["parsed_value_count"] == 4
    assert block["positive_value_count"] == 3
    assert block["max_flow_cfs"] == 20.0
    assert block["valid_positive"] is True


def test_inspect_hec_ras_water_source_files_reports_missing_referenced_flow(tmp_path):
    project = _make_project(
        tmp_path,
        plan_text=_plan_text(flow_ext="u02"),
        include_flow=False,
    )

    evidence = ws.inspect_hec_ras_water_source_files(project)

    assert evidence["has_external_hydrograph"] is False
    assert evidence["flow_files"] == []
    assert evidence["plan_files"][0]["referenced_flow_exists"] is False
    assert any("No generated .u##" in item for item in evidence["diagnostics"])
    assert any("references missing flow file u02" in item for item in evidence["diagnostics"])


def test_validate_project_water_source_infers_external_hydrograph(tmp_path):
    project = _make_project(tmp_path)

    validation = ws.validate_project_water_source(
        project,
        water_source_mode="auto",
        water_source_provenance={
            "source": "generated_duh",
            "hydrograph_path": tmp_path / "hydrograph.csv",
        },
    )

    assert validation["schema_version"] == "ras-agent-water-source/v1"
    assert validation["requested_mode"] == "auto"
    assert validation["mode"] == "external_hydrograph"
    assert validation["contract_status"] == "valid"
    assert validation["production_ready"] is True
    assert validation["screening_only"] is False
    assert validation["provenance"]["hydrograph_path"].endswith("hydrograph.csv")


def test_validate_project_water_source_accepts_rain_on_grid_with_provenance(tmp_path):
    project = _make_project(
        tmp_path,
        plan_text=_plan_text(extra="Meteorological Data=AORC event catalog\n"),
    )

    validation = ws.validate_project_water_source(
        project,
        water_source_mode="rain-on-grid",
        water_source_provenance={"dataset": "NOAA AORC v1.1"},
    )

    assert validation["requested_mode"] == "rain_on_grid"
    assert validation["mode"] == "rain_on_grid"
    assert validation["contract_status"] == "valid"
    assert validation["production_ready"] is True
    assert validation["file_evidence"]["has_rain_on_grid_marker"] is True


def test_validate_project_water_source_records_screening_mode_warnings(tmp_path):
    project = _make_project(tmp_path, include_plan=False, include_flow=False)

    validation = ws.validate_project_water_source(project, mock=True)

    assert validation["mode"] == "mock_screening"
    assert validation["contract_status"] == "screening_only"
    assert validation["production_ready"] is False
    assert validation["diagnostics"] == []
    assert any("No generated .p##" in item for item in validation["warnings"])
    assert any("Mock/low-detail screening mode" in item for item in validation["warnings"])


def test_attach_water_source_metadata_writes_validation_artifacts(tmp_path):
    project = _make_project(tmp_path)
    validation = ws.validate_project_water_source(
        project,
        water_source_mode="external_hydrograph",
        water_source_provenance={"source": "test_fixture"},
    )

    water_source = ws.attach_water_source_metadata(project, validation)

    validation_path = Path(water_source["validation_path"])
    metadata_path = Path(water_source["metadata_path"])
    assert validation_path.exists()
    assert metadata_path.exists()
    assert project.metadata["water_source"] == water_source
    assert project.metadata["model_readiness"] == "production_ready"
    assert project.metadata["water_source_validation"] == str(validation_path)
    assert json.loads(validation_path.read_text(encoding="utf-8"))["mode"] == (
        "external_hydrograph"
    )
    metadata_json = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata_json["water_source"]["production_ready"] is True


@pytest.mark.parametrize(
    ("mode", "project_kwargs", "provenance", "expected_detail"),
    [
        (
            "auto",
            {"flow_text": _dry_flow_text()},
            {},
            "No defensible water source was found",
        ),
        (
            "external_hydrograph",
            {"flow_text": _dry_flow_text()},
            {"source": "test_fixture"},
            "requires at least one well-formed Flow Hydrograph block",
        ),
        (
            "rain_on_grid",
            {},
            {"source": "manual"},
            "requires generated .u##/.p## content",
        ),
        (
            "rain_on_grid",
            {"plan_text": _plan_text(extra="Precipitation=AORC\n")},
            {"source": "manual"},
            "rain_on_grid provenance must identify an AORC or MRMS source",
        ),
    ],
)
def test_format_water_source_error_includes_each_validation_failure_type(
    tmp_path,
    mode,
    project_kwargs,
    provenance,
    expected_detail,
):
    project = _make_project(tmp_path, **project_kwargs)
    validation = ws.validate_project_water_source(
        project,
        water_source_mode=mode,
        water_source_provenance=provenance,
    )

    message = ws.format_water_source_error(validation, project.project_dir)

    assert expected_detail in message
    assert f"Water-source mode={validation['mode']!r}" in message
    assert f"status={validation['contract_status']!r}" in message
    assert str(project.project_dir / "water_source_validation.json") in message


def test_format_water_source_error_uses_default_detail_without_diagnostics(tmp_path):
    validation = {
        "mode": "mock_screening",
        "contract_status": "screening_only",
        "diagnostics": [],
    }

    message = ws.format_water_source_error(validation, tmp_path)

    assert "Water-source contract is not valid." in message
    assert "Water-source mode='mock_screening'" in message
    assert "status='screening_only'" in message


def test_ensure_project_water_source_ready_returns_validated_project(tmp_path):
    project = _make_project(tmp_path)

    validation = ws.ensure_project_water_source_ready(
        project,
        water_source_mode="external_hydrograph",
        water_source_provenance={"source": "test_fixture"},
    )

    assert validation["production_ready"] is True
    assert project.metadata["model_readiness"] == "production_ready"
    assert (project.project_dir / "water_source_validation.json").exists()
    assert (project.project_dir / "ras_agent_model_metadata.json").exists()


def test_ensure_project_water_source_ready_raises_for_invalid_contract(tmp_path):
    project = _make_project(tmp_path, flow_text=_dry_flow_text())

    with pytest.raises(ws.WaterSourceContractError) as excinfo:
        ws.ensure_project_water_source_ready(project)

    assert "Generated model is not production-ready" in str(excinfo.value)
    assert excinfo.value.validation["contract_status"] == "invalid"
    assert excinfo.value.validation["mode"] == "none"
    assert project.metadata["model_readiness"] == "blocked"
    assert (project.project_dir / "water_source_validation.json").exists()


def test_ensure_project_water_source_ready_blocks_screening_when_production_required(
    tmp_path,
):
    project = _make_project(tmp_path, include_plan=False, include_flow=False)

    with pytest.raises(ws.WaterSourceContractError) as excinfo:
        ws.ensure_project_water_source_ready(project, mock=True)

    assert excinfo.value.validation["contract_status"] == "screening_only"
    assert excinfo.value.validation["production_ready"] is False
    assert project.metadata["model_readiness"] == "screening_only"

    non_production_project = _make_project(
        tmp_path / "non_production",
        include_plan=False,
        include_flow=False,
    )
    validation = ws.ensure_project_water_source_ready(
        non_production_project,
        mock=True,
        require_production_ready=False,
    )
    assert validation["contract_status"] == "screening_only"
    assert validation["production_ready"] is False
