"""
plan_matrix.py - AEP x duration design-storm plan matrix factory.

This module composes ras-commander precipitation, plan, unsteady-flow, and
permutation helpers to produce rain-on-grid HEC-RAS plan variants from an
existing base project. It intentionally stays orchestration-focused: reusable
HEC-RAS file editing remains in ras-commander, while ras-agent owns the RoG
design-storm matrix contract and audit manifest.
"""

from __future__ import annotations

import logging
import math
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

import pandas as pd

logger = logging.getLogger(__name__)


MAX_HECRAS_PLANS = 99
DEFAULT_MANIFEST_FILENAME = "design_storm_plan_matrix.csv"
DEFAULT_PRECIP_DIRNAME = "plan_matrix_precip"


class PlanMatrixError(RuntimeError):
    """Raised when a design-storm plan matrix cannot be generated."""


class _RasCommanderApis(NamedTuple):
    init_ras_project: Callable[..., Any]
    RasPlan: Any
    RasUnsteady: Any
    RasPermutation: Any
    AbmHyetographGrid: Any


@dataclass
class DesignStormSpec:
    """Configuration for an AEP x duration design-storm matrix."""

    aep_years: list[int] = field(default_factory=lambda: [10, 50, 100, 500])
    durations_hours: list[float] = field(default_factory=lambda: [6, 12, 24])
    timestep_minutes: int = 15
    peak_position_percent: float = 50.0
    time_of_concentration_hours: float = 0.0
    recession_factor: float = 1.5
    simulation_start: datetime = field(default_factory=lambda: datetime(2000, 1, 1, 0, 0))
    max_plans_per_batch: int = MAX_HECRAS_PLANS
    precipitation_dirname: str = DEFAULT_PRECIP_DIRNAME
    manifest_filename: str = DEFAULT_MANIFEST_FILENAME

    def validate(self) -> None:
        """Validate design-storm matrix settings before project mutation."""

        if not self.aep_years:
            raise ValueError("aep_years must contain at least one return period")
        if not self.durations_hours:
            raise ValueError("durations_hours must contain at least one duration")

        aeps = [int(aep) for aep in self.aep_years]
        if any(aep <= 0 for aep in aeps):
            raise ValueError("aep_years values must be positive integers")

        durations = [float(duration) for duration in self.durations_hours]
        if any(not math.isfinite(duration) or duration <= 0 for duration in durations):
            raise ValueError("durations_hours values must be positive finite hours")

        if int(self.timestep_minutes) <= 0:
            raise ValueError("timestep_minutes must be greater than zero")
        for duration in durations:
            duration_minutes = duration * 60.0
            intervals = duration_minutes / float(self.timestep_minutes)
            if abs(intervals - round(intervals)) > 1e-9:
                raise ValueError(
                    f"timestep_minutes={self.timestep_minutes} does not evenly divide "
                    f"duration_hours={duration:g}"
                )

        if not (0.0 <= float(self.peak_position_percent) <= 100.0):
            raise ValueError("peak_position_percent must be between 0 and 100")
        if float(self.time_of_concentration_hours) < 0.0:
            raise ValueError("time_of_concentration_hours cannot be negative")
        if float(self.recession_factor) < 0.0:
            raise ValueError("recession_factor cannot be negative")
        if int(self.max_plans_per_batch) <= 0:
            raise ValueError("max_plans_per_batch must be greater than zero")
        if not str(self.precipitation_dirname).strip():
            raise ValueError("precipitation_dirname cannot be blank")
        if not str(self.manifest_filename).strip():
            raise ValueError("manifest_filename cannot be blank")


