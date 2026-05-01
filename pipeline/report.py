"""
report.py — Self-contained HTML run report generator

Generates a fully self-contained HTML report for each RAS Agent pipeline run.
The HTML embeds inline CSS and base64-encoded matplotlib plots — no external
dependencies at render time. Suitable for FEMA memos, internal review, and
project files.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import base64
import json
import io
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.request import urlopen

logger = logging.getLogger(__name__)

_VERSION = "0.1.0"
_WORKSPACE_REPORT_SCHEMA_VERSION = "base-engineering-report/v1"
_WORKSPACE_GAP_SCHEMA_VERSION = "data-gap-analysis/v1"


def _utc_timestamp() -> str:
    """Return a stable UTC timestamp string for generated artifacts."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = """\
body { font-family: Arial, sans-serif; max-width: 1100px; margin: 0 auto; padding: 20px; color: #333; }
h1 { color: #1a5276; border-bottom: 2px solid #1a5276; }
h2 { color: #1a5276; margin-top: 30px; }
table { border-collapse: collapse; width: 100%; margin: 15px 0; }
th { background: #1a5276; color: white; padding: 8px 12px; text-align: left; }
td { padding: 7px 12px; border-bottom: 1px solid #ddd; }
tr:nth-child(even) { background: #f5f5f5; }
.meta { color: #666; font-size: 0.9em; }
.footer { margin-top: 40px; padding-top: 15px; border-top: 1px solid #ddd; color: #888; font-size: 0.85em; text-align: center; }
img { max-width: 100%; margin: 10px 0; }
.status-complete { color: #27ae60; font-weight: bold; }
.status-partial { color: #e67e22; font-weight: bold; }
.status-failed { color: #e74c3c; font-weight: bold; }
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_git_commit() -> str:
    """Return short git commit hash, or 'unknown' if unavailable."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _b64_png(fig) -> str:
    """Encode a matplotlib Figure as a base64 PNG data URI string."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    buf.seek(0)
    data = base64.b64encode(buf.read()).decode("ascii")
    return f"data:image/png;base64,{data}"


def _html_table(headers: list, rows: list) -> str:
    """Build a simple HTML table string."""
    th_cells = "".join(f"<th>{h}</th>" for h in headers)
    rows_html = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>\n"
        for row in rows
    )
    return (
        f"<table><thead><tr>{th_cells}</tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
    )


def _status_span(status: str) -> str:
    return f'<span class="status-{status}">{status.upper()}</span>'


# ── Plot Generation ───────────────────────────────────────────────────────────

def _plot_hydrographs(hydro_set, output_periods: list) -> str:
    """
    Return base64-encoded PNG of combined hydrograph figure.

    Args:
        hydro_set:      HydrographSet from hydrograph.py
        output_periods: List of return periods to plot

    Returns:
        Base64-encoded PNG data URI, or empty string if unavailable.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        logger.warning("matplotlib not available; skipping hydrograph plot")
        return ""

    if not output_periods:
        return ""

    n = len(output_periods)
    # Color scale: blue (frequent) → red (rare)
    colors = plt.cm.cool_r(np.linspace(0.1, 0.9, n))

    fig, ax = plt.subplots(figsize=(10, 5))
    for color, rp in zip(colors, output_periods):
        hydro = hydro_set.hydrographs.get(rp)
        if hydro is None:
            continue
        ax.plot(
            hydro.times_hr,
            hydro.flows_cfs,
            label=f"T={rp}-yr  (Qp={hydro.peak_flow_cfs:,.0f} cfs)",
            color=color,
            linewidth=1.5,
        )
        # Annotate peak
        peak_idx = int(np.argmax(hydro.flows_cfs))
        ax.annotate(
            f"{hydro.peak_flow_cfs:,.0f}",
            xy=(hydro.times_hr[peak_idx], hydro.flows_cfs[peak_idx]),
            xytext=(3, 3),
            textcoords="offset points",
            fontsize=7,
            color=color,
        )

    ax.set_xlabel("Time (hours)")
    ax.set_ylabel("Flow (cfs)")
    ax.set_title("Synthetic Design Hydrographs — NRCS Unit Hydrograph Method")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, linestyle="--", alpha=0.4)
    fig.tight_layout()

    encoded = _b64_png(fig)
    plt.close(fig)
    return encoded


def _plot_flood_extents(results: dict) -> str:
    """
    Return base64-encoded PNG of flood extent overlays.

    Args:
        results: {return_period: {name: path}} dict from OrchestratorResult.results

    Returns:
        Base64-encoded PNG data URI, or empty string if no gpkg files exist.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import numpy as np
        import geopandas as gpd
    except ImportError:
        logger.warning("matplotlib/geopandas not available; skipping flood extent plot")
        return ""

    # Collect available gpkg paths
    rp_gpkgs = {}
    for rp, output_dict in results.items():
        if not isinstance(output_dict, dict):
            continue
        gpkg = output_dict.get("flood_extent_gpkg")
        if gpkg is not None and Path(gpkg).exists():
            rp_gpkgs[rp] = Path(gpkg)

    if not rp_gpkgs:
        return ""

    sorted_rps = sorted(rp_gpkgs.keys())
    n = len(sorted_rps)
    # Color scale: light blue (frequent) → dark blue (rare)
    colors = plt.cm.Blues(np.linspace(0.3, 0.9, n))

    fig, ax = plt.subplots(figsize=(8, 6))
    patches = []
    for color, rp in zip(colors, sorted_rps):
        try:
            gdf = gpd.read_file(str(rp_gpkgs[rp]))
            if not gdf.empty:
                gdf.plot(ax=ax, color=color, alpha=0.5, edgecolor="navy", linewidth=0.5)
                patches.append(
                    mpatches.Patch(color=color, label=f"T={rp}-yr flood")
                )
        except Exception as exc:
            logger.warning(f"Could not read flood extent for T={rp}yr: {exc}")

    if patches:
        ax.legend(handles=patches, fontsize=8, loc="upper right")

    ax.set_title("Flood Extent by Return Period")
    ax.set_xlabel("Easting (m, EPSG:5070)")
    ax.set_ylabel("Northing (m, EPSG:5070)")
    ax.grid(True, linestyle="--", alpha=0.3)
    fig.tight_layout()

    encoded = _b64_png(fig)
    plt.close(fig)
    return encoded


# ── Report Sections ───────────────────────────────────────────────────────────

def _section_header(result, git_commit: str, run_ts: str) -> str:
    lon, lat = result.pour_point
    return f"""\
<h1>RAS Agent — Watershed Run Report</h1>
<p class="meta">
  <strong>Watershed:</strong> {result.name}<br>
  <strong>Pour point:</strong> {lat:.5f}&deg;N, {lon:.5f}&deg;W<br>
  <strong>Run timestamp:</strong> {run_ts}<br>
  <strong>Status:</strong> {_status_span(result.status)}<br>
  <strong>Duration:</strong> {result.duration_sec:.1f} s<br>
  <strong>RAS Agent version:</strong> {_VERSION} (commit {git_commit})<br>
  <strong>Output directory:</strong> <code>{result.output_dir}</code>
</p>
"""


def _section_basin(result) -> str:
    html = "<h2>Basin Characteristics</h2>"
    if result.watershed is None:
        return html + "<p class='meta'>Watershed delineation not available.</p>"
    c = result.watershed.characteristics
    rows = [
        ["Drainage Area", f"{c.drainage_area_mi2:.2f} mi\u00b2", f"{c.drainage_area_km2:.2f} km\u00b2"],
        ["Mean Elevation", f"{c.mean_elevation_m:.1f} m", "\u2014"],
        ["Relief", f"{c.relief_m:.1f} m", "\u2014"],
        ["Main Channel Length", f"{c.main_channel_length_km:.2f} km", "\u2014"],
        ["Main Channel Slope", f"{c.main_channel_slope_m_per_m:.5f} m/m", "\u2014"],
        ["Centroid (WGS84)", f"{c.centroid_lat:.5f}\u00b0N", f"{c.centroid_lon:.5f}\u00b0W"],
    ]
    return html + _html_table(["Parameter", "Value (Primary)", "Value (Alt)"], rows)


def _section_peak_flows(result) -> str:
    html = "<h2>Peak Flow Estimates</h2>"
    if result.peak_flows is None:
        return html + "<p class='meta'>Peak flow estimates not available.</p>"
    pf = result.peak_flows
    ws_label = pf.workspace_id if pf.workspace_id else "\u2014"
    html += (
        f"<p class='meta'>Source: <strong>{pf.source}</strong> | "
        f"StreamStats workspace: {ws_label}</p>"
    )
    rp_labels = [(2,"Q2"),(5,"Q5"),(10,"Q10"),(25,"Q25"),(50,"Q50"),(100,"Q100"),(500,"Q500")]
    rows = []
    for rp, label in rp_labels:
        val = getattr(pf, label, None)
        if val is not None:
            rows.append([f"{rp}-year", label, f"{val:,.0f} cfs"])
    return html + _html_table(["Return Period", "Statistic", "Peak Flow (CFS)"], rows)


def _section_hydrographs(result, include_plots: bool) -> str:
    html = "<h2>Hydrograph Plots</h2>"
    if not include_plots:
        return html + "<p class='meta'>Plots disabled.</p>"
    if result.hydro_set is None:
        return html + "<p class='meta'>Hydrograph data not available.</p>"
    output_periods = sorted(result.hydro_set.hydrographs.keys())
    png_data = _plot_hydrographs(result.hydro_set, output_periods)
    if png_data:
        html += f'<img src="{png_data}" alt="Hydrograph plot" />'
    else:
        html += "<p class='meta'>Hydrograph plot could not be generated.</p>"
    return html


def _section_results(result) -> str:
    html = "<h2>Results Summary</h2>"
    if not result.results:
        return html + "<p class='meta'>No simulation results available.</p>"
    rows = []
    for rp in sorted(result.results.keys()):
        od = result.results[rp]
        depth = od.get("depth_grid", "\u2014") if isinstance(od, dict) else "\u2014"
        gpkg = od.get("flood_extent_gpkg", "\u2014") if isinstance(od, dict) else "\u2014"
        rows.append([f"{rp}-year", str(depth), str(gpkg)])
    return html + _html_table(
        ["Return Period", "Max Depth Raster", "Flood Extent (GeoPackage)"],
        rows,
    )


def _section_flood_preview(result, include_plots: bool) -> str:
    html = "<h2>Flood Extent Preview</h2>"
    if not include_plots:
        return html + "<p class='meta'>Plots disabled.</p>"
    if not result.results:
        return html + "<p class='meta'>No simulation results available for flood extent preview.</p>"
    flood_png = _plot_flood_extents(result.results)
    if flood_png:
        html += f'<img src="{flood_png}" alt="Flood extent preview" />'
    else:
        html += "<p class='meta'>No flood extent GeoPackage files found for preview.</p>"
    return html


def _section_provenance(result, git_commit: str, run_ts: str) -> str:
    html = "<h2>Data Provenance</h2>"
    lon, lat = result.pour_point
    rows = []

    if result.terrain is not None:
        rows.append(["DEM source", str(result.terrain.dem_path)])
    else:
        rows.append(["DEM source", "\u2014"])

    if result.peak_flows is not None and result.peak_flows.workspace_id:
        rows.append(["StreamStats workspace ID", result.peak_flows.workspace_id])
    else:
        rows.append(["StreamStats workspace ID", "Not used (regression fallback)"])

    if result.peak_flows is not None:
        rows.append(["Peak flow source", result.peak_flows.source])

    if result.hydro_set is not None:
        rows.append([
            "Hydrograph method",
            "NRCS Dimensionless Unit Hydrograph (NEH Part 630, Ch. 16)",
        ])
        rows.append([
            "Time of concentration",
            f"{result.hydro_set.time_of_concentration_hr:.2f} hr (Kirpich method)",
        ])

    rows.append(["Pour point (WGS84)", f"{lat:.6f}\u00b0N, {lon:.6f}\u00b0W"])
    rows.append(["Git commit", git_commit])
    rows.append(["Report generated", run_ts])

    html += _html_table(["Parameter", "Value"], rows)

    if result.errors:
        html += "<h3>Non-fatal Errors</h3><ul>"
        for err in result.errors:
            html += f"<li class='meta'>{err}</li>"
        html += "</ul>"

    return html


# ── Public API ────────────────────────────────────────────────────────────────

def generate_report(
    result,
    output_path: Optional[Path] = None,
    include_plots: bool = True,
) -> Path:
    """
    Generate an HTML report for an orchestrator run result.

    The output is a fully self-contained HTML file with inline CSS and
    base64-encoded PNG plots — suitable for email, project files, and FEMA memos.

    Args:
        result:        OrchestratorResult from run_watershed()
        output_path:   Path to write HTML; defaults to result.output_dir / "report.html"
        include_plots: If True, embed matplotlib hydrograph and flood extent plots.

    Returns:
        Path to the written HTML file.
    """
    if output_path is None:
        output_path = Path(result.output_dir) / "report.html"
    else:
        output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    git_commit = _get_git_commit()
    run_ts = _utc_timestamp()

    sections = [
        _section_header(result, git_commit, run_ts),
        _section_basin(result),
        _section_peak_flows(result),
        _section_hydrographs(result, include_plots),
        _section_results(result),
        _section_flood_preview(result, include_plots),
        _section_provenance(result, git_commit, run_ts),
    ]

    footer = (
        '<div class="footer">'
        "Generated by RAS Agent (Apache 2.0) &mdash; "
        "Illinois State Water Survey / CHAMP"
        "</div>"
    )

    body = "\n".join(sections) + "\n" + footer

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>RAS Agent Report \u2014 {result.name}</title>
  <style>
{_CSS}  </style>
</head>
<body>
{body}
</body>
</html>
"""

    output_path.write_text(html, encoding="utf-8")
    logger.info(f"[Report] Written to {output_path}")
    return output_path


# ── Workspace Report Support ───────────────────────────────────────────────────

_MAPLIBRE_VERSION = "4.7.1"
_MAPLIBRE_CSS_URL = f"https://unpkg.com/maplibre-gl@{_MAPLIBRE_VERSION}/dist/maplibre-gl.css"
_MAPLIBRE_JS_URL = f"https://unpkg.com/maplibre-gl@{_MAPLIBRE_VERSION}/dist/maplibre-gl.js"
_MAPLIBRE_CSS_CACHE = None
_MAPLIBRE_JS_CACHE = None

_STATION_PRECIP_QAQC_CANDIDATES = (
    "08_report/station_precip_qaqc.json",
    "00_metadata/station_precip_qaqc.json",
    "07_research/station_precip_qaqc.json",
    "08_report/precip_station_qaqc.json",
    "00_metadata/precip_station_qaqc.json",
)

_WORKSPACE_CSS = """\
body.workspace-report { font-family: "Segoe UI", Arial, sans-serif; max-width: 1320px; margin: 0 auto; padding: 24px; color: #243342; background: #f6f8fb; }
.page-header { margin-bottom: 24px; }
.page-header h1 { margin-bottom: 6px; color: #173a5e; border-bottom: none; }
.lede { color: #4c6279; max-width: 980px; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin: 20px 0 28px; }
.summary-card { background: white; border: 1px solid #d7e0ea; border-radius: 10px; padding: 14px 16px; box-shadow: 0 1px 2px rgba(17, 24, 39, 0.04); }
.summary-card .label { font-size: 0.8rem; color: #6a7c8f; text-transform: uppercase; letter-spacing: 0.04em; }
.summary-card .value { font-size: 1.25rem; font-weight: 700; margin-top: 6px; color: #16324f; }
.section { background: white; border: 1px solid #d7e0ea; border-radius: 12px; padding: 20px; margin: 0 0 20px; box-shadow: 0 1px 2px rgba(17, 24, 39, 0.04); }
.section h2 { margin-top: 0; color: #173a5e; }
.section h3 { color: #214d78; margin-top: 20px; }
.section p { color: #405568; }
.figure-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 18px; }
.figure-card { background: #fbfcfe; border: 1px solid #dbe4ef; border-radius: 10px; padding: 12px; }
.figure-card h3 { margin-top: 0; }
.figure-card img { width: 100%; border-radius: 8px; border: 1px solid #dbe4ef; background: white; }
.figure-caption { font-size: 0.92rem; color: #5c7287; margin-top: 8px; }
.map-wrap { display: grid; grid-template-columns: minmax(0, 1fr) 260px; gap: 16px; align-items: start; }
.map-panel { border: 1px solid #dbe4ef; border-radius: 10px; overflow: hidden; background: #eef3f8; }
#spring-creek-map { width: 100%; height: 640px; }
.map-sidebar { border: 1px solid #dbe4ef; border-radius: 10px; background: #fbfcfe; padding: 12px; }
.map-sidebar h3 { margin-top: 0; }
.layer-control { display: flex; align-items: center; gap: 8px; margin: 8px 0; color: #30465d; }
.tiny-note { color: #6e7f90; font-size: 0.88rem; }
.codeish { font-family: Consolas, "Courier New", monospace; font-size: 0.92rem; }
.soil-table td, .soil-table th { font-size: 0.92rem; }
@media (max-width: 980px) { .map-wrap { grid-template-columns: 1fr; } #spring-creek-map { height: 520px; } }
"""


def _fetch_text(url: str) -> str:
    with urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8")


def _load_maplibre_assets() -> tuple[str, str]:
    global _MAPLIBRE_CSS_CACHE, _MAPLIBRE_JS_CACHE
    if _MAPLIBRE_CSS_CACHE is None:
        _MAPLIBRE_CSS_CACHE = _fetch_text(_MAPLIBRE_CSS_URL)
    if _MAPLIBRE_JS_CACHE is None:
        _MAPLIBRE_JS_CACHE = _fetch_text(_MAPLIBRE_JS_URL).replace("https://maplibre.org/", "#")
    return _MAPLIBRE_CSS_CACHE, _MAPLIBRE_JS_CACHE


def _html_table_safe(headers: list[str], rows: list[list[object]], table_class: str = "") -> str:
    def _esc(value: object) -> str:
        text = "—" if value is None else str(value)
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    class_attr = f' class="{table_class}"' if table_class else ""
    th_cells = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    rows_html = "".join(
        "<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>\n"
        for row in rows
    )
    return f"<table{class_attr}><thead><tr>{th_cells}</tr></thead><tbody>{rows_html}</tbody></table>"


def _file_data_uri(path: Path, mime_type: str = "image/png") -> str:
    path = Path(path)
    if not path.exists():
        return ""
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{data}"


def _round_coords(obj, decimals: int = 5):
    if isinstance(obj, list):
        return [_round_coords(v, decimals) for v in obj]
    if isinstance(obj, float):
        return round(obj, decimals)
    return obj


# DEV NOTE 2026-04-20:
# These workspace report helpers are still Spring Creek seed-workspace code even
# though their names read like generic report-package utilities.
# Needed next steps:
# 1. Drive station/workspace identity from manifest metadata instead of hardcoded
#    `USGS_05577500` filenames and `Spring Creek Base Data Report` strings.
# 2. Centralize artifact naming/path discovery so validation, context loading,
#    JSON output, and HTML generation share one contract.
# 3. Keep the Spring Creek case as a fixture-backed test workspace, but make the
#    implementation reject unsupported layouts explicitly rather than silently
#    mislabeling other studies as Spring Creek.
def _find_station_precip_qaqc_path(workspace_dir: Path) -> Optional[Path]:
    workspace_dir = Path(workspace_dir)
    for rel_path in _STATION_PRECIP_QAQC_CANDIDATES:
        candidate = workspace_dir / rel_path
        if candidate.exists():
            return candidate
    return None


def _load_workspace_context(workspace_dir: Path) -> dict:
    import pandas as pd
    import geopandas as gpd

    workspace_dir = Path(workspace_dir)
    gauge_dir = workspace_dir / "01_gauge"
    meta_dir = workspace_dir / "00_metadata"
    nhd_dir = workspace_dir / "03_nhdplus"
    terrain_dir = workspace_dir / "04_terrain"
    landcover_dir = workspace_dir / "05_landcover_nlcd"
    soils_dir = workspace_dir / "06_soils"
    basin_dir = workspace_dir / "02_basin_outline"

    def _read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _optional_json(path: Path) -> dict:
        return _read_json(path) if path.exists() else {}

    def _pick_path(preferred: Path, fallback: Path) -> Path:
        return preferred if preferred.exists() else fallback

    iv_flow = pd.read_csv(gauge_dir / "continuous" / "USGS_05577500_iv_last365d_flow.csv")
    iv_stage = pd.read_csv(gauge_dir / "continuous" / "USGS_05577500_iv_last365d_stage.csv")
    dv_flow = pd.read_csv(gauge_dir / "daily" / "USGS_05577500_dv_period_of_record_flow.csv")
    dv_stage = pd.read_csv(gauge_dir / "daily" / "USGS_05577500_dv_period_of_record_stage.csv")
    for df in (iv_flow, iv_stage, dv_flow, dv_stage):
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df["value"] = pd.to_numeric(df["value"], errors="coerce")

    gauge_point = gpd.read_file(gauge_dir / "USGS_05577500_point.geojson").to_crs("EPSG:4326")
    gauge_point_5070 = gpd.read_file(gauge_dir / "USGS_05577500_point_5070.geojson").to_crs("EPSG:5070")
    basin = gpd.read_file(basin_dir / "USGS_05577500_nldi_basin.geojson").to_crs("EPSG:4326")
    basin_5070 = gpd.read_file(basin_dir / "USGS_05577500_nldi_basin_5070.geojson").to_crs("EPSG:5070")
    analysis_extent_path = _pick_path(meta_dir / "analysis_extent.geojson", basin_dir / "USGS_05577500_nldi_basin.geojson")
    analysis_extent_5070_path = _pick_path(meta_dir / "analysis_extent_5070.geojson", basin_dir / "USGS_05577500_nldi_basin_5070.geojson")
    analysis_extent = gpd.read_file(analysis_extent_path).to_crs("EPSG:4326")
    analysis_extent_5070 = gpd.read_file(analysis_extent_5070_path).to_crs("EPSG:5070")
    flowlines_path = _pick_path(
        nhd_dir / "USGS_05577500_upstream_flowlines_analysis_extent.geojson",
        nhd_dir / "USGS_05577500_upstream_flowlines.geojson",
    )
    flowlines_5070_path = _pick_path(
        nhd_dir / "USGS_05577500_upstream_flowlines_analysis_extent_5070.geojson",
        nhd_dir / "USGS_05577500_upstream_flowlines_5070.geojson",
    )
    flowlines = gpd.read_file(flowlines_path).to_crs("EPSG:4326")
    flowlines_5070 = gpd.read_file(flowlines_5070_path).to_crs("EPSG:5070")
    gauge_huc12 = gpd.read_file(nhd_dir / "gauge_huc12.geojson").to_crs("EPSG:4326")
    gauge_huc12_5070 = gpd.read_file(nhd_dir / "gauge_huc12_5070.geojson").to_crs("EPSG:5070")
    hucs_path = _pick_path(
        nhd_dir / "basin_intersecting_huc12_analysis_extent.geojson",
        nhd_dir / "basin_intersecting_huc12.geojson",
    )
    hucs_5070_path = _pick_path(
        nhd_dir / "basin_intersecting_huc12_analysis_extent_5070.geojson",
        nhd_dir / "basin_intersecting_huc12_5070.geojson",
    )
    hucs = gpd.read_file(hucs_path).to_crs("EPSG:4326")
    hucs_5070 = gpd.read_file(hucs_5070_path).to_crs("EPSG:5070")
    soils_path = _pick_path(
        soils_dir / "ssurgo_mapunitpoly_analysis_extent.geojson",
        soils_dir / "ssurgo_mapunitpoly_basin.geojson",
    )
    soils_5070_path = _pick_path(
        soils_dir / "ssurgo_mapunitpoly_analysis_extent_5070.geojson",
        soils_dir / "ssurgo_mapunitpoly_basin_5070.geojson",
    )
    soils = gpd.read_file(soils_path).to_crs("EPSG:4326")
    soils_5070 = gpd.read_file(soils_5070_path).to_crs("EPSG:5070")
    nlcd_path = _pick_path(
        landcover_dir / "nlcd_2021_analysis_extent.tif",
        landcover_dir / "nlcd_2021_watershed.tif",
    )
    precip_qaqc_path = _find_station_precip_qaqc_path(workspace_dir)

    peaks = pd.read_csv(
        gauge_dir / "peaks" / "USGS_05577500_annual_peaks.rdb",
        sep="\t",
        comment="#",
        skiprows=[1],
        dtype=str,
    )
    peaks["peak_dt"] = pd.to_datetime(peaks["peak_dt"], format="%Y-%m-%d", errors="coerce")
    peaks["peak_va"] = pd.to_numeric(peaks["peak_va"], errors="coerce")
    peaks["gage_ht"] = pd.to_numeric(peaks["gage_ht"], errors="coerce")
    peaks = peaks.dropna(subset=["peak_dt", "peak_va"]).copy()

    return {
        "workspace_dir": workspace_dir,
        "manifest": _read_json(meta_dir / "manifest.json"),
        "gauge_summary": _read_json(meta_dir / "gauge_data_summary.json"),
        "huc_summary": _read_json(meta_dir / "nhdplus_huc_summary.json"),
        "gauge_feature": _read_json(gauge_dir / "USGS_05577500_feature.json"),
        "iv_flow": iv_flow,
        "iv_stage": iv_stage,
        "dv_flow": dv_flow,
        "dv_stage": dv_stage,
        "peaks": peaks,
        "gauge_point": gauge_point,
        "gauge_point_5070": gauge_point_5070,
        "basin": basin,
        "basin_5070": basin_5070,
        "analysis_extent": analysis_extent,
        "analysis_extent_5070": analysis_extent_5070,
        "analysis_extent_path": analysis_extent_path,
        "analysis_extent_5070_path": analysis_extent_5070_path,
        "analysis_extent_summary": _optional_json(meta_dir / "analysis_extent_summary.json"),
        "flowlines": flowlines,
        "flowlines_5070": flowlines_5070,
        "flowlines_path": flowlines_path,
        "flowlines_5070_path": flowlines_5070_path,
        "gauge_huc12": gauge_huc12,
        "gauge_huc12_5070": gauge_huc12_5070,
        "hucs": hucs,
        "hucs_5070": hucs_5070,
        "hucs_path": hucs_path,
        "hucs_5070_path": hucs_5070_path,
        "soils": soils,
        "soils_5070": soils_5070,
        "soils_path": soils_path,
        "soils_5070_path": soils_5070_path,
        "dem_path": terrain_dir / "spring_creek_basin_dem_5070.tif",
        "nlcd_path": nlcd_path,
        "precip_station_qaqc_path": precip_qaqc_path,
        "precip_station_qaqc": _read_json(precip_qaqc_path) if precip_qaqc_path else None,
    }


def validate_workspace(workspace_dir: Path) -> dict:
    """
    Validate that the Spring Creek-style workspace contains the minimum files
    required for report-package generation.
    """
    workspace_dir = Path(workspace_dir)
    required_files = [
        "00_metadata/manifest.json",
        "00_metadata/gauge_data_summary.json",
        "00_metadata/nhdplus_huc_summary.json",
        "01_gauge/USGS_05577500_feature.json",
        "01_gauge/USGS_05577500_point.geojson",
        "01_gauge/USGS_05577500_point_5070.geojson",
        "01_gauge/continuous/USGS_05577500_iv_last365d_flow.csv",
        "01_gauge/continuous/USGS_05577500_iv_last365d_stage.csv",
        "01_gauge/daily/USGS_05577500_dv_period_of_record_flow.csv",
        "01_gauge/daily/USGS_05577500_dv_period_of_record_stage.csv",
        "01_gauge/peaks/USGS_05577500_annual_peaks.rdb",
        "02_basin_outline/USGS_05577500_nldi_basin.geojson",
        "02_basin_outline/USGS_05577500_nldi_basin_5070.geojson",
        "03_nhdplus/gauge_huc12.geojson",
        "03_nhdplus/gauge_huc12_5070.geojson",
        "03_nhdplus/basin_intersecting_huc12.geojson",
        "03_nhdplus/basin_intersecting_huc12_5070.geojson",
        "03_nhdplus/USGS_05577500_upstream_flowlines.geojson",
        "03_nhdplus/USGS_05577500_upstream_flowlines_5070.geojson",
        "04_terrain/spring_creek_basin_dem_5070.tif",
        "05_landcover_nlcd/nlcd_2021_watershed.tif",
        "06_soils/ssurgo_mapunitpoly_basin.geojson",
        "06_soils/ssurgo_mapunitpoly_basin_5070.geojson",
    ]
    missing = [
        rel_path
        for rel_path in required_files
        if not (workspace_dir / rel_path).exists()
    ]

    optional_checks = {
        "analysis_extent_summary": workspace_dir / "00_metadata" / "analysis_extent_summary.json",
        "nlcd_analysis_extent": workspace_dir / "05_landcover_nlcd" / "nlcd_2021_analysis_extent.tif",
        "soils_analysis_extent": workspace_dir / "06_soils" / "ssurgo_mapunitpoly_analysis_extent.geojson",
        "nhdplus_analysis_extent": workspace_dir / "03_nhdplus" / "USGS_05577500_upstream_flowlines_analysis_extent.geojson",
        "taudem_verification": workspace_dir / "09_taudem_verification" / "boundary_verification.json",
        "drainage_area_comparison": workspace_dir / "00_metadata" / "drainage_area_comparison.json",
        "model_handoff": workspace_dir / "00_metadata" / "model_handoff.json",
    }
    present_optional = {
        name: str(path)
        for name, path in optional_checks.items()
        if path.exists()
    }
    precip_qaqc_path = _find_station_precip_qaqc_path(workspace_dir)
    if precip_qaqc_path is not None:
        present_optional["precip_station_qaqc"] = str(precip_qaqc_path)

    status = "complete" if not missing else "partial"
    return {
        "status": status,
        "workspace_dir": str(workspace_dir),
        "required_file_count": len(required_files),
        "missing_required_artifacts": missing,
        "present_optional_artifacts": present_optional,
    }


def build_workspace_gap_analysis(
    ctx: dict,
    *,
    validation: Optional[dict] = None,
    issue_urls: Optional[dict[str, Optional[str]]] = None,
    generated_at: Optional[str] = None,
) -> dict:
    """Build a machine-readable gap analysis for the base-data workspace."""
    generated_at = generated_at or _utc_timestamp()
    issue_urls = issue_urls or {}
    validation = validation or validate_workspace(ctx["workspace_dir"])
    manifest_notes = ctx["manifest"].get("notes", {})
    gaps = []

    for rel_path in validation.get("missing_required_artifacts", []):
        gaps.append({
            "id": f"missing-{rel_path.replace('/', '-').replace('.', '-')}",
            "category": "data",
            "severity": "high",
            "status": "open",
            "description": f"Required base-data artifact is missing from the workspace: {rel_path}",
            "affected_artifact": rel_path,
            "owner_repo": "ras-agent",
            "issue_url": issue_urls.get("ras_agent_report_contract"),
            "blocking_for": "report-package-completeness",
            "recommended_action": "Acquire or regenerate the missing artifact before treating the workspace as complete.",
        })

    streamstats_note = manifest_notes.get("streamstats_status")
    if streamstats_note:
        gaps.append({
            "id": "streamstats-service-transition",
            "category": "service",
            "severity": "medium",
            "status": "open",
            "description": streamstats_note,
            "affected_artifact": "07_research/streamstats",
            "owner_repo": "ras-agent",
            "issue_url": issue_urls.get("ras_agent_streamstats"),
            "blocking_for": "automated_peak_flow_inputs",
            "recommended_action": (
                "Update the StreamStats integration to the current USGS contract or "
                "document the approved regression fallback path."
            ),
        })

    if "taudem_verification" not in validation.get("present_optional_artifacts", {}):
        gaps.append({
            "id": "taudem-boundary-verification-pending",
            "category": "analysis",
            "severity": "medium",
            "status": "open",
            "description": (
                "TauDEM-based boundary verification has not yet been captured in a "
                "workspace verification artifact."
            ),
            "affected_artifact": "09_taudem_verification/boundary_verification.json",
            "owner_repo": "hms-commander",
            "issue_url": issue_urls.get("hms_commander_taudem_workflow")
            or issue_urls.get("hms_commander_gauge_study"),
            "blocking_for": "boundary_verification",
            "recommended_action": (
                "Run the generalized TauDEM verification workflow and save a verification "
                "artifact into the workspace."
            ),
        })

    if "drainage_area_comparison" not in validation.get("present_optional_artifacts", {}):
        gaps.append({
            "id": "drainage-area-comparison-pending",
            "category": "analysis",
            "severity": "medium",
            "status": "open",
            "description": (
                "No drainage-area comparison artifact was found for gauge, official basin, "
                "TauDEM, and model values."
            ),
            "affected_artifact": "00_metadata/drainage_area_comparison.json",
            "owner_repo": "ras-commander",
            "issue_url": issue_urls.get("ras_commander_drainage_area"),
            "blocking_for": "model-readiness",
            "recommended_action": (
                "Generate a standardized drainage-area comparison before promoting the "
                "workspace to model-building status."
            ),
        })

    if "model_handoff" not in validation.get("present_optional_artifacts", {}):
        gaps.append({
            "id": "geometry-first-model-handoff-pending",
            "category": "tooling",
            "severity": "medium",
            "status": "open",
            "description": (
                "No geometry-first model handoff artifact was found for passing the "
                "watershed package into ras-commander."
            ),
            "affected_artifact": "00_metadata/model_handoff.json",
            "owner_repo": "ras-commander",
            "issue_url": issue_urls.get("ras_commander_geometry_builder"),
            "blocking_for": "geometry_creation",
            "recommended_action": (
                "Use the geometry-first 2D flow area builder workflow to generate a "
                "documented model handoff package."
            ),
        })

    precip_qaqc = ctx.get("precip_station_qaqc")
    precip_qaqc_path = ctx.get("precip_station_qaqc_path")
    if precip_qaqc is None:
        gaps.append({
            "id": "station-precip-qaqc-pending",
            "category": "analysis",
            "severity": "medium",
            "status": "open",
            "description": (
                "No GHCND/station precipitation comparison artifact was found for "
                "the rain-on-grid review package."
            ),
            "affected_artifact": "08_report/station_precip_qaqc.json",
            "owner_repo": "ras-agent",
            "issue_url": issue_urls.get("ras_agent_report_contract"),
            "blocking_for": "model-readiness",
            "recommended_action": (
                "Generate station precipitation QAQC before using gridded forcing "
                "evidence for calibration or validation decisions."
            ),
        })
    else:
        affected_artifact = (
            str(precip_qaqc_path)
            if precip_qaqc_path is not None
            else "08_report/station_precip_qaqc.json"
        )
        gap_flag_ids = {
            "no-noaa-token",
            "no-nearby-stations",
            "station-observations-missing",
            "no-valid-observations",
            "gridded-depth-missing",
            "low-station-count",
            "station-grid-disagreement",
        }
        for flag in precip_qaqc.get("flags", []):
            flag_id = flag.get("id")
            if flag_id not in gap_flag_ids:
                continue
            gaps.append({
                "id": f"station-precip-qaqc-{flag_id}",
                "category": flag.get("category", "analysis"),
                "severity": flag.get("severity", "medium"),
                "status": "open",
                "description": flag.get("message", flag_id),
                "affected_artifact": affected_artifact,
                "owner_repo": "ras-agent",
                "issue_url": issue_urls.get("ras_agent_report_contract"),
                "blocking_for": flag.get("blocking_for", "model-readiness"),
                "recommended_action": (
                    "Review station precipitation evidence before calibration; "
                    "treat recalibration or regridding as a follow-up decision."
                ),
            })

    return {
        "schema_version": _WORKSPACE_GAP_SCHEMA_VERSION,
        "generated_at": generated_at,
        "study_name": "Spring Creek Base Data Report",
        "workspace_dir": str(ctx["workspace_dir"]),
        "status": validation.get("status", "partial"),
        "gap_count": len(gaps),
        "gaps": gaps,
    }


def _workspace_precip_qaqc_json(ctx: dict) -> dict:
    precip_qaqc = ctx.get("precip_station_qaqc")
    if not precip_qaqc:
        return {
            "status": "missing",
            "artifact": None,
            "summary": {
                "assessment": "not_evaluated",
                "station_count": 0,
                "valid_observation_count": 0,
                "missing_data_conditions": ["station-precip-qaqc-pending"],
            },
            "flags": [{
                "id": "station-precip-qaqc-pending",
                "severity": "medium",
                "message": "No station precipitation comparison artifact was found.",
            }],
            "station_comparisons": [],
        }

    return {
        "status": "present",
        "artifact": str(ctx.get("precip_station_qaqc_path")) if ctx.get("precip_station_qaqc_path") else None,
        "schema_version": precip_qaqc.get("schema_version"),
        "generated_at": precip_qaqc.get("generated_at"),
        "event": precip_qaqc.get("event", {}),
        "grid": precip_qaqc.get("grid", {}),
        "station_network": precip_qaqc.get("station_network", {}),
        "summary": precip_qaqc.get("summary", {}),
        "flags": precip_qaqc.get("flags", []),
        "station_comparisons": precip_qaqc.get("stations", []),
        "artifacts": precip_qaqc.get("artifacts", {}),
    }


def build_workspace_report_json(
    ctx: dict,
    *,
    report_html_path: Optional[Path] = None,
    gap_analysis: Optional[dict] = None,
    validation: Optional[dict] = None,
    generated_at: Optional[str] = None,
    git_commit: Optional[str] = None,
) -> dict:
    """Build the shared-contract report.json document for the workspace."""
    generated_at = generated_at or _utc_timestamp()
    git_commit = git_commit or _get_git_commit()
    validation = validation or validate_workspace(ctx["workspace_dir"])
    gap_analysis = gap_analysis or build_workspace_gap_analysis(ctx, validation=validation)

    gauge_props = ctx["gauge_feature"]["features"][0]["properties"]
    gauge_huc = ctx["huc_summary"]["gauge_huc12"][0]
    manifest = ctx["manifest"]
    downloads = manifest.get("downloads", {})
    analysis_extent_summary = ctx.get("analysis_extent_summary", {})

    terrain_sources = []
    for key in ("terrain_tiles_latest", "terrain_fallback_tiles_retained"):
        value = downloads.get(key)
        if isinstance(value, list):
            terrain_sources.extend(value)

    figures = [
        "terrain_overview",
        "nlcd_land_cover",
        "ssurgo_soils",
        "recent_continuous_record",
        "flow_duration_curve",
        "annual_peak_history",
    ]
    if ctx.get("precip_station_qaqc"):
        figures.append("station_precip_qaqc")

    return {
        "schema_version": _WORKSPACE_REPORT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "study": {
            "name": "Spring Creek Base Data Report",
            "workspace_dir": str(ctx["workspace_dir"]),
            "primary_gauge_id": "05577500",
            "default_processing_crs": "EPSG:5070",
            "git_commit": git_commit,
        },
        "artifacts": {
            "report_html": str(report_html_path) if report_html_path else None,
            "data_gap_analysis": "data_gap_analysis.json",
        },
        "gauge": {
            "site_id": "05577500",
            "station_name": gauge_props.get("name"),
            "monitoring_url": gauge_props.get("uri"),
            "nldi_comid": gauge_props.get("comid"),
            "reachcode": gauge_props.get("reachcode"),
            "continuous_last365d": ctx["gauge_summary"].get("continuous_last365d", []),
            "daily_period_of_record": ctx["gauge_summary"].get("daily_period_of_record", []),
            "annual_peak_count": len(ctx["peaks"]),
        },
        "basin": {
            "gauge_huc12": gauge_huc,
            "intersecting_huc12_count": ctx["huc_summary"].get("intersecting_huc12_count", 0),
            "intersecting_huc12": ctx["huc_summary"].get("intersecting_huc12", []),
            "basin_geojson": str(ctx["workspace_dir"] / "02_basin_outline" / "USGS_05577500_nldi_basin.geojson"),
            "upstream_flowlines_geojson": str(ctx["flowlines_path"]),
        },
        "analysis_extent": {
            "geojson": str(ctx["analysis_extent_path"]),
            "geojson_5070": str(ctx["analysis_extent_5070_path"]),
            "buffer_m": analysis_extent_summary.get("buffer_m"),
            "bbox_wgs84": analysis_extent_summary.get("bbox_wgs84"),
            "bbox_5070": analysis_extent_summary.get("bbox_5070"),
            "source_boundary": analysis_extent_summary.get("source_boundary"),
        },
        "terrain": {
            "dem_path": str(ctx["dem_path"]),
            "preferred_source": manifest.get("sources", {}).get("terrain_primary_image_server"),
            "fallback_source": manifest.get("sources", {}).get("terrain_fallback"),
            "source_tiles": terrain_sources,
        },
        "landcover": {
            "nlcd_path": str(ctx["nlcd_path"]),
            "source": manifest.get("sources", {}).get("nlcd_wcs"),
        },
        "soils": {
            "soil_geojson": str(ctx["soils_path"]),
            "soil_geojson_5070": str(ctx["soils_5070_path"]),
            "source": manifest.get("sources", {}).get("soils_wfs"),
        },
        "precipitation_qaqc": _workspace_precip_qaqc_json(ctx),
        "map_layers": [
            "Buffered analysis extent",
            "Intersecting HUC12 fill",
            "Gauge HUC12 highlight",
            "NLDI basin outline",
            "Upstream flowlines",
            "Gauge point",
        ],
        "figures": figures,
        "provenance": {
            "sources": manifest.get("sources", {}),
            "notes": manifest.get("notes", {}),
            "downloads": downloads,
        },
        "validation": validation,
        "data_gaps": {
            "count": gap_analysis.get("gap_count", 0),
            "ids": [gap["id"] for gap in gap_analysis.get("gaps", [])],
        },
    }


def write_workspace_report_package(
    workspace_dir: Path,
    *,
    output_dir: Optional[Path] = None,
    include_map: bool = True,
    issue_urls: Optional[dict[str, Optional[str]]] = None,
) -> dict[str, Path]:
    """
    Generate report.html, report.json, and data_gap_analysis.json for a workspace.
    """
    workspace_dir = Path(workspace_dir)
    output_dir = Path(output_dir) if output_dir else workspace_dir / "08_report"
    output_dir.mkdir(parents=True, exist_ok=True)

    report_html_path = generate_workspace_report(
        workspace_dir,
        output_path=output_dir / "report.html",
        include_map=include_map,
    )
    ctx = _load_workspace_context(workspace_dir)
    validation = validate_workspace(workspace_dir)
    generated_at = _utc_timestamp()
    gap_analysis = build_workspace_gap_analysis(
        ctx,
        validation=validation,
        issue_urls=issue_urls,
        generated_at=generated_at,
    )
    report_json = build_workspace_report_json(
        ctx,
        report_html_path=report_html_path,
        gap_analysis=gap_analysis,
        validation=validation,
        generated_at=generated_at,
    )

    report_json_path = output_dir / "report.json"
    gap_json_path = output_dir / "data_gap_analysis.json"
    report_json_path.write_text(json.dumps(report_json, indent=2), encoding="utf-8")
    gap_json_path.write_text(json.dumps(gap_analysis, indent=2), encoding="utf-8")
    logger.info(f"[Workspace Report] Wrote package JSON to {output_dir}")

    return {
        "report_html": report_html_path,
        "report_json": report_json_path,
        "data_gap_analysis": gap_json_path,
    }


def _serialize_geojson_source(gdf, keep_props: list[str], simplify_m: float = 0.0) -> dict:
    gdf = gdf.to_crs("EPSG:5070")
    if simplify_m > 0:
        gdf = gdf.copy()
        gdf["geometry"] = gdf.geometry.simplify(simplify_m, preserve_topology=True)
    gdf = gdf.to_crs("EPSG:4326")
    records = json.loads(gdf.to_json(drop_id=True))
    for feature in records.get("features", []):
        props = feature.get("properties", {}) or {}
        feature["properties"] = {k: props.get(k) for k in keep_props if k in props}
        if feature.get("geometry") is not None:
            feature["geometry"]["coordinates"] = _round_coords(feature["geometry"]["coordinates"])
    return records


def _workspace_map_payload(ctx: dict) -> dict:
    map_extent = ctx.get("analysis_extent", ctx["basin"])
    basin_bounds = list(map_extent.total_bounds)
    centroid = map_extent.geometry.iloc[0].centroid
    return {
        "bounds": [[round(basin_bounds[0], 6), round(basin_bounds[1], 6)], [round(basin_bounds[2], 6), round(basin_bounds[3], 6)]],
        "center": [round(float(centroid.x), 6), round(float(centroid.y), 6)],
        "sources": {
            "hucs": _serialize_geojson_source(ctx["hucs"], ["huc12", "huc12_name", "states"], simplify_m=20.0),
            "gauge_huc12": _serialize_geojson_source(ctx["gauge_huc12"], ["huc12", "huc12_name", "states"], simplify_m=20.0),
            "basin": _serialize_geojson_source(ctx["basin"], ["identifier", "name"], simplify_m=0.0),
            "flowlines": _serialize_geojson_source(ctx["flowlines"], ["nhdplus_comid"], simplify_m=15.0),
            "gauge": _serialize_geojson_source(ctx["gauge_point"], ["identifier", "name", "comid"], simplify_m=0.0),
        },
    }


def _plot_workspace_terrain(ctx: dict) -> str:
    import numpy as np
    import rasterio
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with rasterio.open(ctx["dem_path"]) as src:
        dem = src.read(
            1,
            out_shape=(1, max(1, src.height // 4), max(1, src.width // 4)),
            masked=True,
            resampling=rasterio.enums.Resampling.bilinear,
        )
        bounds = src.bounds
        xres = abs(src.transform.a) * (src.width / dem.shape[1])
        yres = abs(src.transform.e) * (src.height / dem.shape[0])

    filled = dem.filled(np.nan)
    finite = np.isfinite(filled)
    fill_value = float(np.nanmedian(filled[finite])) if finite.any() else 0.0
    surface = np.where(finite, filled, fill_value)
    gy, gx = np.gradient(surface, yres, xres)
    slope = np.pi / 2.0 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az = np.deg2rad(315.0)
    alt = np.deg2rad(45.0)
    shade = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    shade = np.clip((shade + 1.0) / 2.0, 0.0, 1.0)
    norm = (surface - np.nanmin(surface)) / max(np.nanmax(surface) - np.nanmin(surface), 1e-6)
    rgb = plt.cm.gist_earth(norm)
    rgb[..., :3] = rgb[..., :3] * 0.58 + shade[..., None] * 0.42

    fig, ax = plt.subplots(figsize=(8.8, 6.6))
    ax.imshow(rgb, extent=[bounds.left, bounds.right, bounds.bottom, bounds.top], origin="upper")
    ctx["basin_5070"].boundary.plot(ax=ax, color="#f4f6f8", linewidth=2.2)
    ctx["flowlines_5070"].plot(ax=ax, color="#0f6db0", linewidth=0.8, alpha=0.85)
    ctx["gauge_point_5070"].plot(ax=ax, color="#d73027", markersize=46, zorder=5)
    ax.set_title("Terrain Overview — ILHMP / CHAMP Sangamon County 2018 DEM")
    ax.set_xlabel("Easting (m, EPSG:5070)")
    ax.set_ylabel("Northing (m, EPSG:5070)")
    fig.tight_layout()
    data = _b64_png(fig)
    plt.close(fig)
    return data


def _plot_workspace_nlcd(ctx: dict) -> tuple[str, list[tuple[int, str]]]:
    import numpy as np
    import rasterio
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap, BoundaryNorm
    from matplotlib.patches import Patch

    nlcd_meta = {
        11: ("Open Water", "#466b9f"),
        21: ("Developed, Open Space", "#dec5c5"),
        22: ("Developed, Low Intensity", "#d99282"),
        23: ("Developed, Medium Intensity", "#eb0000"),
        24: ("Developed, High Intensity", "#ab0000"),
        31: ("Barren Land", "#b3ac9f"),
        41: ("Deciduous Forest", "#68ab5f"),
        42: ("Evergreen Forest", "#1c5f2c"),
        43: ("Mixed Forest", "#b5c58f"),
        52: ("Shrub/Scrub", "#ccb879"),
        71: ("Grassland/Herbaceous", "#dfdfc2"),
        81: ("Pasture/Hay", "#d1d182"),
        82: ("Cultivated Crops", "#a3cc51"),
        90: ("Woody Wetlands", "#7aa873"),
        95: ("Emergent Herbaceous Wetlands", "#dcd939"),
    }

    with rasterio.open(ctx["nlcd_path"]) as src:
        arr = src.read(1)
        bounds = src.bounds
    classes = sorted(int(v) for v in np.unique(arr) if int(v) in nlcd_meta)
    cmap = ListedColormap([nlcd_meta[v][1] for v in classes])
    norm = BoundaryNorm(classes + [classes[-1] + 1], cmap.N)

    fig, ax = plt.subplots(figsize=(8.8, 6.6))
    ax.imshow(arr, extent=[bounds.left, bounds.right, bounds.bottom, bounds.top], origin="upper", cmap=cmap, norm=norm, interpolation="nearest")
    ctx["basin_5070"].boundary.plot(ax=ax, color="#1b3c59", linewidth=1.6)
    ax.set_title("NLCD 2021 Land Cover — Shared Analysis Extent")
    ax.set_xlabel("Easting (m, EPSG:5070)")
    ax.set_ylabel("Northing (m, EPSG:5070)")
    handles = [Patch(facecolor=nlcd_meta[v][1], edgecolor="none", label=f"{v} — {nlcd_meta[v][0]}") for v in classes]
    ax.legend(handles=handles, fontsize=7.5, loc="upper left", frameon=True, framealpha=0.95)
    fig.tight_layout()
    data = _b64_png(fig)
    plt.close(fig)
    return data, [(v, nlcd_meta[v][0]) for v in classes]


def _plot_workspace_soils(ctx: dict) -> tuple[str, list[list[object]]]:
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import to_hex

    soils = ctx["soils_5070"].copy()
    soils["muareaacres"] = soils["muareaacres"].astype(float)
    dominant = (
        soils.groupby(["musym", "nationalmusym"], dropna=False)["muareaacres"]
        .sum()
        .reset_index()
        .sort_values("muareaacres", ascending=False)
    )
    top = dominant.head(10).copy()
    top_symbols = set(top["musym"].fillna("Unknown"))
    soils["plot_group"] = soils["musym"].fillna("Unknown").where(soils["musym"].fillna("Unknown").isin(top_symbols), "Other")

    palette = plt.cm.tab20(np.linspace(0.02, 0.92, max(len(top_symbols), 1)))
    color_map = {sym: to_hex(color) for sym, color in zip(sorted(top_symbols), palette)}
    color_map["Other"] = "#d9dde2"

    fig, ax = plt.subplots(figsize=(8.8, 6.6))
    for group, subset in soils.groupby("plot_group"):
        subset.plot(ax=ax, color=color_map[group], linewidth=0.05, edgecolor="white", alpha=0.95)
    ctx["basin_5070"].boundary.plot(ax=ax, color="#173a5e", linewidth=1.2)
    ctx["gauge_point_5070"].plot(ax=ax, color="#c0362c", markersize=20, zorder=5)
    ax.set_title("SSURGO Soils — Shared Analysis Extent")
    ax.set_xlabel("Easting (m, EPSG:5070)")
    ax.set_ylabel("Northing (m, EPSG:5070)")
    fig.tight_layout()
    data = _b64_png(fig)
    plt.close(fig)
    legend_rows = [[row["musym"], row["nationalmusym"], f'{row["muareaacres"]:,.1f}'] for _, row in top.iterrows()]
    return data, legend_rows


def _plot_workspace_recent_gauge(ctx: dict) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(10.5, 6.8), sharex=True)
    axes[0].plot(ctx["iv_flow"]["datetime"], ctx["iv_flow"]["value"], color="#0b6aa9", linewidth=0.9)
    axes[0].set_ylabel("Flow (cfs)")
    axes[0].set_title("Recent Continuous Gauge Data — Last 365 Days")
    axes[0].grid(True, linestyle="--", alpha=0.25)
    axes[1].plot(ctx["iv_stage"]["datetime"], ctx["iv_stage"]["value"], color="#d04f35", linewidth=0.9)
    axes[1].set_ylabel("Stage (ft)")
    axes[1].set_xlabel("Date")
    axes[1].grid(True, linestyle="--", alpha=0.25)
    fig.tight_layout()
    data = _b64_png(fig)
    plt.close(fig)
    return data


def _plot_workspace_flow_duration(ctx: dict) -> str:
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    flows = ctx["dv_flow"]["value"].dropna().astype(float)
    if flows.empty:
        return ""
    sorted_flows = np.sort(flows.to_numpy())[::-1]
    exceedance = (np.arange(1, len(sorted_flows) + 1) / (len(sorted_flows) + 1)) * 100.0
    fig, ax = plt.subplots(figsize=(9.2, 5.8))
    ax.plot(exceedance, sorted_flows, color="#173a5e", linewidth=1.4)
    ax.set_yscale("log")
    ax.set_xlabel("Exceedance Probability (%)")
    ax.set_ylabel("Daily Flow (cfs)")
    ax.set_title("Flow Duration Curve — Daily Period of Record")
    ax.grid(True, which="both", linestyle="--", alpha=0.25)
    fig.tight_layout()
    data = _b64_png(fig)
    plt.close(fig)
    return data


def _plot_workspace_annual_peaks(ctx: dict) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    peaks = ctx["peaks"].sort_values("peak_dt").copy()
    if peaks.empty:
        return ""
    peaks["rolling5"] = peaks["peak_va"].rolling(window=5, center=True, min_periods=3).median()
    fig, ax = plt.subplots(figsize=(9.2, 5.8))
    ax.scatter(peaks["peak_dt"], peaks["peak_va"], s=22, color="#0f6db0", alpha=0.85, label="Annual peaks")
    ax.plot(peaks["peak_dt"], peaks["rolling5"], color="#d95f02", linewidth=1.7, label="5-peak rolling median")
    ax.set_ylabel("Peak Flow (cfs)")
    ax.set_xlabel("Peak Date")
    ax.set_title("Annual Peak Flow History")
    ax.grid(True, linestyle="--", alpha=0.25)
    ax.legend()
    fig.tight_layout()
    data = _b64_png(fig)
    plt.close(fig)
    return data


def _workspace_summary_cards(ctx: dict) -> list[tuple[str, str]]:
    gauge_feature = ctx["gauge_feature"]["features"][0]["properties"]
    gauge_huc = ctx["huc_summary"]["gauge_huc12"][0]
    return [
        ("Gauge", "USGS-05577500"),
        ("Station", "SPRING CREEK AT SPRINGFIELD, IL"),
        ("NLDI COMID", str(gauge_feature.get("comid", "—"))),
        ("Gauge HUC12", f'{gauge_huc["huc12"]}'),
        ("Intersecting HUC12s", str(ctx["huc_summary"]["intersecting_huc12_count"])),
        ("Annual Peaks", str(len(ctx["peaks"]))),
    ]


def _section_workspace_header(ctx: dict, git_commit: str, run_ts: str) -> str:
    cards = "".join(
        f'<div class="summary-card"><div class="label">{label}</div><div class="value">{value}</div></div>'
        for label, value in _workspace_summary_cards(ctx)
    )
    return f"""\
<section class="page-header">
  <h1>Spring Creek Base Data Report</h1>
  <p class="lede">
    Self-contained workspace report for the upstream drainage area to USGS gauge <span class="codeish">05577500</span>.
    This report packages observed data, watershed vector context, terrain, land cover, soils, and provenance for
    Spring Creek at Springfield, Illinois.
  </p>
  <p class="meta">
    Report generated: {run_ts}<br>
    Source workspace: <code>{ctx["workspace_dir"]}</code><br>
    Git commit: {git_commit}
  </p>
  <div class="summary-grid">{cards}</div>
</section>
"""


def _section_workspace_map() -> str:
    return """\
<section class="section">
  <h2>Interactive Vector Context Map</h2>
  <p>
    This map is fully self-contained. It uses inline MapLibre sources with no external basemap or tile service.
    The backdrop is intentionally blank so the report remains portable and offline-safe.
  </p>
  <div class="map-wrap">
    <div class="map-panel"><div id="spring-creek-map"></div></div>
    <div class="map-sidebar">
      <h3>Layers</h3>
      <label class="layer-control"><input type="checkbox" data-layer="hucs-fill" checked /> Intersecting HUC12 fill</label>
      <label class="layer-control"><input type="checkbox" data-layer="hucs-line" checked /> Intersecting HUC12 outlines</label>
      <label class="layer-control"><input type="checkbox" data-layer="site-huc-fill" checked /> Gauge HUC12 highlight</label>
      <label class="layer-control"><input type="checkbox" data-layer="site-huc-line" checked /> Gauge HUC12 outline</label>
      <label class="layer-control"><input type="checkbox" data-layer="basin-line" checked /> NLDI basin outline</label>
      <label class="layer-control"><input type="checkbox" data-layer="flowlines" checked /> Upstream flowlines</label>
      <label class="layer-control"><input type="checkbox" data-layer="gauge-point" checked /> Gauge point</label>
      <p class="tiny-note">
        Layer popups are available on the gauge point and HUC polygons. The map defaults to the official basin bounds.
      </p>
      <button id="reset-map-view" type="button">Reset View</button>
    </div>
  </div>
</section>
"""


def _section_workspace_context(ctx: dict) -> str:
    gauge_props = ctx["gauge_feature"]["features"][0]["properties"]
    gauge_summary = ctx["gauge_summary"]
    huc = ctx["huc_summary"]["gauge_huc12"][0]
    rows = [
        ["Gauge ID", "USGS-05577500"],
        ["Station name", gauge_props.get("name")],
        ["Monitoring URL", gauge_props.get("uri")],
        ["NLDI COMID", gauge_props.get("comid")],
        ["Reachcode", gauge_props.get("reachcode")],
        ["Gauge HUC12", f'{huc["huc12"]} — {huc["huc12_name"]}'],
        ["Intersecting HUC12 count", ctx["huc_summary"]["intersecting_huc12_count"]],
        ["Continuous flow records (365d)", gauge_summary["continuous_last365d"][0]["record_count"]],
        ["Daily flow records", gauge_summary["daily_period_of_record"][0]["record_count"]],
        ["Annual peaks", len(ctx["peaks"])],
    ]
    huc_rows = [[item["huc12"], item["huc12_name"], item["states"]] for item in ctx["huc_summary"]["intersecting_huc12"]]
    html = '<section class="section"><h2>Gauge And Basin Context</h2>'
    html += _html_table_safe(["Field", "Value"], rows)
    html += "<h3>Intersecting HUC12 Watersheds</h3>"
    html += _html_table_safe(["HUC12", "Name", "States"], huc_rows)
    html += "</section>"
    return html


def _section_workspace_landscape(ctx: dict) -> str:
    terrain_png = _plot_workspace_terrain(ctx)
    nlcd_png, nlcd_classes = _plot_workspace_nlcd(ctx)
    soils_png, dominant_soils = _plot_workspace_soils(ctx)
    class_rows = [[code, label] for code, label in nlcd_classes]
    return f"""\
<section class="section">
  <h2>Landscape Inputs</h2>
  <div class="figure-grid">
    <div class="figure-card">
      <h3>Terrain</h3>
      <img src="{terrain_png}" alt="Terrain overview" />
      <div class="figure-caption">Basin-clipped ILHMP / CHAMP DEM with flowlines and gauge location overlaid.</div>
    </div>
    <div class="figure-card">
      <h3>NLCD 2021 Land Cover</h3>
      <img src="{nlcd_png}" alt="NLCD 2021 land cover" />
      <div class="figure-caption">Categorical land cover clipped to the shared buffered analysis extent.</div>
      {_html_table_safe(["Code", "Class"], class_rows)}
    </div>
    <div class="figure-card">
      <h3>SSURGO Soils</h3>
      <img src="{soils_png}" alt="SSURGO soils" />
      <div class="figure-caption">Dominant map units are highlighted individually within the shared buffered analysis extent; smaller units are grouped as <span class="codeish">Other</span>.</div>
      {_html_table_safe(["MUSYM", "National Symbol", "Area (acres)"], dominant_soils, table_class="soil-table")}
    </div>
  </div>
</section>
"""


def _section_workspace_observed(ctx: dict) -> str:
    recent_png = _plot_workspace_recent_gauge(ctx)
    fdc_png = _plot_workspace_flow_duration(ctx)
    peaks_png = _plot_workspace_annual_peaks(ctx)

    flow_recent = ctx["iv_flow"]["value"].dropna()
    stage_recent = ctx["iv_stage"]["value"].dropna()
    dv_flow = ctx["dv_flow"]["value"].dropna()
    peak_vals = ctx["peaks"]["peak_va"].dropna()
    summary_rows = [
        ["Recent flow period", f'{ctx["iv_flow"]["datetime"].min().date()} to {ctx["iv_flow"]["datetime"].max().date()}'],
        ["Recent flow range", f"{flow_recent.min():,.1f} to {flow_recent.max():,.1f} cfs"],
        ["Recent stage range", f"{stage_recent.min():.2f} to {stage_recent.max():.2f} ft"],
        ["Daily flow period of record", f'{ctx["dv_flow"]["datetime"].min().date()} to {ctx["dv_flow"]["datetime"].max().date()}'],
        ["Daily flow median", f"{dv_flow.median():,.1f} cfs"],
        ["Annual peak maximum", f"{peak_vals.max():,.0f} cfs"],
    ]
    return f"""\
<section class="section">
  <h2>Observed Gauge Data</h2>
  {_html_table_safe(["Metric", "Value"], summary_rows)}
  <div class="figure-grid">
    <div class="figure-card">
      <h3>Recent Continuous Record</h3>
      <img src="{recent_png}" alt="Recent continuous gauge data" />
      <div class="figure-caption">Continuous flow and stage from the last 365 days downloaded from USGS NWIS water services.</div>
    </div>
    <div class="figure-card">
      <h3>Flow Duration Curve</h3>
      <img src="{fdc_png}" alt="Flow duration curve" />
      <div class="figure-caption">Daily period-of-record flow values represented as exceedance probability.</div>
    </div>
    <div class="figure-card">
      <h3>Annual Peak History</h3>
      <img src="{peaks_png}" alt="Annual peak history" />
      <div class="figure-caption">Annual peak-flow series with a centered 5-peak rolling median for visual context.</div>
    </div>
  </div>
</section>
"""


def _resolve_precip_artifact_path(ctx: dict, path_value: object) -> Optional[Path]:
    if not path_value:
        return None
    path = Path(str(path_value))
    if path.is_absolute():
        return path
    base = ctx.get("precip_station_qaqc_path")
    if base is not None:
        return Path(base).parent / path
    return Path(ctx["workspace_dir"]) / "08_report" / path


def _fmt_number(value: object, digits: int = 2, suffix: str = "") -> str:
    try:
        if value is None:
            return "—"
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _section_workspace_precip_qaqc(ctx: dict) -> str:
    precip_qaqc = ctx.get("precip_station_qaqc")
    html = '<section class="section"><h2>Station Precipitation QAQC</h2>'
    if not precip_qaqc:
        html += (
            "<p>GHCND/station precipitation comparison evidence was not found "
            "for this workspace package.</p>"
        )
        html += _html_table_safe(
            ["Field", "Value"],
            [
                ["Status", "Missing station precipitation QAQC artifact"],
                ["Expected artifact", "08_report/station_precip_qaqc.json"],
                ["Model readiness impact", "Recorded as a data gap"],
            ],
        )
        html += "</section>"
        return html

    event = precip_qaqc.get("event", {})
    grid = precip_qaqc.get("grid", {})
    station_network = precip_qaqc.get("station_network", {})
    summary = precip_qaqc.get("summary", {})
    flags = precip_qaqc.get("flags", [])
    stations = precip_qaqc.get("stations", [])

    summary_rows = [
        ["Event start", event.get("start_time")],
        ["Event end", event.get("end_time")],
        ["Grid source", grid.get("source")],
        ["Grid depth", _fmt_number(grid.get("depth_in"), suffix=" in")],
        ["Search radius", _fmt_number(station_network.get("search_radius_mi"), suffix=" mi")],
        ["Station count", summary.get("station_count")],
        ["Valid observations", summary.get("valid_observation_count")],
        ["Missing observations", summary.get("missing_observation_count")],
        ["Median observed depth", _fmt_number(summary.get("observed_depth_median_in"), suffix=" in")],
        ["Median station/grid ratio", _fmt_number(summary.get("station_to_grid_ratio_median"))],
        ["Assessment", summary.get("assessment")],
    ]
    html += _html_table_safe(["Field", "Value"], summary_rows)

    figure_path = _resolve_precip_artifact_path(
        ctx,
        precip_qaqc.get("artifacts", {}).get("figure_png"),
    )
    figure_data = _file_data_uri(figure_path) if figure_path else ""
    if figure_data:
        html += f"""
  <div class="figure-grid">
    <div class="figure-card">
      <h3>Station Comparison</h3>
      <img src="{figure_data}" alt="Station precipitation comparison" />
      <div class="figure-caption">Observed station storm depths compared with the gridded forcing depth used for review.</div>
    </div>
  </div>
"""

    flag_rows = (
        [[flag.get("id"), flag.get("severity"), flag.get("message")] for flag in flags]
        if flags else
        [["none", "none", "No station precipitation QAQC flags were raised."]]
    )
    html += "<h3>Flags</h3>"
    html += _html_table_safe(["Flag", "Severity", "Message"], flag_rows)

    station_rows = [
        [
            row.get("station_id"),
            row.get("station_name"),
            _fmt_number(row.get("distance_mi"), suffix=" mi"),
            _fmt_number(row.get("observed_depth_in"), suffix=" in"),
            _fmt_number(row.get("gridded_depth_in"), suffix=" in"),
            _fmt_number(row.get("station_to_grid_ratio")),
            row.get("status"),
        ]
        for row in stations
    ]
    if station_rows:
        html += "<h3>Station Comparison Table</h3>"
        html += _html_table_safe(
            ["Station ID", "Name", "Distance", "Observed", "Gridded", "Ratio", "Status"],
            station_rows,
        )

    html += "</section>"
    return html


def _section_workspace_inventory(ctx: dict) -> str:
    manifest = ctx["manifest"]
    rows = []
    for key, value in manifest.get("downloads", {}).items():
        rows.append([key, f"{len(value)} item(s)" if isinstance(value, list) else value])
    html = '<section class="section"><h2>Data Inventory And Provenance</h2>'
    html += _html_table_safe(["Artifact", "Value"], rows)
    html += "<h3>Source Endpoints</h3>"
    html += _html_table_safe(["Source", "Endpoint"], [[k, v] for k, v in manifest.get("sources", {}).items()])
    if manifest.get("notes"):
        html += "<h3>Notes</h3>"
        html += _html_table_safe(["Item", "Note"], [[k, v] for k, v in manifest["notes"].items()])
    html += "</section>"
    return html


def _workspace_map_script(map_payload: dict) -> str:
    payload_json = json.dumps(map_payload, separators=(",", ":"))
    return f"""\
<script type="application/json" id="spring-creek-map-data">{payload_json}</script>
<script>
(function() {{
  const payload = JSON.parse(document.getElementById('spring-creek-map-data').textContent);
  const map = new maplibregl.Map({{
    container: 'spring-creek-map',
    style: {{
      version: 8,
      sources: {{}},
      layers: [{{ id: 'background', type: 'background', paint: {{ 'background-color': '#edf2f7' }} }}]
    }},
    center: payload.center,
    zoom: 9,
    attributionControl: false,
    maplibreLogo: false
  }});
  map.addControl(new maplibregl.NavigationControl({{ showCompass: false }}), 'top-right');
  map.on('load', function() {{
    Object.entries(payload.sources).forEach(([name, data]) => map.addSource(name, {{ type: 'geojson', data }}));
    map.addLayer({{ id: 'hucs-fill', type: 'fill', source: 'hucs', paint: {{ 'fill-color': '#d8e6f2', 'fill-opacity': 0.45 }} }});
    map.addLayer({{ id: 'hucs-line', type: 'line', source: 'hucs', paint: {{ 'line-color': '#8aa1b5', 'line-width': 1 }} }});
    map.addLayer({{ id: 'site-huc-fill', type: 'fill', source: 'gauge_huc12', paint: {{ 'fill-color': '#f7c873', 'fill-opacity': 0.35 }} }});
    map.addLayer({{ id: 'site-huc-line', type: 'line', source: 'gauge_huc12', paint: {{ 'line-color': '#b06d00', 'line-width': 2 }} }});
    map.addLayer({{ id: 'basin-line', type: 'line', source: 'basin', paint: {{ 'line-color': '#173a5e', 'line-width': 3 }} }});
    map.addLayer({{ id: 'flowlines', type: 'line', source: 'flowlines', paint: {{ 'line-color': '#0077b6', 'line-width': 1.6 }} }});
    map.addLayer({{ id: 'gauge-point', type: 'circle', source: 'gauge', paint: {{ 'circle-radius': 6, 'circle-color': '#d73027', 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 2 }} }});
    map.fitBounds(payload.bounds, {{ padding: 32, duration: 0 }});
    document.querySelectorAll('[data-layer]').forEach((checkbox) => {{
      checkbox.addEventListener('change', () => {{
        const layerId = checkbox.getAttribute('data-layer');
        map.setLayoutProperty(layerId, 'visibility', checkbox.checked ? 'visible' : 'none');
      }});
    }});
    document.getElementById('reset-map-view').addEventListener('click', () => map.fitBounds(payload.bounds, {{ padding: 32 }}));
    const popup = new maplibregl.Popup({{ closeButton: false, closeOnClick: false }});
    function addPopup(layerId, htmlBuilder) {{
      map.on('mousemove', layerId, (e) => {{
        map.getCanvas().style.cursor = 'pointer';
        const feature = e.features && e.features[0];
        if (!feature) return;
        popup.setLngLat(e.lngLat).setHTML(htmlBuilder(feature.properties || {{}})).addTo(map);
      }});
      map.on('mouseleave', layerId, () => {{ map.getCanvas().style.cursor = ''; popup.remove(); }});
    }}
    addPopup('gauge-point', (p) => `<strong>${{p.name || 'Gauge'}}</strong><br>${{p.identifier || ''}}<br>COMID: ${{p.comid || ''}}`);
    addPopup('site-huc-fill', (p) => `<strong>${{p.huc12_name || 'Gauge HUC12'}}</strong><br>HUC12: ${{p.huc12 || ''}}`);
    addPopup('hucs-fill', (p) => `<strong>${{p.huc12_name || 'HUC12'}}</strong><br>HUC12: ${{p.huc12 || ''}}`);
  }});
}})();
</script>
"""


def generate_workspace_report(
    workspace_dir: Path,
    output_path: Optional[Path] = None,
    include_map: bool = True,
) -> Path:
    """
    Generate a self-contained HTML report for a prepared workspace dataset.
    """
    workspace_dir = Path(workspace_dir)
    if output_path is None:
        output_path = workspace_dir / "08_report" / "report.html"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ctx = _load_workspace_context(workspace_dir)
    git_commit = _get_git_commit()
    run_ts = _utc_timestamp()

    map_css = ""
    map_js = ""
    map_section = ""
    map_script = ""
    if include_map:
        map_css, map_js = _load_maplibre_assets()
        map_section = _section_workspace_map()
        map_script = _workspace_map_script(_workspace_map_payload(ctx))

    sections = [
        _section_workspace_header(ctx, git_commit, run_ts),
        map_section,
        _section_workspace_context(ctx),
        _section_workspace_landscape(ctx),
        _section_workspace_observed(ctx),
        _section_workspace_precip_qaqc(ctx),
        _section_workspace_inventory(ctx),
    ]

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Spring Creek Base Data Report</title>
  <style>
{_CSS}
{_WORKSPACE_CSS}
{map_css}
  </style>
</head>
<body class="workspace-report">
{''.join(section for section in sections if section)}
  <div class="footer">
    Generated by RAS Agent workspace reporting using MapLibre GL JS v{_MAPLIBRE_VERSION} (3-Clause BSD),
    inline figures, and local Spring Creek source artifacts.
  </div>
  <script>
{map_js}
  </script>
{map_script}
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    logger.info(f"[Workspace Report] Written to {output_path}")
    return output_path
