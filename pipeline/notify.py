"""
notify.py — Webhook and email notifications for RAS Agent pipeline runs

Sends a JSON webhook POST and/or a plain-text SMTP email when a single
watershed run or a batch run completes.  Never raises on failure — all
errors are logged as warnings and the function returns False.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

from __future__ import annotations

import hashlib
import hmac
import json
import smtplib
import subprocess
from dataclasses import dataclass
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import TYPE_CHECKING, Optional

try:
    from loguru import logger
except ImportError:  # pragma: no cover - fallback for lean test environments
    import logging
    logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from batch import BatchResult
    from orchestrator import OrchestratorResult


# ── Git commit hash ────────────────────────────────────────────────────────────

_git_result = subprocess.run(
    ["git", "rev-parse", "--short", "HEAD"],
    capture_output=True,
    text=True,
    cwd=Path(__file__).parent.parent,
)
GIT_COMMIT = _git_result.stdout.strip() if _git_result.returncode == 0 else "unknown"


# ── Config ─────────────────────────────────────────────────────────────────────

@dataclass
class NotifyConfig:
    """Notification configuration for webhook and/or email delivery."""
    webhook_url: Optional[str] = None       # POST JSON payload to this URL on completion
    webhook_secret: Optional[str] = None    # Optional: add X-RAS-Agent-Signature header (HMAC-SHA256)
    email_to: Optional[str] = None          # Send summary email to this address
    email_from: Optional[str] = None        # From address (default: ras-agent@localhost)
    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_tls: bool = False
    on_complete: bool = True                # Notify on success
    on_failure: bool = True                 # Notify on failure
    on_partial: bool = True                 # Notify on partial completion


# ── Payload builders ───────────────────────────────────────────────────────────

def _build_run_payload(result: OrchestratorResult) -> dict:
    """Build webhook JSON payload for a single watershed run."""
    drainage_area_mi2 = None
    if result.watershed is not None:
        try:
            drainage_area_mi2 = result.watershed.characteristics.drainage_area_mi2
        except AttributeError:
            pass

    peak_flows: dict = {}
    if result.peak_flows is not None:
        pf = result.peak_flows
        for attr, key in [("Q10", "q10"), ("Q50", "q50"), ("Q100", "q100")]:
            val = getattr(pf, attr, None)
            if val is not None:
                peak_flows[key] = round(float(val), 1)

    return_periods = sorted(result.results.keys()) if result.results else []

    return {
        "event": "run_complete",
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "ras_agent_version": f"git:{GIT_COMMIT}",
        "run": {
            "name": result.name,
            "status": result.status,
            "duration_sec": round(result.duration_sec, 1),
            "pour_point": list(result.pour_point) if result.pour_point else None,
            "drainage_area_mi2": (
                round(drainage_area_mi2, 1) if drainage_area_mi2 is not None else None
            ),
            "return_periods": return_periods,
            "output_dir": str(result.output_dir),
        },
        "peak_flows": peak_flows,
        "errors": list(result.errors),
    }


def _build_batch_payload(batch_result: BatchResult) -> dict:
    """Build webhook JSON payload for a batch run."""
    failed_watersheds = list(batch_result.errors.keys())
    return {
        "event": "batch_complete",
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "ras_agent_version": f"git:{GIT_COMMIT}",
        "batch": {
            "input_file": batch_result.input_file.name,
            "total": batch_result.total,
            "completed": batch_result.completed,
            "failed": batch_result.failed,
            "skipped": batch_result.skipped,
            "duration_sec": round(batch_result.duration_sec, 1),
        },
        "failed_watersheds": failed_watersheds,
    }


# ── Email body builders ────────────────────────────────────────────────────────

def _build_run_email(result: OrchestratorResult) -> tuple[str, str]:
    """Return (subject, body) for a single-run email."""
    # Extract data
    drainage_area_mi2 = None
    if result.watershed is not None:
        try:
            drainage_area_mi2 = result.watershed.characteristics.drainage_area_mi2
        except AttributeError:
            pass

    peak_flows: dict = {}
    if result.peak_flows is not None:
        pf = result.peak_flows
        for attr, key in [("Q10", "q10"), ("Q50", "q50"), ("Q100", "q100")]:
            val = getattr(pf, attr, None)
            if val is not None:
                peak_flows[key] = round(float(val), 1)

    return_periods = sorted(result.results.keys()) if result.results else []
    max_rp = max(return_periods) if return_periods else None
    rp_label = f"{max_rp}yr" if max_rp is not None else "N/A"

    lon, lat = result.pour_point if result.pour_point else (None, None)
    report_path = result.output_dir / "report.html"

    subject = f"[RAS Agent] Run complete: {result.name} ({rp_label})"

    peak_lines = []
    for attr, label in [("q10", "Q10"), ("q50", "Q50"), ("q100", "Q100")]:
        val = peak_flows.get(attr)
        if val is not None:
            peak_lines.append(f"  {label}:  {val:,.0f} cfs")

    body_parts = [
        "RAS Agent Run Complete",
        "======================",
        f"Watershed: {result.name}",
        f"Status: {result.status}",
        f"Duration: {result.duration_sec:.1f}s",
        f"Pour Point: {lon}, {lat}" if lon is not None else "Pour Point: N/A",
        (
            f"Drainage Area: {drainage_area_mi2:.1f} mi\u00b2"
            if drainage_area_mi2 is not None else "Drainage Area: N/A"
        ),
        "",
        "Peak Flows:",
    ]
    body_parts.extend(peak_lines if peak_lines else ["  (not available)"])
    body_parts += [
        "",
        f"Output: {result.output_dir}",
        f"Report: {report_path}",
        "",
        "--",
        "RAS Agent (Apache 2.0) \u2014 Illinois State Water Survey / CHAMP",
        "https://github.com/gheistand/ras-agent",
    ]

    return subject, "\n".join(body_parts)


def _build_batch_email(batch_result: BatchResult) -> tuple[str, str]:
    """Return (subject, body) for a batch-run email."""
    subject = (
        f"[RAS Agent] Batch complete: "
        f"{batch_result.completed}/{batch_result.total} watersheds"
    )

    failed_lines = (
        [f"  - {name}: {msg}" for name, msg in batch_result.errors.items()]
        if batch_result.errors
        else ["  (none)"]
    )

    body_parts = [
        "RAS Agent Batch Complete",
        "========================",
        f"Input: {batch_result.input_file}",
        f"Total: {batch_result.total}",
        f"Completed: {batch_result.completed}",
        f"Failed: {batch_result.failed}",
        f"Skipped: {batch_result.skipped}",
        f"Duration: {batch_result.duration_sec:.1f}s",
        f"Summary CSV: {batch_result.summary_csv}",
        "",
        "Failed Watersheds:",
    ]
    body_parts.extend(failed_lines)
    body_parts += [
        "",
        "--",
        "RAS Agent (Apache 2.0) \u2014 Illinois State Water Survey / CHAMP",
        "https://github.com/gheistand/ras-agent",
    ]

    return subject, "\n".join(body_parts)


# ── Delivery helpers ───────────────────────────────────────────────────────────

def _send_webhook(payload: dict, config: NotifyConfig) -> bool:
    """POST payload to config.webhook_url. Returns True on success."""
    if not config.webhook_url:
        return False
    try:
        import requests  # third-party; confirmed in requirements.txt

        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if config.webhook_secret:
            sig = hmac.new(
                config.webhook_secret.encode(), body, hashlib.sha256
            ).hexdigest()
            headers["X-RAS-Agent-Signature"] = f"sha256={sig}"

        resp = requests.post(
            config.webhook_url, data=body, headers=headers, timeout=30
        )
        resp.raise_for_status()
        logger.info(
            f"[notify] Webhook OK → {config.webhook_url} "
            f"(HTTP {resp.status_code})"
        )
        return True
    except Exception as exc:
        logger.warning(f"[notify] Webhook failed: {exc}")
        return False


def _send_email(subject: str, body: str, config: NotifyConfig) -> bool:
    """Send plain-text email via smtplib. Returns True on success."""
    if not config.email_to:
        return False
    try:
        from_addr = config.email_from or "ras-agent@localhost"
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = config.email_to

        smtp = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10)
        try:
            if config.smtp_tls:
                smtp.starttls()
            if config.smtp_user and config.smtp_password:
                smtp.login(config.smtp_user, config.smtp_password)
            smtp.sendmail(from_addr, [config.email_to], msg.as_string())
        finally:
            smtp.quit()

        logger.info(f"[notify] Email sent → {config.email_to}")
        return True
    except Exception as exc:
        logger.warning(f"[notify] Email failed: {exc}")
        return False


# ── Public API ─────────────────────────────────────────────────────────────────

def notify_run_complete(result: OrchestratorResult, config: NotifyConfig) -> bool:
    """
    Send webhook and/or email notification for a completed run.

    Checks on_complete / on_failure / on_partial flags before sending.
    Returns True if at least one notification succeeded.
    Logs warnings on failure — never raises.
    """
    should_notify = (
        (result.status == "complete" and config.on_complete)
        or (result.status == "failed" and config.on_failure)
        or (result.status == "partial" and config.on_partial)
    )
    if not should_notify:
        logger.debug(
            f"[notify] Skipping run notification "
            f"(status={result.status}, config flags unchanged)"
        )
        return False

    payload = _build_run_payload(result)
    subject, body = _build_run_email(result)

    webhook_ok = _send_webhook(payload, config)
    email_ok = _send_email(subject, body, config)

    success = webhook_ok or email_ok
    if not success and (config.webhook_url or config.email_to):
        logger.warning(
            f"[notify] All notification channels failed for run {result.name!r}"
        )
    return success


def notify_batch_complete(batch_result: BatchResult, config: NotifyConfig) -> bool:
    """
    Send webhook and/or email notification for a completed batch run.

    Returns True if at least one notification succeeded.
    Logs warnings on failure — never raises.
    """
    payload = _build_batch_payload(batch_result)
    subject, body = _build_batch_email(batch_result)

    webhook_ok = _send_webhook(payload, config)
    email_ok = _send_email(subject, body, config)

    success = webhook_ok or email_ok
    if not success and (config.webhook_url or config.email_to):
        logger.warning("[notify] All notification channels failed for batch run")
    return success
