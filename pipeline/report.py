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
import io
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_VERSION = "0.1.0"

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
    run_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

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