def generate_plan_matrix(
    project_dir: str | Path,
    base_plan: str | int,
    storm_spec: DesignStormSpec | None = None,
    bounds: Sequence[float] | None = None,
    *,
    ras_object: Any = None,
    ras_version: str | None = None,
    overwrite_batch_projects: bool = True,
) -> pd.DataFrame:
    """
    Generate an AEP x duration matrix of HEC-RAS design-storm plan variants.

    Parameters
    ----------
    project_dir:
        Existing HEC-RAS project directory containing the base plan.
    base_plan:
        Base HEC-RAS plan number, e.g. ``"01"``.
    storm_spec:
        Design storm matrix configuration. Defaults to 4 AEPs x 3 durations.
    bounds:
        WGS84 bounding box ``(west, south, east, north)`` passed to
        ``AbmHyetographGrid.generate()``.
    ras_object:
        Optional initialized ras-commander project object. Primarily useful for
        tests and callers that already initialized the project.
    ras_version:
        Optional ras-commander version/executable hint for project
        initialization when ``ras_object`` is not provided.
    overwrite_batch_projects:
        When the matrix exceeds the per-project plan limit, sibling batch
        folders are created. Existing batch folders are replaced by default.

    Returns
    -------
    pandas.DataFrame
        Manifest rows mapping each generated plan number to AEP, duration,
        NetCDF path, unsteady file, simulation dates, and batch folder.
    """

    spec = storm_spec or DesignStormSpec()
    spec.validate()
    if bounds is None:
        raise ValueError("bounds must be provided as (west, south, east, north)")
    bbox = _validate_bounds(bounds)

    project_path = Path(project_dir).resolve()
    if not project_path.exists():
        raise FileNotFoundError(f"project_dir does not exist: {project_path}")

    apis = _load_ras_commander()
    base_ras = ras_object or _initialize_project(
        apis.init_ras_project,
        project_path,
        ras_version=ras_version,
    )

    matrix_df = _build_matrix_dataframe(spec)
    existing_plan_count = _existing_plan_count(base_ras)
    effective_batch_size = min(
        MAX_HECRAS_PLANS - existing_plan_count,
        int(spec.max_plans_per_batch),
        MAX_HECRAS_PLANS,
    )
    if effective_batch_size <= 0:
        raise PlanMatrixError(
            "Base project already has 99 plans; cannot add design-storm variants"
        )

    batches = apis.RasPermutation._partition_dataframe(
        matrix_df,
        effective_batch_size,
    )
    if not batches:
        raise PlanMatrixError("No design-storm rows were produced")

    manifest_rows: list[dict[str, Any]] = []
    multi_batch = len(batches) > 1

    for batch_index, batch_df in enumerate(batches, start=1):
        if multi_batch:
            working_project = _prepare_batch_project(
                source_project=project_path,
                batch_index=batch_index,
                overwrite=overwrite_batch_projects,
            )
            batch_ras = _initialize_project(
                apis.init_ras_project,
                working_project,
                ras_version=ras_version,
            )
        else:
            working_project = project_path
            batch_ras = base_ras

        logger.info(
            "Generating design-storm plan batch %s/%s in %s",
            batch_index,
            len(batches),
            working_project,
        )

        batch_rows = _generate_batch(
            batch_df=batch_df,
            batch_index=batch_index,
            project_dir=working_project,
            base_plan=base_plan,
            spec=spec,
            bounds=bbox,
            ras_object=batch_ras,
            apis=apis,
        )
        manifest_rows.extend(batch_rows)

        batch_manifest = pd.DataFrame(batch_rows)
        _write_manifest_csv(batch_manifest, working_project / spec.manifest_filename)

    manifest = pd.DataFrame(manifest_rows)
    manifest_path = project_path / spec.manifest_filename
    if multi_batch:
        manifest_path = project_path / f"master_{spec.manifest_filename}"
    _write_manifest_csv(manifest, manifest_path)
    manifest.attrs["manifest_csv"] = str(manifest_path)
    manifest.attrs["batch_count"] = len(batches)
    manifest.attrs["max_plans_per_batch"] = effective_batch_size

    logger.info(
        "Generated %s design-storm plan variants across %s batch(es); manifest=%s",
        len(manifest.index),
        len(batches),
        manifest_path,
    )
    return manifest


