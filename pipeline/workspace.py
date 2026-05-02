"""
workspace.py - Stable command surface for Spring Creek-style study workspaces.

This module keeps `ras-agent` focused on Illinois integration and reporting while
delegating reusable gauge-first study packaging to `hms-commander`.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

import context_layers
import report

logger = logging.getLogger(__name__)

DEFAULT_ISSUE_URLS: dict[str, Optional[str]] = {
    "ras_agent_streamstats": None,  # GitHub issues are disabled in gpt-cmdr/ras-agent.
    "ras_agent_report_contract": None,
    "hms_commander_gauge_study": "https://github.com/gpt-cmdr/hms-commander/issues/2",
    "hms_commander_workspace_organizer": "https://github.com/gpt-cmdr/hms-commander/issues/3",
    "hms_commander_report_contract": "https://github.com/gpt-cmdr/hms-commander/issues/4",
    "hms_commander_taudem_workflow": "https://github.com/gpt-cmdr/hms-commander/issues/5",
    "ras_commander_drainage_area": "https://github.com/gpt-cmdr/ras-commander/issues/36",
    "ras_commander_report_contract": "https://github.com/gpt-cmdr/ras-commander/issues/37",
    "ras_commander_geometry_builder": "https://github.com/gpt-cmdr/ras-commander/issues/38",
}

DEFAULT_WORKSPACE_SUBDIRS = [
    "00_metadata",
    "01_gauge",
    "02_basin_outline",
    "03_nhdplus",
    "04_terrain",
    "05_landcover_nlcd",
    "06_soils",
    "07_research",
    "08_report",
]


def _to_jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value


def create_workspace_structure(study_name: str, workspace_root: Optional[Path] = None) -> Path:
    """Create the standard base-data workspace folder structure."""
    workspace_root = Path(workspace_root) if workspace_root else Path("workspace")
    workspace_dir = workspace_root / study_name
    for subdir in DEFAULT_WORKSPACE_SUBDIRS:
        (workspace_dir / subdir).mkdir(parents=True, exist_ok=True)
    return workspace_dir


def build_report_package(
    workspace_dir: Path,
    *,
    include_map: bool = True,
    issue_urls: Optional[dict[str, Optional[str]]] = None,
) -> dict[str, Path]:
    """Build `report.html`, `report.json`, and `data_gap_analysis.json`."""
    merged_issue_urls = DEFAULT_ISSUE_URLS.copy()
    if issue_urls:
        merged_issue_urls.update(issue_urls)
    return report.write_workspace_report_package(
        workspace_dir,
        include_map=include_map,
        issue_urls=merged_issue_urls,
    )


def generate_gap_analysis(
    workspace_dir: Path,
    *,
    output_path: Optional[Path] = None,
    issue_urls: Optional[dict[str, Optional[str]]] = None,
) -> Path:
    """Generate only `data_gap_analysis.json` for a workspace."""
    merged_issue_urls = DEFAULT_ISSUE_URLS.copy()
    if issue_urls:
        merged_issue_urls.update(issue_urls)
    ctx = report._load_workspace_context(Path(workspace_dir))
    validation = report.validate_workspace(workspace_dir)
    gap_analysis = report.build_workspace_gap_analysis(
        ctx,
        validation=validation,
        issue_urls=merged_issue_urls,
    )
    output_path = Path(output_path) if output_path else Path(workspace_dir) / "08_report" / "data_gap_analysis.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(gap_analysis, indent=2), encoding="utf-8")
    return output_path


def validate_workspace_completeness(workspace_dir: Path) -> dict:
    """Return the shared-contract validation summary for a workspace."""
    return report.validate_workspace(workspace_dir)


def refresh_context_layers(
    workspace_dir: Path,
    *,
    buffer_m: float = context_layers.DEFAULT_ANALYSIS_BUFFER_M,
    nlcd_year: int = 2021,
) -> dict[str, Path]:
    """Refresh buffered informational layers for a workspace."""
    return context_layers.refresh_workspace_context_layers(
        Path(workspace_dir),
        buffer_m=buffer_m,
        nlcd_year=nlcd_year,
    )


def _resolve_hms_gauge_study_builder():
    try:
        from hms_commander import HmsGaugeStudy
    except ImportError as exc:
        raise ImportError(
            "The reusable gauge-first study builder lives in hms-commander. "
            "Install hms-commander in the active environment before using "
            "gather_base_data()."
        ) from exc

    for attr in ("build_from_usgs_site", "build_study_workspace", "create_study_workspace", "build_workspace"):
        if hasattr(HmsGaugeStudy, attr):
            return getattr(HmsGaugeStudy, attr)

    raise AttributeError(
        "HmsGaugeStudy was found, but no supported builder method was available. "
        "Expected one of: build_from_usgs_site, build_study_workspace, create_study_workspace, build_workspace."
    )


def gather_base_data(
    site_id: str,
    study_name: str,
    *,
    workspace_root: Optional[Path] = None,
    builder_kwargs: Optional[dict] = None,
) -> dict:
    """
    Delegate the reusable gauge-first study build to `hms-commander` and return
    the created workspace path plus upstream builder output.
    """
    workspace_dir = create_workspace_structure(study_name, workspace_root=workspace_root)
    builder = _resolve_hms_gauge_study_builder()
    builder_kwargs = builder_kwargs or {}

    try:
        study_result = builder(site_id=site_id, workspace_root=workspace_dir, **builder_kwargs)
    except TypeError:
        try:
            study_result = builder(site_id=site_id, workspace_dir=workspace_dir, **builder_kwargs)
        except TypeError:
            try:
                study_result = builder(usgs_site_id=site_id, workspace_dir=workspace_dir, **builder_kwargs)
            except TypeError:
                study_result = builder(site_id, workspace_dir, **builder_kwargs)

    return {
        "site_id": site_id,
        "workspace_dir": str(workspace_dir),
        "study_result": study_result,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAS Agent workspace utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create-workspace", help="Create the standard workspace folder structure")
    create_parser.add_argument("--study-name", required=True)
    create_parser.add_argument("--workspace-root", default="workspace")

    gather_parser = subparsers.add_parser("gather-base-data", help="Delegate the reusable study build to hms-commander")
    gather_parser.add_argument("--site-id", required=True)
    gather_parser.add_argument("--study-name", required=True)
    gather_parser.add_argument("--workspace-root", default="workspace")

    report_parser = subparsers.add_parser("build-report-package", help="Generate report.html/report.json/data_gap_analysis.json")
    report_parser.add_argument("--workspace-dir", required=True)
    report_parser.add_argument("--no-map", action="store_true")

    gap_parser = subparsers.add_parser("generate-gap-analysis", help="Generate only data_gap_analysis.json")
    gap_parser.add_argument("--workspace-dir", required=True)
    gap_parser.add_argument("--output-path")

    validate_parser = subparsers.add_parser("validate-workspace", help="Validate workspace completeness")
    validate_parser.add_argument("--workspace-dir", required=True)

    refresh_parser = subparsers.add_parser(
        "refresh-context-layers",
        help="Refresh NLCD, soils, and NHDPlus context against the shared buffered analysis extent",
    )
    refresh_parser.add_argument("--workspace-dir", required=True)
    refresh_parser.add_argument("--buffer-m", type=float, default=context_layers.DEFAULT_ANALYSIS_BUFFER_M)
    refresh_parser.add_argument("--nlcd-year", type=int, default=2021)

    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "create-workspace":
        workspace_dir = create_workspace_structure(args.study_name, workspace_root=Path(args.workspace_root))
        print(workspace_dir)
        return 0

    if args.command == "gather-base-data":
        result = gather_base_data(
            args.site_id,
            args.study_name,
            workspace_root=Path(args.workspace_root),
        )
        print(json.dumps(_to_jsonable(result), indent=2))
        return 0

    if args.command == "build-report-package":
        outputs = build_report_package(Path(args.workspace_dir), include_map=not args.no_map)
        print(json.dumps({key: str(value) for key, value in outputs.items()}, indent=2))
        return 0

    if args.command == "generate-gap-analysis":
        output = generate_gap_analysis(Path(args.workspace_dir), output_path=args.output_path)
        print(output)
        return 0

    if args.command == "validate-workspace":
        validation = validate_workspace_completeness(Path(args.workspace_dir))
        print(json.dumps(validation, indent=2))
        return 0

    if args.command == "refresh-context-layers":
        outputs = refresh_context_layers(
            Path(args.workspace_dir),
            buffer_m=args.buffer_m,
            nlcd_year=args.nlcd_year,
        )
        print(json.dumps({key: str(value) for key, value in outputs.items()}, indent=2))
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
