"""
water_source.py - headwater water-source contract validation.

This module stays in ras-agent as an orchestration/readiness gate. It does not
write reusable precipitation or inflow primitives; those belong in ras-commander
once rain-on-grid and external hydrograph authoring are generalized.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional


WATER_SOURCE_MODES = (
    "auto",
    "none",
    "rain_on_grid",
    "external_hydrograph",
    "mock_screening",
)

RAIN_ON_GRID_SOURCE_LABELS = ("aorc", "mrms")
RAIN_ON_GRID_MARKERS = (
    "rain-on-grid",
    "rain on grid",
    "precipitation",
    "meteorological",
    "met data",
    "aorc",
    "mrms",
)

_FLOAT_RE = re.compile(
    r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
)


class WaterSourceContractError(RuntimeError):
    """Raised when a generated model does not have a valid water source."""

    def __init__(self, message: str, validation: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.validation = validation or {}


def normalize_water_source_mode(
    water_source_mode: Optional[str],
    *,
    mock: bool = False,
    allow_low_detail_screening: bool = False,
) -> str:
    """Normalize public water-source mode labels."""
    if water_source_mode is None:
        return "mock_screening" if mock or allow_low_detail_screening else "auto"

    normalized = str(water_source_mode).strip().lower().replace("-", "_")
    if normalized == "":
        return "mock_screening" if mock or allow_low_detail_screening else "auto"
    if normalized == "auto" and (mock or allow_low_detail_screening):
        return "mock_screening"

    aliases = {
        "auto": "auto",
        "none": "none",
        "dry": "none",
        "no_water": "none",
        "rain": "rain_on_grid",
        "rog": "rain_on_grid",
        "rain_on_grid": "rain_on_grid",
        "rainfall": "rain_on_grid",
        "aorc": "rain_on_grid",
        "mrms": "rain_on_grid",
        "hydrograph": "external_hydrograph",
        "boundary_hydrograph": "external_hydrograph",
        "external_hydrograph": "external_hydrograph",
        "external_inflow": "external_hydrograph",
        "inflow_hydrograph": "external_hydrograph",
        "mock": "mock_screening",
        "screening": "mock_screening",
        "first_pass": "mock_screening",
        "low_detail": "mock_screening",
        "low_detail_screening": "mock_screening",
        "mock_screening": "mock_screening",
    }
    if normalized not in aliases:
        raise ValueError(
            f"Unknown water_source_mode: {water_source_mode!r}. "
            f"Valid options: {', '.join(WATER_SOURCE_MODES)}"
        )
    return aliases[normalized]


def _jsonable(value: Any) -> Any:
    """Convert pathlib and simple containers to JSON-serializable values."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except TypeError:
        return path.read_text(errors="ignore")


def _unique_existing(paths: list[Path]) -> list[Path]:
    seen = set()
    unique: list[Path] = []
    for path in paths:
        p = Path(path)
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def _project_plan_files(project) -> list[Path]:
    project_dir = Path(project.project_dir)
    paths = []
    plan_file = getattr(project, "plan_file", None)
    if plan_file is not None:
        paths.append(Path(plan_file))
    paths.extend(project_dir.glob("*.p??"))
    return [
        path for path in _unique_existing(paths)
        if re.fullmatch(r"\.p\d\d", path.suffix.lower())
    ]


def _project_flow_files(project) -> list[Path]:
    project_dir = Path(project.project_dir)
    paths = []
    flow_file = getattr(project, "flow_file", None)
    if flow_file is not None:
        paths.append(Path(flow_file))
    paths.extend(project_dir.glob("*.u??"))
    return [
        path for path in _unique_existing(paths)
        if re.fullmatch(r"\.u\d\d", path.suffix.lower())
    ]


def _flow_file_for_plan(plan_file: Path, flow_ext: str) -> Optional[Path]:
    candidate = plan_file.with_suffix(f".{flow_ext}")
    if candidate.exists():
        return candidate
    matches = sorted(plan_file.parent.glob(f"*.{flow_ext}"))
    return matches[0] if matches else candidate


