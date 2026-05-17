"""
rog_config.py - Rain-on-Grid workflow configuration.

Defines the serializable configuration contract used by generalized RoG
workflow orchestration and Symphony runner audit trails.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - exercised only in lean environments
    Draft202012Validator = None


SCHEMA_VERSION = "ras-agent-rog-workflow-config/v1"
SCHEMA_PATH = Path(__file__).with_name("schemas") / "rog_workflow_config.schema.json"
DEFAULT_TARGET_CRS = "EPSG:5070"
DEFAULT_AEP_YEARS = [10, 50, 100, 500]
DEFAULT_DURATIONS_HOURS = [6.0, 12.0, 24.0]
DEFAULT_PRECIP_TIMESTEP_MINUTES = 15
DEFAULT_PEAK_POSITION_PERCENT = 50.0
DEFAULT_INFILTRATION_METHOD = "deficit_constant"
DEFAULT_MIN_DEPTH_THRESHOLD_FT = 0.5


class RogWorkflowConfigError(ValueError):
    """Raised when a RoG workflow config cannot be loaded or validated."""


@dataclass
class RogWorkflowConfig:
    """
    Configuration for generalized rain-on-grid workflow orchestration.

    ``precip_timestep_minutes`` is the precipitation hyetograph temporal
    resolution. It is not the HEC-RAS computational timestep, output interval,
    or mapping interval; those RAS model parameters are configured separately.
    MRMS event-based workflows typically use 60 minutes to match
    ``GaugeCorr_QPE_01H``. Synthetic Atlas 14 workflows commonly use 5-15
    minute hyetograph discretization.
    """

    aep_years: list[int] = field(default_factory=lambda: list(DEFAULT_AEP_YEARS))
    durations_hours: list[float] = field(
        default_factory=lambda: list(DEFAULT_DURATIONS_HOURS)
    )
    precip_timestep_minutes: int = DEFAULT_PRECIP_TIMESTEP_MINUTES
    peak_position_percent: float = DEFAULT_PEAK_POSITION_PERCENT
    infiltration_method: str = DEFAULT_INFILTRATION_METHOD
    min_depth_threshold_ft: float = DEFAULT_MIN_DEPTH_THRESHOLD_FT
    target_crs: Optional[str] = None
    calibration_gauges: Optional[list[dict[str, Any]]] = None
    mock: bool = False

    def __post_init__(self) -> None:
        values = _validated_fields(_fields_from_config(self), source="RogWorkflowConfig")
        for key, value in values.items():
            setattr(self, key, value)

    @property
    def effective_target_crs(self) -> str:
        """Return the runtime CRS when no explicit override is supplied."""
        return self.target_crs or DEFAULT_TARGET_CRS

    @property
    def plan_count(self) -> int:
        """Return the AEP x duration plan count implied by this config."""
        return len(self.aep_years) * len(self.durations_hours)

    def plan_matrix(self) -> list[dict[str, float | int]]:
        """Return one design-storm record for each AEP/duration pair."""
        return [
            {"aep_year": aep_year, "duration_hours": duration}
            for aep_year in self.aep_years
            for duration in self.durations_hours
        ]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable config dictionary."""
        return copy.deepcopy(_fields_from_config(self))

    def to_audit_dict(self) -> dict[str, Any]:
        """Return config plus derived audit fields for OrchestratorResult."""
        payload = self.to_dict()
        payload.update(
            {
                "schema_version": SCHEMA_VERSION,
                "effective_target_crs": self.effective_target_crs,
                "plan_count": self.plan_count,
            }
        )
        return payload


