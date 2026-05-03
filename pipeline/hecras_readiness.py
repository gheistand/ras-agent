"""
hecras_readiness.py - pre-run HEC-RAS artifact readiness gate.

The geometry-first build path writes authoritative plain-text project files.
This module checks the derived HEC-RAS artifacts that must be current before
RAS Agent hands a plan HDF to the queue runner:

* RASMapper terrain HDF registration
* compiled geometry HDF and geometry preprocessor artifacts
* unsteady plan HDF

Regeneration is intentionally routed through ras-commander APIs. This module
does not shell out to Ras.exe directly.
"""

from __future__ import annotations

import json
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


_RASTER_SUFFIXES = {".tif", ".tiff", ".vrt", ".flt", ".adf"}


@dataclass
class ArtifactStatus:
    """Filesystem status for one required HEC-RAS artifact."""

    name: str
    path: Optional[Path]
    exists: bool
    size_bytes: Optional[int]
    modified_utc: Optional[str]
    stale: bool = False
    stale_reason: Optional[str] = None
    source_paths: list[Path] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path) if self.path is not None else None,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "modified_utc": self.modified_utc,
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "source_paths": [str(path) for path in self.source_paths],
        }


@dataclass
class HecRasReadinessReport:
    """Readiness result written before HEC-RAS compute is started."""

    status: str
    project_dir: Path
    plan_hdf: Path
    geom_ext: str
    checked_at_utc: str
    hec_ras_version: Optional[str] = None
    artifacts: dict[str, ArtifactStatus] = field(default_factory=dict)
    rasmap_terrain_layers: list[dict[str, Any]] = field(default_factory=list)
    regeneration_attempted: bool = False
    regeneration_performed: bool = False
    actions: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    preprocessor: Optional[dict[str, Any]] = None
    report_path: Optional[Path] = None

    @property
    def ready(self) -> bool:
        return self.status in {"ready", "regenerated"}

    def summary(self) -> str:
        if self.ready:
            return f"HEC-RAS pre-run readiness: {self.status}"
        if not self.blockers:
            return "HEC-RAS pre-run readiness blocked"
        return "HEC-RAS pre-run readiness blocked: " + "; ".join(self.blockers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "project_dir": str(self.project_dir),
            "plan_hdf": str(self.plan_hdf),
            "geom_ext": self.geom_ext,
            "checked_at_utc": self.checked_at_utc,
            "hec_ras_version": self.hec_ras_version,
            "artifacts": {
                name: status.to_dict()
                for name, status in self.artifacts.items()
            },
            "rasmap_terrain_layers": self.rasmap_terrain_layers,
            "regeneration_attempted": self.regeneration_attempted,
            "regeneration_performed": self.regeneration_performed,
            "actions": list(self.actions),
            "messages": list(self.messages),
            "blockers": list(self.blockers),
            "preprocessor": self.preprocessor,
            "report_path": str(self.report_path) if self.report_path else None,
        }


class HecRasReadinessError(RuntimeError):
    """Raised when the pre-run gate cannot make artifacts ready."""

    def __init__(self, report: HecRasReadinessReport):
        self.report = report
        super().__init__(report.summary())


@dataclass
class _ProjectContext:
    project_dir: Path
    plan_hdf: Path
    geom_ext: str
    project_base: str
    plan_number: str
    project_file: Optional[Path]
    rasmap_file: Optional[Path]
    plan_file: Path
    flow_file: Optional[Path]
    geometry_file: Path
    geometry_hdf: Path
    geompre_file: Path
    b_file: Path
    x_file: Path
    tmp_plan_hdf: Path
    terrain_hdf: Optional[Path]
    terrain_source: Optional[Path]
    terrain_layer_name: str
    terrain_hdf_registered: bool
    terrain_layers: list[dict[str, Any]]
    hec_ras_version: Optional[str]