def _parse_flow_hydrograph_blocks(text: str) -> list[dict[str, Any]]:
    """Parse declared Flow Hydrograph blocks and their following value lines."""
    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []

    for idx, line in enumerate(lines):
        match = re.search(r"Flow Hydrograph=\s*(\d+)", line)
        if not match:
            continue

        declared_points = int(match.group(1))
        values: list[float] = []
        for value_line in lines[idx + 1 :]:
            stripped = value_line.strip()
            if not stripped:
                if values:
                    break
                continue
            if "=" in stripped:
                break
            try:
                values.extend(float(token) for token in _FLOAT_RE.findall(stripped))
            except ValueError:
                break
            if len(values) >= declared_points:
                values = values[:declared_points]
                break

        positive_values = [value for value in values if value > 0.0]
        malformed = len(values) < declared_points
        blocks.append(
            {
                "line_number": idx + 1,
                "declared_points": declared_points,
                "parsed_value_count": len(values),
                "positive_value_count": len(positive_values),
                "max_flow_cfs": max(values) if values else None,
                "all_zero": bool(values) and not positive_values,
                "malformed": malformed,
                "valid_positive": (
                    declared_points > 0
                    and not malformed
                    and bool(positive_values)
                ),
            }
        )

    return blocks


def inspect_hec_ras_water_source_files(project) -> dict[str, Any]:
    """Inspect generated .p## and .u## files for water-source evidence."""
    plan_checks = []
    flow_checks = []
    diagnostics = []

    plan_files = _project_plan_files(project)
    flow_files = _project_flow_files(project)

    if not plan_files:
        diagnostics.append("No generated .p## plan files were found.")
    if not flow_files:
        diagnostics.append("No generated .u## unsteady flow files were found.")

    for plan_file in plan_files:
        check: dict[str, Any] = {
            "path": str(plan_file),
            "exists": plan_file.exists(),
            "flow_file": None,
            "referenced_flow_exists": False,
            "has_geom_file": False,
            "has_simulation_date": False,
            "rain_on_grid_marker_count": 0,
        }
        if not plan_file.exists():
            diagnostics.append(f"Plan file is missing: {plan_file}")
            plan_checks.append(check)
            continue

        text = _read_text(plan_file)
        text_lower = text.lower()
        check["rain_on_grid_marker_count"] = sum(
            1 for marker in RAIN_ON_GRID_MARKERS if marker in text_lower
        )
        flow_match = re.search(r"^Flow File=(\S+)", text, flags=re.MULTILINE)
        geom_match = re.search(r"^Geom File=(\S+)", text, flags=re.MULTILINE)
        check["has_geom_file"] = geom_match is not None
        check["has_simulation_date"] = "Simulation Date=" in text
        if flow_match:
            flow_ext = flow_match.group(1).strip()
            flow_path = _flow_file_for_plan(plan_file, flow_ext)
            check["flow_file"] = str(flow_path)
            check["referenced_flow_exists"] = flow_path.exists()
            if not flow_path.exists():
                diagnostics.append(
                    f"Plan {plan_file.name} references missing flow file {flow_ext}."
                )
        else:
            diagnostics.append(f"Plan {plan_file.name} does not include a Flow File entry.")
        if not check["has_geom_file"]:
            diagnostics.append(f"Plan {plan_file.name} does not include a Geom File entry.")
        plan_checks.append(check)

    for flow_file in flow_files:
        check = {
            "path": str(flow_file),
            "exists": flow_file.exists(),
            "boundary_location_count": 0,
            "normal_depth_count": 0,
            "flow_hydrograph_count": 0,
            "flow_hydrograph_points": [],
            "flow_hydrograph_blocks": [],
            "has_positive_flow_hydrograph": False,
            "rain_on_grid_marker_count": 0,
        }
        if not flow_file.exists():
            diagnostics.append(f"Flow file is missing: {flow_file}")
            flow_checks.append(check)
            continue

        text = _read_text(flow_file)
        text_lower = text.lower()
        check["boundary_location_count"] = text.count("Boundary Location=")
        check["normal_depth_count"] = (
            text.count("Normal Depth=") + text.count("Friction Slope=")
        )
        blocks = _parse_flow_hydrograph_blocks(text)
        points = [block["declared_points"] for block in blocks]
        check["flow_hydrograph_count"] = len(points)
        check["flow_hydrograph_points"] = points
        check["flow_hydrograph_blocks"] = blocks
        check["has_positive_flow_hydrograph"] = any(
            block["valid_positive"] for block in blocks
        )
        check["rain_on_grid_marker_count"] = sum(
            1 for marker in RAIN_ON_GRID_MARKERS if marker in text_lower
        )
        if check["boundary_location_count"] == 0:
            diagnostics.append(f"Flow file {flow_file.name} has no Boundary Location entries.")
        for block in blocks:
            if block["declared_points"] <= 0:
                diagnostics.append(
                    f"Flow file {flow_file.name} has a Flow Hydrograph block "
                    f"with non-positive point count on line {block['line_number']}."
                )
            elif block["malformed"]:
                diagnostics.append(
                    f"Flow file {flow_file.name} declares "
                    f"{block['declared_points']} Flow Hydrograph values on line "
                    f"{block['line_number']} but only "
                    f"{block['parsed_value_count']} numeric values were found."
                )
            elif block["all_zero"]:
                diagnostics.append(
                    f"Flow file {flow_file.name} has an all-zero/non-positive "
                    f"Flow Hydrograph block on line {block['line_number']}."
                )
        flow_checks.append(check)

    return {
        "plan_files": plan_checks,
        "flow_files": flow_checks,
        "diagnostics": diagnostics,
        "has_external_hydrograph": any(
            check["has_positive_flow_hydrograph"] for check in flow_checks
        ),
        "has_rain_on_grid_marker": any(
            check["rain_on_grid_marker_count"] > 0 for check in flow_checks
        )
        or any(check["rain_on_grid_marker_count"] > 0 for check in plan_checks),
    }