def _load_ras_commander() -> _RasCommanderApis:
    try:
        from ras_commander import init_ras_project
        from ras_commander.RasPermutation import RasPermutation
        from ras_commander.RasPlan import RasPlan
        from ras_commander.RasUnsteady import RasUnsteady
        from ras_commander.precip.AbmHyetographGrid import AbmHyetographGrid
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ImportError(
            "ras-commander>=0.95.0 with precipitation helpers is required for "
            "design-storm plan matrix generation"
        ) from exc

    return _RasCommanderApis(
        init_ras_project=init_ras_project,
        RasPlan=RasPlan,
        RasUnsteady=RasUnsteady,
        RasPermutation=RasPermutation,
        AbmHyetographGrid=AbmHyetographGrid,
    )


def _initialize_project(
    init_ras_project: Callable[..., Any],
    project_dir: Path,
    *,
    ras_version: str | None,
) -> Any:
    return init_ras_project(
        project_dir,
        ras_version,
        load_results_summary=False,
    )


def _validate_bounds(bounds: Sequence[float]) -> tuple[float, float, float, float]:
    if len(bounds) != 4:
        raise ValueError("bounds must be a 4-tuple: (west, south, east, north)")
    west, south, east, north = (float(value) for value in bounds)
    if not all(math.isfinite(value) for value in (west, south, east, north)):
        raise ValueError("bounds contains a non-finite coordinate")
    if west >= east or south >= north:
        raise ValueError("bounds must be ordered west < east and south < north")
    if west < -180 or east > 180 or south < -90 or north > 90:
        raise ValueError("bounds must be in WGS84 longitude/latitude coordinates")
    return west, south, east, north


def _build_matrix_dataframe(spec: DesignStormSpec) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    matrix_id = 1
    for aep_years in spec.aep_years:
        for duration_hours in spec.durations_hours:
            short_id = _plan_short_identifier(int(aep_years), float(duration_hours))
            rows.append(
                {
                    "matrix_id": matrix_id,
                    "aep_years": int(aep_years),
                    "duration_hours": float(duration_hours),
                    "plan_short_id": short_id,
                    "plan_title": _plan_title(short_id),
                }
            )
            matrix_id += 1
    return pd.DataFrame(rows)


