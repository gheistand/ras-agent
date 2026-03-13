"""
batch.py — Batch processing: runs multiple watersheds from a CSV/JSON input file

Loads a watershed spec file (CSV or JSON), runs the full RAS Agent pipeline for
each watershed via concurrent.futures.ThreadPoolExecutor, and writes a summary CSV.

Supports idempotent resume mode: watersheds with an existing run_metadata.json
(status=complete) are skipped without re-running.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import csv
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

import orchestrator as _orchestrator
from orchestrator import OrchestratorResult, run_watershed


# ── Git commit hash ────────────────────────────────────────────────────────────

_git_result = subprocess.run(
    ["git", "rev-parse", "--short", "HEAD"],
    capture_output=True,
    text=True,
    cwd=Path(__file__).parent.parent,
)
GIT_COMMIT = _git_result.stdout.strip() if _git_result.returncode == 0 else "unknown"


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class WatershedSpec:
    """Specification for a single watershed pipeline run."""
    name: str
    lon: float
    lat: float
    return_periods: list
    notes: str = ""


@dataclass
class BatchResult:
    """Aggregate result for a batch of watershed pipeline runs."""
    input_file: Path
    output_dir: Path
    total: int
    completed: int
    failed: int
    skipped: int                       # already had results on disk — idempotent
    results: list                      # list[OrchestratorResult]
    errors: dict                       # {watershed_name: error_message}
    duration_sec: float
    summary_csv: Path                  # path to written summary CSV


# ── Spec Loading ───────────────────────────────────────────────────────────────

def _parse_return_periods(value) -> list:
    """Parse return_periods from a string ("10,50,100"), int, or list."""
    if isinstance(value, list):
        return [int(v) for v in value]
    if isinstance(value, int):
        return [value]
    # string: "10,50,100" or "100"
    return [int(v.strip()) for v in str(value).split(",") if v.strip()]


def load_watershed_specs(input_file: Path) -> list:
    """
    Load CSV or JSON watershed spec file. Auto-detects format by extension.

    Args:
        input_file: Path to CSV or JSON watershed spec file

    Returns:
        List of WatershedSpec objects
    """
    input_file = Path(input_file)
    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")

    suffix = input_file.suffix.lower()

    if suffix == ".json":
        with input_file.open() as fh:
            records = json.load(fh)
        specs = []
        for rec in records:
            specs.append(WatershedSpec(
                name=rec["name"],
                lon=float(rec["lon"]),
                lat=float(rec["lat"]),
                return_periods=_parse_return_periods(rec["return_periods"]),
                notes=rec.get("notes", ""),
            ))
        logger.info(f"Loaded {len(specs)} watershed specs from JSON: {input_file}")
        return specs

    elif suffix == ".csv":
        specs = []
        with input_file.open(newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                specs.append(WatershedSpec(
                    name=row["name"].strip(),
                    lon=float(row["lon"]),
                    lat=float(row["lat"]),
                    return_periods=_parse_return_periods(row["return_periods"]),
                    notes=row.get("notes", "").strip(),
                ))
        logger.info(f"Loaded {len(specs)} watershed specs from CSV: {input_file}")
        return specs

    else:
        raise ValueError(
            f"Unsupported input file format: {suffix!r} (expected .csv or .json)"
        )


# ── Resume / Idempotency ───────────────────────────────────────────────────────

def _metadata_path(output_dir: Path, name: str) -> Path:
    return output_dir / name / "run_metadata.json"


def _is_already_complete(output_dir: Path, name: str) -> bool:
    """Return True if run_metadata.json exists with status='complete'."""
    meta = _metadata_path(output_dir, name)
    if not meta.exists():
        return False
    try:
        with meta.open() as fh:
            data = json.load(fh)
        return data.get("status") == "complete"
    except Exception:
        return False


def _write_run_metadata(
    spec: WatershedSpec,
    result: OrchestratorResult,
    duration_sec: float,
) -> None:
    """Write run_metadata.json to the watershed output directory."""
    ws_dir = result.output_dir
    ws_dir.mkdir(parents=True, exist_ok=True)

    # Extract drainage area
    drainage_area_mi2 = None
    if result.watershed is not None:
        try:
            drainage_area_mi2 = result.watershed.characteristics.drainage_area_mi2
        except AttributeError:
            pass

    # Extract peak flows
    peak_flows: dict = {}
    if result.peak_flows is not None:
        pf = result.peak_flows
        for attr, key in [("Q10", "q10"), ("Q50", "q50"), ("Q100", "q100")]:
            val = getattr(pf, attr, None)
            if val is not None:
                peak_flows[key] = round(float(val), 1)

    # Extract output files
    output_files: dict = {}
    for rp, file_dict in result.results.items():
        for fname, path in file_dict.items():
            output_files[f"{fname}_{rp}yr"] = str(path)

    metadata = {
        "name": spec.name,
        "pour_point": [spec.lon, spec.lat],
        "return_periods": spec.return_periods,
        "status": result.status,
        "duration_sec": round(duration_sec, 2),
        "drainage_area_mi2": (
            round(drainage_area_mi2, 2) if drainage_area_mi2 is not None else None
        ),
        "peak_flows": peak_flows,
        "output_files": output_files,
        "ras_agent_commit": GIT_COMMIT,
        "run_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    }

    meta_path = ws_dir / "run_metadata.json"
    with meta_path.open("w") as fh:
        json.dump(metadata, fh, indent=2)
    logger.debug(f"Wrote run_metadata.json: {meta_path}")


# ── Core Batch Runner ──────────────────────────────────────────────────────────

def run_batch(
    input_file: Path,
    output_dir: Path,
    max_workers: int = 3,
    resolution_m: float = 3.0,
    mesh_strategy: str = "template_clone",
    ras_exe_dir: Optional[Path] = None,
    resume: bool = True,
    dry_run: bool = False,
    notify_config=None,        # Optional[NotifyConfig] — see pipeline/notify.py
) -> BatchResult:
    """
    Run full pipeline for each watershed in input_file.

    Uses concurrent.futures.ThreadPoolExecutor for parallel execution.
    Each watershed runs in its own subdirectory: output_dir/{watershed_name}/

    If resume=True, skip any watershed where output_dir/{name}/ already exists
    and contains a completed run_metadata.json.

    Args:
        input_file:     CSV or JSON watershed spec file
        output_dir:     Root output directory
        max_workers:    ThreadPoolExecutor concurrency limit
        resolution_m:   DEM resolution in meters
        mesh_strategy:  HEC-RAS mesh build strategy
        ras_exe_dir:    Path to RasUnsteady binary dir; None = mock mode
        resume:         Skip watersheds with existing completed output
        dry_run:        Load + validate specs, print plan, exit without running
        notify_config:  Optional NotifyConfig for per-watershed + batch notifications

    Returns:
        BatchResult with per-watershed results and summary CSV path
    """
    input_file = Path(input_file)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    specs = load_watershed_specs(input_file)
    total = len(specs)
    logger.info(
        f"Batch: {total} watersheds from {input_file.name}, "
        f"output_dir={output_dir}, max_workers={max_workers}, "
        f"resume={resume}, dry_run={dry_run}"
    )

    summary_csv = output_dir / "batch_summary.csv"

    if dry_run:
        logger.info("Dry-run mode — plan:")
        for i, spec in enumerate(specs, 1):
            already = _is_already_complete(output_dir, spec.name) if resume else False
            tag = " [SKIP — complete]" if already else ""
            logger.info(
                f"  [{i}/{total}] {spec.name}  "
                f"lon={spec.lon}, lat={spec.lat}, "
                f"rps={spec.return_periods}{tag}"
            )
        logger.info("Dry-run complete — no execution.")
        return BatchResult(
            input_file=input_file,
            output_dir=output_dir,
            total=total,
            completed=0,
            failed=0,
            skipped=0,
            results=[],
            errors={},
            duration_sec=0.0,
            summary_csv=summary_csv,
        )

    t0 = time.monotonic()
    completed_results = []
    errors: dict = {}
    n_skip = 0

    # Partition specs into to-run and to-skip
    to_run = []
    for spec in specs:
        if resume and _is_already_complete(output_dir, spec.name):
            logger.info(
                f"[resume] Skipping {spec.name!r} — "
                "run_metadata.json already complete"
            )
            n_skip += 1
        else:
            to_run.append(spec)

    n_run = len(to_run)
    logger.info(f"Batch plan: {n_run} to run, {n_skip} skipped (already complete)")

    done_count = 0

    def _run_one(spec: WatershedSpec) -> OrchestratorResult:
        ws_dir = output_dir / spec.name
        t_start = time.monotonic()
        logger.info(
            f"[{spec.name}] Starting pipeline "
            f"(lon={spec.lon}, lat={spec.lat}, rps={spec.return_periods}) …"
        )
        result = run_watershed(
            pour_point_lon=spec.lon,
            pour_point_lat=spec.lat,
            output_dir=ws_dir,
            return_periods=spec.return_periods,
            resolution_m=resolution_m,
            mesh_strategy=mesh_strategy,
            ras_exe_dir=ras_exe_dir,
            name=spec.name,
        )
        dur = time.monotonic() - t_start
        _write_run_metadata(spec, result, dur)
        if (
            notify_config is not None
            and notify_config.on_complete
            and result.status == "complete"
        ):
            import notify as _notify  # lazy import — avoids circular dependency
            _notify.notify_run_complete(result, notify_config)
        logger.info(
            f"[{spec.name}] Complete in {dur:.1f}s [status={result.status}]"
        )
        return result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_spec = {
            executor.submit(_run_one, spec): spec
            for spec in to_run
        }
        for future in as_completed(future_to_spec):
            spec = future_to_spec[future]
            done_count += 1
            n_remaining = n_run - done_count
            try:
                result = future.result()
                completed_results.append(result)
            except Exception as exc:
                errors[spec.name] = str(exc)
                logger.error(f"[{spec.name}] FAILED: {exc}")

            logger.info(
                f"[{len(completed_results)}/{n_run}] complete, "
                f"[{len(errors)}/{n_run}] failed, "
                f"[{n_remaining}/{n_run}] remaining"
            )

    duration_sec = time.monotonic() - t0
    batch_result = BatchResult(
        input_file=input_file,
        output_dir=output_dir,
        total=total,
        completed=len(completed_results),
        failed=len(errors),
        skipped=n_skip,
        results=completed_results,
        errors=errors,
        duration_sec=duration_sec,
        summary_csv=summary_csv,
    )

    write_summary_csv(batch_result, summary_csv)

    logger.info(
        f"Batch complete in {duration_sec:.1f}s — "
        f"{batch_result.completed} done, "
        f"{batch_result.failed} failed, "
        f"{batch_result.skipped} skipped"
    )

    if notify_config is not None:
        import notify as _notify  # lazy import — avoids circular dependency
        _notify.notify_batch_complete(batch_result, notify_config)

    return batch_result


# ── Summary CSV ────────────────────────────────────────────────────────────────

def write_summary_csv(batch_result: BatchResult, output_path: Path) -> Path:
    """
    Write summary CSV with one row per watershed.

    Columns: name, lon, lat, status, duration_sec, drainage_area_mi2,
             q100_cfs, flood_extent_km2 (if available), error_msg

    Args:
        batch_result: Completed BatchResult from run_batch()
        output_path:  Destination path for the CSV file

    Returns:
        Path to the written CSV file
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []

    # Rows from completed / partial results
    for result in batch_result.results:
        drainage_area_mi2 = ""
        if result.watershed is not None:
            try:
                drainage_area_mi2 = result.watershed.characteristics.drainage_area_mi2
            except AttributeError:
                pass

        q100_cfs = ""
        if result.peak_flows is not None:
            val = getattr(result.peak_flows, "Q100", None)
            if val is not None:
                q100_cfs = round(float(val), 1)

        lon, lat = result.pour_point if result.pour_point else ("", "")

        rows.append({
            "name": result.name,
            "lon": lon,
            "lat": lat,
            "status": result.status,
            "duration_sec": round(result.duration_sec, 2),
            "drainage_area_mi2": drainage_area_mi2,
            "q100_cfs": q100_cfs,
            "flood_extent_km2": "",
            "error_msg": "",
        })

    # Rows from failed watersheds
    for ws_name, err_msg in batch_result.errors.items():
        rows.append({
            "name": ws_name,
            "lon": "",
            "lat": "",
            "status": "failed",
            "duration_sec": "",
            "drainage_area_mi2": "",
            "q100_cfs": "",
            "flood_extent_km2": "",
            "error_msg": err_msg,
        })

    fieldnames = [
        "name", "lon", "lat", "status", "duration_sec",
        "drainage_area_mi2", "q100_cfs", "flood_extent_km2", "error_msg",
    ]
    with output_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    logger.info(f"Summary CSV written: {output_path} ({len(rows)} rows)")
    return output_path


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RAS Agent batch processor")
    parser.add_argument("input_file", type=Path,
                        help="CSV or JSON watershed spec file")
    parser.add_argument("output_dir", type=Path,
                        help="Output directory")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--ras-exe-dir", type=Path, default=None)
    parser.add_argument("--no-resume", action="store_true",
                        help="Re-run even if output exists")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--webhook", default=None,
                        help="Webhook URL for completion notification")
    parser.add_argument("--notify-email", default=None,
                        help="Email address for completion notification")
    args = parser.parse_args()

    notify_config = None
    if args.webhook or args.notify_email:
        import notify as _notify
        notify_config = _notify.NotifyConfig(
            webhook_url=args.webhook,
            email_to=args.notify_email,
        )

    result = run_batch(
        args.input_file,
        args.output_dir,
        max_workers=args.workers,
        ras_exe_dir=None if args.mock else args.ras_exe_dir,
        resume=not args.no_resume,
        dry_run=args.dry_run,
        notify_config=notify_config,
    )
    print(
        f"Batch complete: {result.completed} done, "
        f"{result.failed} failed, {result.skipped} skipped"
    )
    print(f"Summary: {result.summary_csv}")