def _infer_water_source_mode(file_evidence: dict[str, Any]) -> str:
    if file_evidence.get("has_rain_on_grid_marker"):
        return "rain_on_grid"
    if file_evidence.get("has_external_hydrograph"):
        return "external_hydrograph"
    return "none"


def _rain_on_grid_provenance_valid(provenance: dict[str, Any]) -> bool:
    values = []
    for key in ("source", "dataset", "provider", "mode"):
        value = provenance.get(key)
        if value is not None:
            values.append(str(value).lower())
    joined = " ".join(values)
    return any(label in joined for label in RAIN_ON_GRID_SOURCE_LABELS)


def validate_project_water_source(
    project,
    *,
    water_source_mode: Optional[str] = "auto",
    water_source_provenance: Optional[dict[str, Any]] = None,
    mock: bool = False,
    allow_low_detail_screening: bool = False,
) -> dict[str, Any]:
    """
    Validate generated model files and metadata against the headwater contract.
    """
    requested_mode = normalize_water_source_mode(
        water_source_mode,
        mock=mock,
        allow_low_detail_screening=allow_low_detail_screening,
    )
    provenance = _jsonable(water_source_provenance or {})
    file_evidence = inspect_hec_ras_water_source_files(project)
    effective_mode = (
        _infer_water_source_mode(file_evidence)
        if requested_mode == "auto"
        else requested_mode
    )

    file_diagnostics = list(file_evidence.get("diagnostics", []))
    failures: list[str] = []
    warnings: list[str] = []
    if effective_mode == "mock_screening":
        warnings.extend(file_diagnostics)
    else:
        failures.extend(file_diagnostics)

    if effective_mode == "none":
        failures.append(
            "No defensible water source was found. Provide AORC/MRMS rain-on-grid, "
            "a generated/external hydrograph boundary in the .u## file, or run in "
            "explicit mock/low-detail screening mode."
        )
    elif effective_mode == "external_hydrograph":
        if not file_evidence.get("has_external_hydrograph"):
            failures.append(
                "water_source_mode='external_hydrograph' requires at least one "
                "well-formed Flow Hydrograph block with positive flow values in "
                "the generated .u## files."
            )
    elif effective_mode == "rain_on_grid":
        if not file_evidence.get("has_rain_on_grid_marker"):
            failures.append(
                "water_source_mode='rain_on_grid' requires generated .u##/.p## "
                "content that references precipitation/rain-on-grid data."
            )
        if not _rain_on_grid_provenance_valid(provenance):
            failures.append(
                "rain_on_grid provenance must identify an AORC or MRMS source."
            )
    elif effective_mode == "mock_screening":
        warnings.append(
            "Mock/low-detail screening mode is not production-ready and must not "
            "be treated as a runnable flood model."
        )

    if effective_mode in ("external_hydrograph", "rain_on_grid") and not provenance:
        warnings.append(
            "Water-source provenance was inferred from model files but no explicit "
            "provenance payload was supplied."
        )

    contract_status = "invalid" if failures else "valid"
    if effective_mode == "mock_screening" and not failures:
        contract_status = "screening_only"

    production_ready = contract_status == "valid" and effective_mode in (
        "external_hydrograph",
        "rain_on_grid",
    )

    return {
        "schema_version": "ras-agent-water-source/v1",
        "mode": effective_mode,
        "requested_mode": requested_mode,
        "contract_status": contract_status,
        "production_ready": production_ready,
        "mock": bool(mock),
        "screening_only": effective_mode == "mock_screening",
        "provenance": provenance,
        "diagnostics": failures,
        "warnings": warnings,
        "file_evidence": file_evidence,
    }


