"""
test_rog_config.py - Tests for Rain-on-Grid workflow configuration.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import rog_config as rog_config_module  # noqa: E402
from rog_config import (  # noqa: E402
    DEFAULT_TARGET_CRS,
    RogWorkflowConfig,
    RogWorkflowConfigError,
    SCHEMA_PATH,
    load_config,
    validate_config,
)


def test_defaults_define_standard_twelve_plan_workflow():
    cfg = RogWorkflowConfig()

    assert cfg.aep_years == [10, 50, 100, 500]
    assert cfg.durations_hours == [6.0, 12.0, 24.0]
    assert cfg.precip_timestep_minutes == 15
    assert cfg.peak_position_percent == 50.0
    assert cfg.infiltration_method == "deficit_constant"
    assert cfg.min_depth_threshold_ft == 0.5
    assert cfg.target_crs is None
    assert cfg.effective_target_crs == DEFAULT_TARGET_CRS
    assert cfg.calibration_gauges is None
    assert cfg.mock is False
    assert cfg.plan_count == 12
    assert len(cfg.plan_matrix()) == 12


def test_load_config_from_json_with_defaults_and_gauges(tmp_path):
    path = tmp_path / "rog_config.json"
    path.write_text(
        json.dumps(
            {
                "aep_years": [25, 100],
                "durations_hours": [3, 6.5],
                "target_crs": "EPSG:26916",
                "calibration_gauges": [
                    {
                        "site_id": "05512345",
                        "x": 301000.25,
                        "y": 4410000.5,
                        "variable": "stage_ft",
                    }
                ],
                "mock": True,
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert cfg.aep_years == [25, 100]
    assert cfg.durations_hours == [3.0, 6.5]
    assert cfg.precip_timestep_minutes == 15
    assert cfg.target_crs == "EPSG:26916"
    assert cfg.calibration_gauges[0]["site_id"] == "05512345"
    assert cfg.mock is True
    assert cfg.plan_count == 4


def test_load_config_from_yaml(tmp_path):
    path = tmp_path / "rog_config.yaml"
    path.write_text(
        """
aep_years: [10, 50, 100]
durations_hours:
  - 6
  - 24
precip_timestep_minutes: 30
peak_position_percent: 45
min_depth_threshold_ft: 0.25
infiltration_method: deficit_constant
mock: true
""".lstrip(),
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert cfg.aep_years == [10, 50, 100]
    assert cfg.durations_hours == [6.0, 24.0]
    assert cfg.precip_timestep_minutes == 30
    assert cfg.peak_position_percent == 45.0
    assert cfg.min_depth_threshold_ft == 0.25
    assert cfg.mock is True
    assert cfg.plan_count == 6


def test_validate_config_rejects_invalid_values_with_field_paths():
    with pytest.raises(RogWorkflowConfigError) as exc_info:
        validate_config(
            {
                "aep_years": [0],
                "durations_hours": [],
                "precip_timestep_minutes": 45,
                "peak_position_percent": 125,
                "unknown_field": "bad",
            }
        )

    message = str(exc_info.value)
    assert "aep_years[0]" in message
    assert "durations_hours" in message
    assert "peak_position_percent" in message
    assert "unknown_field" in message


def test_validate_config_rejects_legacy_timestep_field():
    with pytest.raises(RogWorkflowConfigError) as exc_info:
        validate_config({"timestep_minutes": 15})

    assert "timestep_minutes: unknown field" in str(exc_info.value)


def test_validate_config_rejects_invalid_calibration_gauge():
    with pytest.raises(RogWorkflowConfigError) as exc_info:
        validate_config(
            {
                "calibration_gauges": [
                    {
                        "site_id": "05512345",
                        "x": 301000.25,
                        "y": 4410000.5,
                    }
                ],
            }
        )

    message = str(exc_info.value)
    assert "calibration_gauges[0]" in message
    assert "variable" in message


def test_validate_config_rejects_invalid_target_crs():
    with pytest.raises(RogWorkflowConfigError) as exc_info:
        validate_config({"target_crs": "not-a-crs"})

    assert "target_crs" in str(exc_info.value)
    assert "not-a-crs" in str(exc_info.value)


def test_fallback_validation_rejects_wrong_types_with_clear_errors(monkeypatch):
    monkeypatch.setattr(rog_config_module, "Draft202012Validator", None)

    with pytest.raises(RogWorkflowConfigError) as exc_info:
        validate_config(
            {
                "aep_years": "10,50,100",
                "durations_hours": ["6"],
                "precip_timestep_minutes": "15",
                "mock": "true",
            }
        )

    message = str(exc_info.value)
    assert "aep_years" in message
    assert "durations_hours[0]" in message
    assert "precip_timestep_minutes" in message
    assert "mock" in message


def test_fallback_validation_rejects_peak_position_bounds(monkeypatch):
    monkeypatch.setattr(rog_config_module, "Draft202012Validator", None)

    with pytest.raises(RogWorkflowConfigError) as exc_info:
        validate_config({"peak_position_percent": 125})

    message = str(exc_info.value)
    assert "peak_position_percent" in message
    assert "100" in message


def test_validate_config_does_not_revalidate_after_normalization(monkeypatch):
    calls = []
    original = rog_config_module._validated_fields

    def wrapped(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(rog_config_module, "_validated_fields", wrapped)

    cfg = validate_config({"mock": True})

    assert cfg.mock is True
    assert len(calls) == 1


def test_config_audit_dict_is_json_serializable():
    cfg = validate_config({"mock": True})
    audit_payload = cfg.to_audit_dict()

    encoded = json.dumps(audit_payload, sort_keys=True)
    decoded = json.loads(encoded)

    assert decoded["schema_version"] == "ras-agent-rog-workflow-config/v1"
    assert decoded["effective_target_crs"] == "EPSG:5070"
    assert decoded["plan_count"] == 12
    assert decoded["precip_timestep_minutes"] == 15
    assert decoded["mock"] is True


def test_schema_file_is_valid_json_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["title"] == "RAS Agent Rain-on-Grid Workflow Configuration"
    assert schema["properties"]["aep_years"]["default"] == [10, 50, 100, 500]
    assert schema["properties"]["durations_hours"]["default"] == [6, 12, 24]
    assert "timestep_minutes" not in schema["properties"]
    precip_timestep = schema["properties"]["precip_timestep_minutes"]
    assert precip_timestep["default"] == 15
    assert "not the HEC-RAS computational timestep" in precip_timestep["description"]