def check_hecras_readiness(
    project_dir: Path,
    plan_hdf: Path,
    geom_ext: str = "g01",
    *,
    regenerate: bool = True,
    ras_version: Optional[str | Path] = None,
    max_wait: int = 600,
    terrain_units: str = "Feet",
    write_report: bool = True,
    report_path: Optional[Path] = None,
) -> HecRasReadinessReport:
    """
    Check and optionally regenerate derived HEC-RAS artifacts before compute.

    The returned report is bool-compatible through ``report.ready``. When
    regeneration is needed, terrain creation, RASMapper registration, and
    geometry preprocessing are delegated to ras-commander APIs.
    """
    context = _resolve_context(project_dir, plan_hdf, geom_ext, ras_version)
    report = _build_report(context)

    if regenerate and not report.ready:
        report.regeneration_attempted = True
        _attempt_regeneration(report, context, max_wait=max_wait, terrain_units=terrain_units)

        refreshed = _resolve_context(project_dir, plan_hdf, geom_ext, ras_version)
        refreshed_report = _build_report(refreshed)
        refreshed_report.regeneration_attempted = report.regeneration_attempted
        refreshed_report.regeneration_performed = report.regeneration_performed
        refreshed_report.actions = report.actions + refreshed_report.actions
        refreshed_report.messages = report.messages + refreshed_report.messages
        refreshed_report.preprocessor = report.preprocessor or refreshed_report.preprocessor

        if refreshed_report.ready and report.regeneration_performed:
            refreshed_report.status = "regenerated"
        report = refreshed_report

    if write_report:
        write_readiness_report(report, report_path)

    return report


def ensure_hecras_readiness(
    project_dir: Path,
    plan_hdf: Path,
    geom_ext: str = "g01",
    **kwargs: Any,
) -> HecRasReadinessReport:
    """Return a readiness report or raise ``HecRasReadinessError``."""
    report = check_hecras_readiness(project_dir, plan_hdf, geom_ext, **kwargs)
    if not report.ready:
        raise HecRasReadinessError(report)
    return report


def write_readiness_report(
    report: HecRasReadinessReport,
    report_path: Optional[Path] = None,
) -> Path:
    """Write the readiness report JSON and return its path."""
    if report_path is None:
        report_path = report.project_dir / "pre_run_readiness.json"
    else:
        report_path = Path(report_path)
        if report_path.suffix.lower() != ".json":
            report_path = report_path / f"{report.plan_hdf.stem}_readiness.json"

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report.report_path = report_path
    report_path.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return report_path


def _attempt_regeneration(
    report: HecRasReadinessReport,
    context: _ProjectContext,
    *,
    max_wait: int,
    terrain_units: str,
) -> None:
    if _artifact_needs_work(report, "terrain_hdf") or not context.terrain_hdf_registered:
        _regenerate_or_register_terrain(report, context, terrain_units=terrain_units)

    if (
        _artifact_needs_work(report, "geometry_hdf")
        or _artifact_needs_work(report, "plan_hdf")
        or _artifact_needs_work(report, "geompre_file")
    ):
        _run_geometry_preprocessor(report, context, max_wait=max_wait)


def _artifact_needs_work(report: HecRasReadinessReport, name: str) -> bool:
    artifact = report.artifacts.get(name)
    return bool(artifact and (not artifact.exists or artifact.stale))


def _regenerate_or_register_terrain(
    report: HecRasReadinessReport,
    context: _ProjectContext,
    *,
    terrain_units: str,
) -> None:
    if context.rasmap_file is None or not context.rasmap_file.exists():
        report.messages.append("Cannot register terrain HDF because the .rasmap file is missing.")
        return

    terrain_hdf = context.terrain_hdf
    source = context.terrain_source

    try:
        if terrain_hdf is None or not terrain_hdf.exists() or _is_stale(terrain_hdf, [source] if source else [])[0]:
            if source is None or not source.exists():
                report.messages.append(
                    "Cannot create terrain HDF because no existing source raster is registered in RASMapper."
                )
                return

            from ras_commander.terrain import RasTerrain

            terrain_folder = terrain_hdf.parent if terrain_hdf else context.project_dir / "Terrain"
            terrain_name = terrain_hdf.stem if terrain_hdf else source.stem
            created = RasTerrain.create_terrain_from_rasters(
                input_rasters=[source],
                output_folder=terrain_folder,
                terrain_name=terrain_name,
                units=terrain_units,
                hecras_version=context.hec_ras_version or "6.6",
                generate_prj=True,
            )
            terrain_hdf = Path(created)
            report.regeneration_performed = True
            report.actions.append(f"created terrain HDF via RasTerrain: {terrain_hdf}")

        if terrain_hdf is not None and terrain_hdf.exists() and not context.terrain_hdf_registered:
            from ras_commander import RasMap

            projection_prj = terrain_hdf.parent / "Projection.prj"
            RasMap.add_terrain_layer(
                terrain_hdf,
                context.rasmap_file,
                layer_name=context.terrain_layer_name,
                projection_prj=projection_prj if projection_prj.exists() else None,
            )
            report.regeneration_performed = True
            report.actions.append(
                f"registered terrain HDF in RASMapper via RasMap: {terrain_hdf}"
            )
    except Exception as exc:
        report.messages.append(
            "Terrain regeneration failed through ras-commander. "
            f"Windows HEC-RAS/RAS Mapper terrain tools may require manual action: {exc}"
        )