def attach_water_source_metadata(
    project,
    validation: dict[str, Any],
    *,
    write_files: bool = True,
) -> dict[str, Any]:
    """Attach validation to project metadata and write JSON artifacts."""
    metadata = getattr(project, "metadata", None)
    if metadata is None:
        metadata = {}
        project.metadata = metadata

    water_source = {
        "schema_version": validation["schema_version"],
        "mode": validation["mode"],
        "requested_mode": validation["requested_mode"],
        "contract_status": validation["contract_status"],
        "production_ready": validation["production_ready"],
        "screening_only": validation["screening_only"],
        "provenance": validation["provenance"],
        "diagnostics": validation["diagnostics"],
        "warnings": validation["warnings"],
    }
    metadata["water_source"] = water_source
    metadata["model_readiness"] = (
        "production_ready"
        if validation["production_ready"]
        else "screening_only"
        if validation["screening_only"]
        else "blocked"
    )

    if not write_files:
        return water_source

    project_dir = Path(project.project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    validation_path = project_dir / "water_source_validation.json"
    metadata_path = project_dir / "ras_agent_model_metadata.json"
    validation_path.write_text(json.dumps(validation, indent=2), encoding="utf-8")
    water_source["validation_path"] = str(validation_path)
    water_source["metadata_path"] = str(metadata_path)
    metadata["water_source_validation"] = str(validation_path)
    metadata["metadata_path"] = str(metadata_path)
    metadata_path.write_text(json.dumps(_jsonable(metadata), indent=2), encoding="utf-8")
    return water_source


def format_water_source_error(validation: dict[str, Any], project_dir: Path) -> str:
    diagnostics = validation.get("diagnostics") or []
    first = diagnostics[0] if diagnostics else "Water-source contract is not valid."
    return (
        f"Generated model is not production-ready: {first} "
        f"Water-source mode={validation.get('mode')!r}, "
        f"status={validation.get('contract_status')!r}. "
        f"Review {Path(project_dir) / 'water_source_validation.json'} for details."
    )


def ensure_project_water_source_ready(
    project,
    *,
    water_source_mode: Optional[str] = "auto",
    water_source_provenance: Optional[dict[str, Any]] = None,
    mock: bool = False,
    allow_low_detail_screening: bool = False,
    require_production_ready: bool = True,
) -> dict[str, Any]:
    """Validate, attach metadata, and raise when the contract is not sufficient."""
    validation = validate_project_water_source(
        project,
        water_source_mode=water_source_mode,
        water_source_provenance=water_source_provenance,
        mock=mock,
        allow_low_detail_screening=allow_low_detail_screening,
    )
    attach_water_source_metadata(project, validation, write_files=True)
    if validation["contract_status"] == "invalid":
        raise WaterSourceContractError(
            format_water_source_error(validation, Path(project.project_dir)),
            validation=validation,
        )
    if require_production_ready and not validation["production_ready"]:
        raise WaterSourceContractError(
            format_water_source_error(validation, Path(project.project_dir)),
            validation=validation,
        )
    return validation