def load_config(path: str | Path) -> RogWorkflowConfig:
    """
    Load and validate a RoG workflow config from JSON, YAML, or YML.

    Missing fields are filled from :class:`RogWorkflowConfig` defaults. Empty
    files are treated as an empty config so a checked-in placeholder can still
    resolve to the standard workflow.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise RogWorkflowConfigError(f"Config file not found: {config_path}")
    if not config_path.is_file():
        raise RogWorkflowConfigError(f"Config path is not a file: {config_path}")

    suffix = config_path.suffix.lower()
    text = config_path.read_text(encoding="utf-8")
    try:
        if suffix == ".json":
            raw = json.loads(text.strip() or "{}")
        elif suffix in {".yaml", ".yml"}:
            raw = _load_yaml(text)
        else:
            raise RogWorkflowConfigError(
                f"Unsupported config file extension {suffix!r}; "
                "expected .json, .yaml, or .yml"
            )
    except RogWorkflowConfigError:
        raise
    except Exception as exc:
        raise RogWorkflowConfigError(
            f"Could not parse RoG workflow config {config_path}: {exc}"
        ) from exc

    return validate_config(raw, source=str(config_path))


def validate_config(
    config: RogWorkflowConfig | Mapping[str, Any] | None,
    *,
    source: str = "<in-memory>",
) -> RogWorkflowConfig:
    """
    Validate a config mapping or dataclass and return a normalized dataclass.

    Raises:
        RogWorkflowConfigError: with field-specific validation messages when the
        config is invalid.
    """
    if isinstance(config, RogWorkflowConfig):
        raw = config.to_dict()
    elif config is None:
        raw = {}
    elif isinstance(config, Mapping):
        raw = dict(config)
    else:
        raise RogWorkflowConfigError(
            f"Invalid RoG workflow config at {source}: root must be an object/mapping"
        )

    merged = _default_fields()
    merged.update(raw)
    values = _validated_fields(merged, source=source)
    return _config_from_validated_fields(values)


def _fields_from_config(config: RogWorkflowConfig) -> dict[str, Any]:
    return {
        "aep_years": config.aep_years,
        "durations_hours": config.durations_hours,
        "precip_timestep_minutes": config.precip_timestep_minutes,
        "peak_position_percent": config.peak_position_percent,
        "infiltration_method": config.infiltration_method,
        "min_depth_threshold_ft": config.min_depth_threshold_ft,
        "target_crs": config.target_crs,
        "calibration_gauges": config.calibration_gauges,
        "mock": config.mock,
    }


def _default_fields() -> dict[str, Any]:
    return {
        "aep_years": list(DEFAULT_AEP_YEARS),
        "durations_hours": list(DEFAULT_DURATIONS_HOURS),
        "precip_timestep_minutes": DEFAULT_PRECIP_TIMESTEP_MINUTES,
        "peak_position_percent": DEFAULT_PEAK_POSITION_PERCENT,
        "infiltration_method": DEFAULT_INFILTRATION_METHOD,
        "min_depth_threshold_ft": DEFAULT_MIN_DEPTH_THRESHOLD_FT,
        "target_crs": None,
        "calibration_gauges": None,
        "mock": False,
    }


def _config_from_validated_fields(values: Mapping[str, Any]) -> RogWorkflowConfig:
    """Construct a config after validation without re-running __post_init__."""
    config = object.__new__(RogWorkflowConfig)
    for key in _default_fields():
        setattr(config, key, copy.deepcopy(values[key]))
    return config


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validated_fields(raw: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise RogWorkflowConfigError(
            f"Invalid RoG workflow config at {source}: root must be an object/mapping"
        )

    data = copy.deepcopy(dict(raw))
    errors = []

    if Draft202012Validator is not None:
        try:
            validator = Draft202012Validator(_schema())
            for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
                errors.append(f"{_format_jsonschema_path(err.path)}: {err.message}")
        except Exception as exc:
            errors.append(f"schema: JSON schema validation failed: {exc}")

    errors.extend(_fallback_validation_errors(data))

    if errors:
        joined = "; ".join(errors)
        raise RogWorkflowConfigError(
            f"Invalid RoG workflow config at {source}: {joined}"
        )

    return {
        "aep_years": [int(value) for value in data["aep_years"]],
        "durations_hours": [float(value) for value in data["durations_hours"]],
        "precip_timestep_minutes": int(data["precip_timestep_minutes"]),
        "peak_position_percent": float(data["peak_position_percent"]),
        "infiltration_method": data["infiltration_method"],
        "min_depth_threshold_ft": float(data["min_depth_threshold_ft"]),
        "target_crs": data["target_crs"],
        "calibration_gauges": _normalized_gauges(data["calibration_gauges"]),
        "mock": data["mock"],
    }


def _format_jsonschema_path(path) -> str:
    parts = list(path)
    if not parts:
        return "root"
    rendered = str(parts[0])
    for part in parts[1:]:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += f".{part}"
    return rendered


def _fallback_validation_errors(data: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    allowed = set(_default_fields())
    for key in sorted(set(data) - allowed):
        errors.append(f"{key}: unknown field")

    for key in sorted(allowed - set(data)):
        errors.append(f"{key}: missing required field after defaults were applied")

    if "aep_years" in data:
        errors.extend(_positive_integer_list_errors("aep_years", data["aep_years"]))
    if "durations_hours" in data:
        errors.extend(_positive_number_list_errors("durations_hours", data["durations_hours"]))
    if "precip_timestep_minutes" in data:
        errors.extend(
            _integer_errors(
                "precip_timestep_minutes",
                data["precip_timestep_minutes"],
                minimum=1,
            )
        )
    if "peak_position_percent" in data:
        errors.extend(
            _number_errors(
                "peak_position_percent",
                data["peak_position_percent"],
                minimum=0.0,
                maximum=100.0,
            )
        )
    if "infiltration_method" in data:
        value = data["infiltration_method"]
        if not isinstance(value, str):
            errors.append("infiltration_method: expected string")
        elif not value.strip():
            errors.append("infiltration_method: must not be empty")
    if "min_depth_threshold_ft" in data:
        errors.extend(
            _number_errors(
                "min_depth_threshold_ft",
                data["min_depth_threshold_ft"],
                minimum=0.0,
            )
        )
    if "target_crs" in data:
        errors.extend(_target_crs_errors(data["target_crs"]))
    if "calibration_gauges" in data:
        errors.extend(_calibration_gauge_errors(data["calibration_gauges"]))
    if "mock" in data:
        value = data["mock"]
        if not isinstance(value, bool):
            errors.append("mock: expected boolean")

    if not errors:
        values = {
            "aep_years": [int(value) for value in data["aep_years"]],
            "durations_hours": [float(value) for value in data["durations_hours"]],
            "precip_timestep_minutes": int(data["precip_timestep_minutes"]),
        }
        errors.extend(_cross_field_errors(values))

    return errors


def _positive_integer_list_errors(field_name: str, value: Any) -> list[str]:
    errors = []
    if not isinstance(value, list):
        return [f"{field_name}: expected list"]
    if not value:
        errors.append(f"{field_name}: must contain at least one item")
    seen = set()
    for idx, item in enumerate(value):
        item_path = f"{field_name}[{idx}]"
        errors.extend(_integer_errors(item_path, item, minimum=1))
        if isinstance(item, int) and not isinstance(item, bool):
            if item in seen:
                errors.append(f"{item_path}: duplicate value {item}")
            seen.add(item)
    return errors


def _positive_number_list_errors(field_name: str, value: Any) -> list[str]:
    errors = []
    if not isinstance(value, list):
        return [f"{field_name}: expected list"]
    if not value:
        errors.append(f"{field_name}: must contain at least one item")
    seen = set()
    for idx, item in enumerate(value):
        item_path = f"{field_name}[{idx}]"
        errors.extend(_number_errors(item_path, item, exclusive_minimum=0.0))
        if _is_number(item):
            normalized = float(item)
            if normalized in seen:
                errors.append(f"{item_path}: duplicate value {normalized:g}")
            seen.add(normalized)
    return errors


def _integer_errors(field_name: str, value: Any, *, minimum: int | None = None) -> list[str]:
    if not isinstance(value, int) or isinstance(value, bool):
        return [f"{field_name}: expected integer"]
    errors = []
    if minimum is not None and value < minimum:
        errors.append(f"{field_name}: must be >= {minimum}")
    return errors


def _number_errors(
    field_name: str,
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: float | None = None,
) -> list[str]:
    if not _is_number(value):
        return [f"{field_name}: expected number"]
    number = float(value)
    errors = []
    if not math.isfinite(number):
        errors.append(f"{field_name}: must be finite")
    if minimum is not None and number < minimum:
        errors.append(f"{field_name}: must be >= {minimum:g}")
    if maximum is not None and number > maximum:
        errors.append(f"{field_name}: must be <= {maximum:g}")
    if exclusive_minimum is not None and number <= exclusive_minimum:
        errors.append(f"{field_name}: must be > {exclusive_minimum:g}")
    return errors


def _target_crs_errors(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, str):
        return ["target_crs: expected string or null"]
    if not value.strip():
        return ["target_crs: must not be empty"]

    try:
        from rasterio.crs import CRS

        CRS.from_user_input(value)
    except Exception as exc:
        return [f"target_crs: invalid CRS {value!r}: {exc}"]
    return []


def _calibration_gauge_errors(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return ["calibration_gauges: expected list or null"]
    errors = []
    required = {"site_id", "x", "y", "variable"}
    allowed = required
    for idx, gauge in enumerate(value):
        path = f"calibration_gauges[{idx}]"
        if not isinstance(gauge, Mapping):
            errors.append(f"{path}: expected object")
            continue
        missing = required - set(gauge)
        for key in sorted(missing):
            errors.append(f"{path}.{key}: missing required field")
        extra = set(gauge) - allowed
        for key in sorted(extra):
            errors.append(f"{path}.{key}: unknown field")
        for key in ("site_id", "variable"):
            if key in gauge:
                if not isinstance(gauge[key], str):
                    errors.append(f"{path}.{key}: expected string")
                elif not gauge[key].strip():
                    errors.append(f"{path}.{key}: must not be empty")
        for key in ("x", "y"):
            if key in gauge:
                errors.extend(_number_errors(f"{path}.{key}", gauge[key]))
    return errors


def _normalized_gauges(value: Any) -> Optional[list[dict[str, Any]]]:
    if value is None:
        return None
    return [
        {
            "site_id": gauge["site_id"],
            "x": float(gauge["x"]),
            "y": float(gauge["y"]),
            "variable": gauge["variable"],
        }
        for gauge in value
    ]


def _cross_field_errors(values: Mapping[str, Any]) -> list[str]:
    errors = []
    timestep = values["precip_timestep_minutes"]
    if timestep < 60 and 60 % timestep != 0:
        errors.append(
            "precip_timestep_minutes: sub-hourly timesteps must divide evenly into 60 minutes"
        )
    if timestep > 60 and timestep % 60 != 0:
        errors.append(
            "precip_timestep_minutes: multi-hour timesteps must be a whole-hour multiple"
        )
    return errors


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _load_yaml(text: str) -> Any:
    if not text.strip():
        return {}
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency declared
        raise RogWorkflowConfigError(
            "YAML config loading requires PyYAML. Install pipeline requirements "
            "before loading .yaml/.yml configs."
        ) from exc
    return yaml.safe_load(text) if text.strip() else {}