def _run_geometry_preprocessor(
    report: HecRasReadinessReport,
    context: _ProjectContext,
    *,
    max_wait: int,
) -> None:
    try:
        from ras_commander import init_ras_project
        from ras_commander.geom import GeomPreprocessor

        init_path = context.project_file or context.project_dir
        ras_object = init_ras_project(
            init_path,
            ras_version=context.hec_ras_version,
            ras_object="new",
            load_results_summary=False,
        )
        result = GeomPreprocessor.run_geometry_preprocessor(
            context.plan_file,
            ras_object=ras_object,
            max_wait=max_wait,
            force=True,
            clear_messages=True,
            clear_geompre=True,
            geometry_only=True,
            restore_plan_settings=True,
        )
        report.preprocessor = _preprocessor_result_to_dict(result)

        if getattr(result, "success", False):
            report.regeneration_performed = True
            report.actions.append(
                "ran geometry preprocessing via GeomPreprocessor.run_geometry_preprocessor"
            )
            _promote_tmp_plan_hdf(report, context)
        else:
            report.messages.append(
                "Geometry preprocessing did not complete successfully through ras-commander: "
                f"{getattr(result, 'error', 'unknown error')}"
            )
    except Exception as exc:
        report.messages.append(
            "Geometry/plan regeneration failed through ras-commander. "
            "Regeneration requires Windows HEC-RAS command/GUI components; "
            f"no direct Ras.exe fallback was attempted: {exc}"
        )


def _promote_tmp_plan_hdf(report: HecRasReadinessReport, context: _ProjectContext) -> None:
    if not context.tmp_plan_hdf.exists() or context.tmp_plan_hdf.stat().st_size <= 0:
        return

    needs_copy = not context.plan_hdf.exists()
    if context.plan_hdf.exists():
        needs_copy = context.tmp_plan_hdf.stat().st_mtime > context.plan_hdf.stat().st_mtime

    if needs_copy:
        shutil.copy2(context.tmp_plan_hdf, context.plan_hdf)
        context.plan_hdf.touch()
        report.actions.append(
            f"promoted geometry preprocessor tmp HDF to plan HDF: {context.plan_hdf}"
        )


def _build_report(context: _ProjectContext) -> HecRasReadinessReport:
    artifacts = _collect_artifacts(context)
    blockers = _collect_blockers(context, artifacts)
    status = "ready" if not blockers else "blocked"

    return HecRasReadinessReport(
        status=status,
        project_dir=context.project_dir,
        plan_hdf=context.plan_hdf,
        geom_ext=context.geom_ext,
        checked_at_utc=_now_utc(),
        hec_ras_version=context.hec_ras_version,
        artifacts=artifacts,
        rasmap_terrain_layers=context.terrain_layers,
        blockers=blockers,
    )


