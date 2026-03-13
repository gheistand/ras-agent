"""
test_notify.py — Tests for pipeline/notify.py

All tests pass without HEC-RAS installed.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import notify as _notify
from notify import NotifyConfig, notify_batch_complete, notify_run_complete


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_peak_flows(q10=4200.0, q50=8100.0, q100=11200.0):
    pf = SimpleNamespace(Q10=q10, Q50=q50, Q100=q100)
    return pf


def _make_orchestrator_result(tmp_path, status="complete"):
    """Return a SimpleNamespace shaped like OrchestratorResult."""
    output_dir = tmp_path / "Sangamon_Monticello"
    output_dir.mkdir(parents=True, exist_ok=True)

    chars = SimpleNamespace(
        drainage_area_mi2=312.5,
        drainage_area_km2=809.4,
    )
    ws = SimpleNamespace(characteristics=chars)

    return SimpleNamespace(
        name="Sangamon_Monticello",
        pour_point=(-88.578, 40.021),
        output_dir=output_dir,
        status=status,
        duration_sec=142.3,
        watershed=ws,
        peak_flows=_make_peak_flows(),
        results={10: {"depth_grid": output_dir / "10yr" / "depth_grid.tif"},
                 50: {"depth_grid": output_dir / "50yr" / "depth_grid.tif"},
                 100: {"depth_grid": output_dir / "100yr" / "depth_grid.tif"}},
        errors=[],
    )


def _make_batch_result(tmp_path):
    """Return a SimpleNamespace shaped like BatchResult."""
    return SimpleNamespace(
        input_file=tmp_path / "watersheds.csv",
        output_dir=tmp_path / "out",
        total=12,
        completed=11,
        failed=1,
        skipped=0,
        results=[],
        errors={"Salt_Fork_Homer": "terrain download failed"},
        duration_sec=1842.0,
        summary_csv=tmp_path / "out" / "batch_summary.csv",
    )


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_notify_run_complete_webhook(tmp_path):
    """notify_run_complete() POSTs to webhook_url with correct payload keys."""
    result = _make_orchestrator_result(tmp_path)
    config = NotifyConfig(webhook_url="http://example.com/hook")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_resp) as mock_post:
        ok = notify_run_complete(result, config)

    assert ok is True
    mock_post.assert_called_once()

    call_kwargs = mock_post.call_args
    url = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("url", mock_post.call_args[0][0])
    assert url == "http://example.com/hook"

    # Decode and check payload keys
    body_bytes = mock_post.call_args[1]["data"]
    payload = json.loads(body_bytes)
    assert payload["event"] == "run_complete"
    assert "timestamp" in payload
    assert "ras_agent_version" in payload
    assert payload["run"]["name"] == "Sangamon_Monticello"
    assert payload["run"]["status"] == "complete"
    assert payload["run"]["duration_sec"] == pytest.approx(142.3, abs=0.1)
    assert payload["run"]["pour_point"] == [-88.578, 40.021]
    assert "q10" in payload["peak_flows"]
    assert "q100" in payload["peak_flows"]
    assert "errors" in payload


def test_notify_webhook_with_secret(tmp_path):
    """HMAC-SHA256 signature header is added when webhook_secret is set."""
    result = _make_orchestrator_result(tmp_path)
    config = NotifyConfig(
        webhook_url="http://example.com/hook",
        webhook_secret="supersecret",
    )

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_resp) as mock_post:
        ok = notify_run_complete(result, config)

    assert ok is True
    headers_sent = mock_post.call_args[1]["headers"]
    assert "X-RAS-Agent-Signature" in headers_sent
    sig_header = headers_sent["X-RAS-Agent-Signature"]
    assert sig_header.startswith("sha256=")

    # Verify the signature is correct
    import hashlib, hmac as _hmac
    body_bytes = mock_post.call_args[1]["data"]
    expected_sig = _hmac.new(
        "supersecret".encode(), body_bytes, hashlib.sha256
    ).hexdigest()
    assert sig_header == f"sha256={expected_sig}"


def test_notify_batch_complete_webhook(tmp_path):
    """notify_batch_complete() POSTs correct batch payload."""
    batch_result = _make_batch_result(tmp_path)
    config = NotifyConfig(webhook_url="http://example.com/batch-hook")

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.post", return_value=mock_resp) as mock_post:
        ok = notify_batch_complete(batch_result, config)

    assert ok is True
    mock_post.assert_called_once()

    body_bytes = mock_post.call_args[1]["data"]
    payload = json.loads(body_bytes)
    assert payload["event"] == "batch_complete"
    assert payload["batch"]["total"] == 12
    assert payload["batch"]["completed"] == 11
    assert payload["batch"]["failed"] == 1
    assert payload["batch"]["skipped"] == 0
    assert "Salt_Fork_Homer" in payload["failed_watersheds"]
    assert "timestamp" in payload
    assert "ras_agent_version" in payload


def test_notify_no_config_skipped(tmp_path):
    """When webhook_url is None, requests.post is never called."""
    result = _make_orchestrator_result(tmp_path)
    config = NotifyConfig(webhook_url=None, email_to=None)

    with patch("requests.post") as mock_post:
        ok = notify_run_complete(result, config)

    mock_post.assert_not_called()
    assert ok is False


def test_notify_webhook_failure_no_raise(tmp_path):
    """ConnectionError from requests.post is caught; function returns False without raising."""
    result = _make_orchestrator_result(tmp_path)
    config = NotifyConfig(webhook_url="http://unreachable.example.com/hook")

    with patch("requests.post", side_effect=ConnectionError("connection refused")):
        ok = notify_run_complete(result, config)

    assert ok is False
    # No exception propagated — test passing proves this