def _generate_batch(
    *,
    batch_df: pd.DataFrame,
    batch_index: int,
    project_dir: Path,
    base_plan: str | int,
    spec: DesignStormSpec,
    bounds: tuple[float, float, float, float],
    ras_object: Any,
    apis: _RasCommanderApis,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source_unsteady_number = _base_unsteady_number(
        base_plan,
        apis.RasPlan,
        ras_object,
    )
    precipitation_dir = project_dir / spec.precipitation_dirname
    precipitation_dir.mkdir(parents=True, exist_ok=True)

    for batch_plan_index, (_, row) in enumerate(batch_df.iterrows(), start=1):
        aep_years = int(row["aep_years"])
        duration_hours = float(row["duration_hours"])
        short_id = str(row["plan_short_id"])
        plan_title = str(row["plan_title"])
        netcdf_path = precipitation_dir / f"{short_id}.nc"

        generated_netcdf = Path(
            apis.AbmHyetographGrid.generate(
                bounds=bounds,
                ari_years=aep_years,
                storm_duration_hours=duration_hours,
                timestep_minutes=int(spec.timestep_minutes),
                peak_position_percent=float(spec.peak_position_percent),
                output_netcdf=netcdf_path,
            )
        )

        unsteady_number, unsteady_file = _clone_unsteady_file(
            source_unsteady_number=source_unsteady_number,
            short_id=short_id,
            title=plan_title,
            ras_object=ras_object,
        )
        plan_number = apis.RasPlan.clone_plan(
            base_plan,
            new_plan_shortid=short_id,
            new_title=plan_title,
            unsteady_flow=unsteady_number,
            intervals={
                "output": _hecras_minutes_interval(int(spec.timestep_minutes)),
                "mapping": "1HOUR",
            },
            description=(
                f"Design storm matrix variant: {aep_years}-year, "
                f"{duration_hours:g}-hour ABM rain-on-grid."
            ),
            ras_object=ras_object,
        )

        apis.RasUnsteady.set_gridded_precipitation(
            unsteady_number,
            generated_netcdf,
            ras_object=ras_object,
        )

        simulation_start = spec.simulation_start
        recession_padding_hours = float(spec.recession_factor) * float(
            spec.time_of_concentration_hours
        )
        simulation_end = simulation_start + timedelta(
            hours=duration_hours + recession_padding_hours
        )
        apis.RasPlan.update_simulation_date(
            plan_number,
            simulation_start,
            simulation_end,
            ras_object=ras_object,
        )
        apis.RasPlan.update_plan_intervals(
            plan_number,
            output_interval=_hecras_minutes_interval(int(spec.timestep_minutes)),
            mapping_interval="1HOUR",
            ras_object=ras_object,
        )

        rows.append(
            {
                "matrix_id": int(row["matrix_id"]),
                "batch_index": int(batch_index),
                "batch_plan_index": int(batch_plan_index),
                "project_dir": str(project_dir),
                "plan_number": str(plan_number).zfill(2),
                "plan_short_id": short_id,
                "plan_title": plan_title,
                "unsteady_number": str(unsteady_number).zfill(2),
                "unsteady_file": str(unsteady_file),
                "aep_years": aep_years,
                "duration_hours": duration_hours,
                "timestep_minutes": int(spec.timestep_minutes),
                "peak_position_percent": float(spec.peak_position_percent),
                "simulation_start": simulation_start.isoformat(),
                "simulation_end": simulation_end.isoformat(),
                "time_of_concentration_hours": float(spec.time_of_concentration_hours),
                "recession_padding_hours": recession_padding_hours,
                "netcdf_path": str(generated_netcdf),
            }
        )

    return rows


def _prepare_batch_project(
    *,
    source_project: Path,
    batch_index: int,
    overwrite: bool,
) -> Path:
    batch_dir = source_project.parent / (
        f"{source_project.name}_design_storms_{batch_index:03d}"
    )
    resolved_batch = batch_dir.resolve()
    resolved_source = source_project.resolve()
    if (
        resolved_batch == resolved_source
        or resolved_batch.parent != resolved_source.parent
        or not resolved_batch.name.startswith(f"{resolved_source.name}_design_storms_")
    ):
        raise PlanMatrixError(f"Refusing unsafe batch project path: {batch_dir}")

    if batch_dir.exists():
        if not overwrite:
            raise FileExistsError(f"Batch project already exists: {batch_dir}")
        shutil.rmtree(batch_dir)
    shutil.copytree(source_project, batch_dir, ignore=shutil.ignore_patterns("*.bak"))
    return batch_dir


def _base_unsteady_number(base_plan: str | int, RasPlan: Any, ras_object: Any) -> str:
    flow_file = RasPlan.get_plan_value(base_plan, "Flow File", ras_object=ras_object)
    if not flow_file:
        raise PlanMatrixError(f"Base plan {base_plan!r} does not have a Flow File entry")
    match = re.fullmatch(r"[uU](\d{1,2})", str(flow_file).strip())
    if not match:
        raise PlanMatrixError(
            f"Base plan {base_plan!r} must reference an unsteady flow file; got {flow_file!r}"
        )
    return match.group(1).zfill(2)


def _clone_unsteady_file(
    *,
    source_unsteady_number: str,
    short_id: str,
    title: str,
    ras_object: Any,
) -> tuple[str, Path]:
    project_dir = Path(ras_object.project_folder)
    project_name = str(ras_object.project_name)
    source_number = str(source_unsteady_number).zfill(2)
    source_path = project_dir / f"{project_name}.u{source_number}"
    if not source_path.exists():
        raise FileNotFoundError(f"Source unsteady flow file not found: {source_path}")

    existing_numbers = _existing_unsteady_numbers(ras_object)
    new_number = _next_ras_number(existing_numbers)
    target_path = project_dir / f"{project_name}.u{new_number}"
    if target_path.exists():
        raise FileExistsError(f"Target unsteady flow file already exists: {target_path}")

    shutil.copy2(source_path, target_path)
    _update_unsteady_title(target_path, title or short_id)

    source_hdf = Path(str(source_path) + ".hdf")
    if source_hdf.exists():
        shutil.copy2(source_hdf, Path(str(target_path) + ".hdf"))

    _append_project_file_entry(Path(ras_object.prj_file), "Unsteady", f"u{new_number}")
    _refresh_project_entries(ras_object)
    return new_number, target_path


def _existing_plan_count(ras_object: Any) -> int:
    if hasattr(ras_object, "get_plan_entries"):
        plan_df = ras_object.get_plan_entries()
    else:
        plan_df = getattr(ras_object, "plan_df", pd.DataFrame())
    return int(len(plan_df.index))


def _existing_unsteady_numbers(ras_object: Any) -> list[str]:
    if hasattr(ras_object, "get_unsteady_entries"):
        unsteady_df = ras_object.get_unsteady_entries()
    else:
        unsteady_df = getattr(ras_object, "unsteady_df", pd.DataFrame())
    if unsteady_df is None or unsteady_df.empty:
        return []
    if "unsteady_number" in unsteady_df.columns:
        return [str(value).zfill(2) for value in unsteady_df["unsteady_number"].tolist()]
    return []


def _refresh_project_entries(ras_object: Any) -> None:
    refresh_map = {
        "plan_df": "get_plan_entries",
        "unsteady_df": "get_unsteady_entries",
        "flow_df": "get_flow_entries",
        "geom_df": "get_geom_entries",
    }
    for attr, method_name in refresh_map.items():
        method = getattr(ras_object, method_name, None)
        if callable(method):
            try:
                setattr(ras_object, attr, method())
            except Exception:
                logger.debug("Could not refresh %s via %s", attr, method_name, exc_info=True)


def _append_project_file_entry(prj_file: Path, entry_type: str, file_id: str) -> None:
    line = f"{entry_type} File={file_id}\n"
    needs_leading_newline = False
    if prj_file.exists():
        content = prj_file.read_text(encoding="utf-8", errors="ignore")
        lines = content.splitlines()
        if any(existing.strip().lower() == line.strip().lower() for existing in lines):
            return
        needs_leading_newline = bool(content and not content.endswith(("\n", "\r")))
    with prj_file.open("a", encoding="utf-8") as handle:
        if needs_leading_newline:
            handle.write("\n")
        handle.write(line)


def _update_unsteady_title(unsteady_path: Path, title: str) -> None:
    lines = unsteady_path.read_text(encoding="utf-8", errors="ignore").splitlines(True)
    replacement = f"Flow Title={title[:80]}\n"
    for index, line in enumerate(lines):
        if line.startswith("Flow Title="):
            lines[index] = replacement
            break
    else:
        lines.insert(0, replacement)
    unsteady_path.write_text("".join(lines), encoding="utf-8")


def _next_ras_number(existing_numbers: Sequence[str]) -> str:
    existing = sorted(int(str(number)) for number in existing_numbers if str(number).isdigit())
    next_number = 1
    for number in existing:
        if number == next_number:
            next_number += 1
        elif number > next_number:
            break
    if next_number > MAX_HECRAS_PLANS:
        raise PlanMatrixError("No available HEC-RAS file numbers remain")
    return f"{next_number:02d}"


def _plan_short_identifier(aep_years: int, duration_hours: float) -> str:
    duration_label = _duration_label(duration_hours)
    short_id = f"Q{int(aep_years)}_{duration_label}"
    safe = re.sub(r"[^A-Za-z0-9_]", "_", short_id)
    return safe[:24]


def _duration_label(duration_hours: float) -> str:
    if float(duration_hours).is_integer():
        return f"{int(duration_hours)}H"
    text = f"{duration_hours:g}".replace(".", "P")
    return f"{text}H"


def _plan_title(short_id: str) -> str:
    title = f"{short_id} Design Storm"
    return title[:32]


def _hecras_minutes_interval(minutes: int) -> str:
    minutes = int(minutes)
    if minutes <= 0:
        raise ValueError("minutes must be greater than zero")
    if minutes < 60:
        return f"{minutes}MIN"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1HOUR" if hours == 1 else f"{hours}HOUR"
    return f"{minutes}MIN"


def _write_manifest_csv(manifest: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest.to_csv(output_path, index=False)