def _collect_artifacts(context: _ProjectContext) -> dict[str, ArtifactStatus]:
    terrain_sources = [context.terrain_source] if context.terrain_source else []
    geometry_sources = [
        context.geometry_file,
        context.rasmap_file,
        context.terrain_hdf,
    ]
    plan_sources = [
        context.plan_file,
        context.flow_file,
        context.geometry_hdf,
        context.project_file,
    ]
    geompre_sources = [context.geometry_file, context.plan_file, context.flow_file]

    return {
        "project_file": _artifact("project_file", context.project_file),
        "rasmap_file": _artifact("rasmap_file", context.rasmap_file),
        "plan_file": _artifact("plan_file", context.plan_file),
        "flow_file": _artifact("flow_file", context.flow_file),
        "geometry_file": _artifact("geometry_file", context.geometry_file),
        "terrain_source": _artifact("terrain_source", context.terrain_source),
        "terrain_hdf": _artifact(
            "terrain_hdf",
            context.terrain_hdf,
            terrain_sources,
            force_stale_reason=(
                None if context.terrain_hdf_registered
                else "terrain HDF is not registered in the .rasmap file"
            ),
        ),
        "geometry_hdf": _artifact("geometry_hdf", context.geometry_hdf, geometry_sources),
        "geompre_file": _artifact("geompre_file", context.geompre_file, geompre_sources),
        "b_file": _artifact("b_file", context.b_file, geompre_sources),
        "x_file": _artifact("x_file", context.x_file, geompre_sources),
        "plan_hdf": _artifact("plan_hdf", context.plan_hdf, plan_sources),
        "tmp_plan_hdf": _artifact("tmp_plan_hdf", context.tmp_plan_hdf),
    }


def _collect_blockers(
    context: _ProjectContext,
    artifacts: dict[str, ArtifactStatus],
) -> list[str]:
    blockers: list[str] = []
    required = [
        "project_file",
        "rasmap_file",
        "plan_file",
        "flow_file",
        "geometry_file",
    ]
    for name in required:
        artifact = artifacts[name]
        if not artifact.exists:
            blockers.append(f"Required {name} is missing: {artifact.path}")

    if not context.terrain_layers:
        blockers.append("No terrain layer is registered in the .rasmap file.")

    for name in ("terrain_hdf", "geometry_hdf", "geompre_file", "plan_hdf"):
        artifact = artifacts[name]
        if not artifact.exists:
            blockers.append(f"{name} is missing: {artifact.path}")
        elif artifact.stale:
            blockers.append(f"{name} is stale: {artifact.stale_reason}")

    if artifacts["terrain_hdf"].stale and context.terrain_source is None:
        blockers.append(
            "Terrain HDF cannot be regenerated automatically because no source terrain raster was found."
        )

    return blockers


def _artifact(
    name: str,
    path: Optional[Path],
    source_paths: Optional[list[Optional[Path]]] = None,
    *,
    force_stale_reason: Optional[str] = None,
) -> ArtifactStatus:
    path = Path(path) if path is not None else None
    source_paths_clean = [Path(p) for p in (source_paths or []) if p is not None]

    if path is None:
        return ArtifactStatus(
            name=name,
            path=None,
            exists=False,
            size_bytes=None,
            modified_utc=None,
            stale=True,
            stale_reason=force_stale_reason or "path is unresolved",
            source_paths=source_paths_clean,
        )

    exists = path.exists()
    size = path.stat().st_size if exists and path.is_file() else None
    modified = _mtime_utc(path) if exists else None
    stale, reason = _is_stale(path, source_paths_clean)
    if force_stale_reason:
        stale = True
        reason = force_stale_reason

    return ArtifactStatus(
        name=name,
        path=path,
        exists=exists,
        size_bytes=size,
        modified_utc=modified,
        stale=stale,
        stale_reason=reason,
        source_paths=source_paths_clean,
    )


def _is_stale(path: Path, source_paths: list[Path]) -> tuple[bool, Optional[str]]:
    if not path.exists():
        return True, "missing"
    if path.is_file() and path.stat().st_size <= 0:
        return True, "empty file"

    existing_sources = [source for source in source_paths if source and source.exists()]
    if not existing_sources:
        return False, None

    target_mtime = path.stat().st_mtime
    newer = [source for source in existing_sources if source.stat().st_mtime > target_mtime]
    if newer:
        names = ", ".join(str(source) for source in newer)
        return True, f"newer source file(s): {names}"

    return False, None


def _resolve_context(
    project_dir: Path,
    plan_hdf: Path,
    geom_ext: str,
    ras_version: Optional[str | Path],
) -> _ProjectContext:
    project_dir = Path(project_dir).resolve()
    plan_hdf = Path(plan_hdf).resolve()
    project_base, plan_number = _parse_plan_hdf_name(plan_hdf)

    plan_file = project_dir / f"{project_base}.p{plan_number}"
    geometry_ext = _read_ras_value(plan_file, "Geom File") or geom_ext
    flow_ext = _read_ras_value(plan_file, "Flow File")
    geometry_file = project_dir / f"{project_base}.{geometry_ext}"
    flow_file = project_dir / f"{project_base}.{flow_ext}" if flow_ext else None
    project_file = _find_project_file(project_dir, project_base)
    rasmap_file = _find_rasmap_file(project_dir, project_base, project_file)
    terrain_layers = _list_terrain_layers(project_dir, rasmap_file)
    terrain_hdf, terrain_source, terrain_name, terrain_registered = _resolve_terrain(
        project_dir,
        terrain_layers,
    )
    hec_ras_version = _normalize_ras_version(
        str(ras_version) if ras_version is not None else _read_program_version(plan_file)
    )

    return _ProjectContext(
        project_dir=project_dir,
        plan_hdf=plan_hdf,
        geom_ext=geometry_ext,
        project_base=project_base,
        plan_number=plan_number,
        project_file=project_file,
        rasmap_file=rasmap_file,
        plan_file=plan_file,
        flow_file=flow_file,
        geometry_file=geometry_file,
        geometry_hdf=geometry_file.with_suffix(geometry_file.suffix + ".hdf"),
        geompre_file=project_dir / f"{project_base}.c{_number_from_ext(geometry_ext)}",
        b_file=project_dir / f"{project_base}.b{plan_number}",
        x_file=project_dir / f"{project_base}.x{_number_from_ext(geometry_ext)}",
        tmp_plan_hdf=project_dir / f"{project_base}.p{plan_number}.tmp.hdf",
        terrain_hdf=terrain_hdf,
        terrain_source=terrain_source,
        terrain_layer_name=terrain_name,
        terrain_hdf_registered=terrain_registered,
        terrain_layers=terrain_layers,
        hec_ras_version=hec_ras_version,
    )


def _parse_plan_hdf_name(plan_hdf: Path) -> tuple[str, str]:
    match = re.match(r"(?P<base>.+)\.p(?P<num>\d{2})(?:\.tmp)?\.hdf$", plan_hdf.name, re.IGNORECASE)
    if not match:
        raise ValueError(
            f"Expected plan HDF name like '<project>.p01.hdf', got: {plan_hdf.name}"
        )
    return match.group("base"), match.group("num")


def _number_from_ext(ext: str) -> str:
    digits = "".join(ch for ch in ext if ch.isdigit())
    return digits.zfill(2) if digits else "01"


def _find_project_file(project_dir: Path, project_base: str) -> Optional[Path]:
    candidate = project_dir / f"{project_base}.prj"
    if candidate.exists():
        return candidate
    prj_files = sorted(project_dir.glob("*.prj"))
    return prj_files[0] if prj_files else candidate


def _find_rasmap_file(
    project_dir: Path,
    project_base: str,
    project_file: Optional[Path],
) -> Optional[Path]:
    candidates = [project_dir / f"{project_base}.rasmap"]
    if project_file is not None:
        candidates.append(project_dir / f"{project_file.stem}.rasmap")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    rasmap_files = sorted(project_dir.glob("*.rasmap"))
    return rasmap_files[0] if rasmap_files else candidates[0]


def _read_ras_value(path: Path, key: str) -> Optional[str]:
    if not path.exists():
        return None
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.strip().startswith(prefix):
            return line.split("=", 1)[1].strip()
    return None


def _read_program_version(plan_file: Path) -> Optional[str]:
    return _read_ras_value(plan_file, "Program Version")


def _normalize_ras_version(raw_version: Optional[str]) -> Optional[str]:
    if raw_version is None:
        return None
    version = str(raw_version).strip()
    if not version:
        return None
    if any(sep in version for sep in ("\\", "/")):
        return version
    parts = version.split(".")
    if len(parts) >= 2 and parts[1].endswith("0") and len(parts[1]) > 1:
        parts[1] = parts[1].rstrip("0") or "0"
        return ".".join(parts[:2])
    return version


def _list_terrain_layers(
    project_dir: Path,
    rasmap_file: Optional[Path],
) -> list[dict[str, Any]]:
    if rasmap_file is None or not rasmap_file.exists():
        return []

    try:
        from ras_commander import RasMap

        frame = RasMap.list_terrain_layers(rasmap_file)
        records = frame.to_dict(orient="records")
        return [_clean_terrain_record(record, rasmap_file.parent) for record in records]
    except Exception:
        return _parse_terrain_layers_xml(rasmap_file, project_dir)


def _parse_terrain_layers_xml(rasmap_file: Path, project_dir: Path) -> list[dict[str, Any]]:
    try:
        root = ET.parse(rasmap_file).getroot()
    except ET.ParseError:
        return []

    records = []
    for layer in root.findall(".//Terrains/Layer"):
        filename = layer.attrib.get("Filename")
        resolved = _resolve_rasmap_path(project_dir, filename)
        records.append(
            {
                "name": layer.attrib.get("Name", ""),
                "filename": filename,
                "resolved_path": str(resolved) if resolved else None,
                "checked": layer.attrib.get("Checked", "True").lower() == "true",
                "type": layer.attrib.get("Type", ""),
            }
        )
    return records


def _clean_terrain_record(record: dict[str, Any], rasmap_dir: Path) -> dict[str, Any]:
    cleaned = dict(record)
    resolved = cleaned.get("resolved_path")
    if resolved is None and cleaned.get("filename"):
        resolved_path = _resolve_rasmap_path(rasmap_dir, cleaned.get("filename"))
        cleaned["resolved_path"] = str(resolved_path) if resolved_path else None
    return cleaned


def _resolve_rasmap_path(base_dir: Path, filename: Optional[str]) -> Optional[Path]:
    if not filename:
        return None
    normalized = filename.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    candidate = Path(normalized)
    if candidate.is_absolute():
        return candidate
    return base_dir / candidate


def _resolve_terrain(
    project_dir: Path,
    terrain_layers: list[dict[str, Any]],
) -> tuple[Optional[Path], Optional[Path], str, bool]:
    registered_hdf: Optional[Path] = None
    source_raster: Optional[Path] = None
    layer_name = "Terrain"

    for layer in terrain_layers:
        if layer.get("name"):
            layer_name = str(layer["name"])
        path_value = layer.get("resolved_path") or layer.get("filename")
        path = _resolve_rasmap_path(project_dir, str(path_value)) if path_value else None
        if path is None:
            continue
        suffix = path.suffix.lower()
        if suffix == ".hdf" and registered_hdf is None:
            registered_hdf = path
        elif suffix in _RASTER_SUFFIXES and source_raster is None:
            source_raster = path

    if registered_hdf is not None:
        return registered_hdf, source_raster, layer_name, True

    terrain_dir_hdfs = sorted((project_dir / "Terrain").glob("*.hdf"))
    if terrain_dir_hdfs:
        return terrain_dir_hdfs[0], source_raster, layer_name, False

    if source_raster is not None:
        return project_dir / "Terrain" / f"{source_raster.stem}.hdf", source_raster, layer_name, False

    return project_dir / "Terrain" / "Terrain.hdf", source_raster, layer_name, False


def _preprocessor_result_to_dict(result: Any) -> dict[str, Any]:
    return {
        "success": bool(getattr(result, "success", False)),
        "plan_number": getattr(result, "plan_number", ""),
        "geometry_number": getattr(result, "geometry_number", None),
        "flow_type": getattr(result, "flow_type", "Unknown"),
        "elapsed_seconds": getattr(result, "elapsed_seconds", 0.0),
        "command": getattr(result, "command", ""),
        "return_code": getattr(result, "return_code", None),
        "signal_detected": getattr(result, "signal_detected", None),
        "compute_message_paths": [
            str(path) for path in getattr(result, "compute_message_paths", [])
        ],
        "artifact_paths": [str(path) for path in getattr(result, "artifact_paths", [])],
        "error_count": getattr(result, "error_count", 0),
        "warning_count": getattr(result, "warning_count", 0),
        "first_error_line": getattr(result, "first_error_line", None),
        "error": getattr(result, "error", None),
    }


def _mtime_utc(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
