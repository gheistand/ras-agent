"""
Run CLB-662 Spring Creek calibration iterations and write review artifacts.

This is an issue-specific orchestration script. It consumes the completed
CLB-660 spatial-parameter model package and the CLB-661 observed event package,
executes HEC-RAS 6.6 runs, extracts the gauge-point time series, computes GOF
metrics, and writes plots/tables for the calibration notebook.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_DIR = REPO_ROOT / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import metrics  # noqa: E402
import results  # noqa: E402


ISSUE = "CLB-662"
AREA_NAME = "MainArea"
GAUGE_LON = -89.6994167
GAUGE_LAT = 39.81541667
SOURCE_MODEL = Path(
    r"H:/Symphony/ras-agent/CLB-660/workspace-outputs/"
    r"clb660_spatial_params/ras_agent_103mi2"
)
EVENT_WORKSPACE = Path(r"H:/Symphony/ras-agent/CLB-661/workspace")
ARTIFACT_ROOT = Path(r"H:/Symphony/ras-agent/CLB-662")
OUTPUT_ROOT = ARTIFACT_ROOT / "calibration_outputs"
WORK_ROOT = REPO_ROOT / "workspace" / "clb662_calibration"
PROJECT_NAME = "ras_agent_103mi2"
PLAN_NUMBER = "01"
HECRAS_EVENT_HOURS: int | None = None


@dataclass
class RunConfig:
    name: str
    label: str
    mannings_scale: float = 1.0
    cn_scale: float = 1.0


def _read_event() -> dict[str, Any]:
    with (EVENT_WORKSPACE / "calibration_event.json").open("r", encoding="utf-8") as f:
        event = json.load(f)
    return event


def _event_path(event: dict[str, Any], key: str) -> Path:
    raw = Path(event[key])
    if raw.is_absolute():
        return raw
    if raw.parts and raw.parts[0].lower() == "workspace":
        return EVENT_WORKSPACE / Path(*raw.parts[1:])
    return EVENT_WORKSPACE / raw


def _hecras_timestamp(timestamp: str) -> str:
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.strftime("%d%b%Y,%H%M").upper()


def _format_hydrograph_values(values: list[float]) -> list[str]:
    lines = []
    for i in range(0, len(values), 10):
        chunk = values[i:i + 10]
        lines.append("".join(f"{value:8.4f}" for value in chunk))
    return lines


def _replace_precip_hydrograph(text: str, values: list[float], interval: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("Interval="):
            out.append(f"Interval={interval}")
            i += 1
            continue
        if line.startswith("Precipitation Hydrograph="):
            out.append(f"Precipitation Hydrograph={len(values):4d} ")
            out.extend(_format_hydrograph_values(values))
            i += 1
            while i < len(lines) and not lines[i].startswith("DSS Path="):
                i += 1
            replaced = True
            continue
        out.append(line)
        i += 1
    if not replaced:
        raise RuntimeError("Could not find Precipitation Hydrograph block in u01")
    return "\n".join(out) + "\n"


def _hyetograph_frame(event: dict[str, Any]) -> pd.DataFrame:
    hyetograph = pd.read_csv(_event_path(event, "hyetograph_csv_path"))
    hourly = hyetograph.copy()
    hourly["datetime_utc"] = pd.to_datetime(hourly["datetime_utc"], utc=True)
    hourly["precip_in"] = hourly["precip_in"].fillna(0.0).clip(lower=0.0)
    return hourly


def _hecras_event_window(event: dict[str, Any]) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Use the AORC hourly timestamps as the HEC-RAS-compatible event window."""
    hourly = _hyetograph_frame(event)
    start = pd.Timestamp(hourly["datetime_utc"].iloc[0]).tz_convert("UTC")
    package_end = pd.Timestamp(hourly["datetime_utc"].iloc[-1]).tz_convert("UTC")
    end = (
        min(start + pd.Timedelta(hours=HECRAS_EVENT_HOURS), package_end)
        if HECRAS_EVENT_HOURS is not None
        else package_end
    )
    return start, end


def _precip_values_hourly(event: dict[str, Any]) -> list[float]:
    """Return hourly AORC precipitation depths for HEC-RAS forcing."""
    start, end = _hecras_event_window(event)
    hourly = _hyetograph_frame(event)
    hourly = hourly[(hourly["datetime_utc"] >= start) & (hourly["datetime_utc"] <= end)]
    return hourly["precip_in"].tolist()


def _configure_text_files(model_dir: Path, event: dict[str, Any]) -> None:
    plan = model_dir / "ras_agent_103mi2.p01"
    flow = model_dir / "ras_agent_103mi2.u01"

    plan_text = plan.read_text(encoding="utf-8")
    hecras_start, hecras_end = _hecras_event_window(event)
    sim_date = (
        f"Simulation Date={_hecras_timestamp(hecras_start.isoformat())},"
        f"{_hecras_timestamp(hecras_end.isoformat())}"
    )
    replacements = {
        r"^Plan Title=.*$": "Plan Title=CLB-662 Spring Creek Calibration",
        r"^Short ID=.*$": "Short ID=CLB662",
        r"^Simulation Date=.*$": sim_date,
        r"^Run HTab=.*$": "Run HTab=-1",
        r"^Run UNet=.*$": "Run UNet= -1",
        r"^Output Interval=.*$": "Output Interval=1HOUR",
        r"^Instantaneous Interval=.*$": "Instantaneous Interval=1HOUR",
        r"^Mapping Interval=.*$": "Mapping Interval=1HOUR",
    }
    for pattern, value in replacements.items():
        plan_text = re.sub(pattern, value, plan_text, flags=re.MULTILINE)
    plan.write_text(plan_text, encoding="utf-8", newline="\r\n")

    precip_in = _precip_values_hourly(event)
    interval = "1HOUR"
    flow_text = flow.read_text(encoding="utf-8")
    flow.write_text(
        _replace_precip_hydrograph(flow_text, precip_in, interval=interval),
        encoding="utf-8",
        newline="\r\n",
    )


def _scale_dataset(ds: h5py.Dataset, scale: float, min_value: float, max_value: float) -> None:
    values = np.asarray(ds, dtype=np.float32)
    scaled = np.clip(values * scale, min_value, max_value).astype(np.float32)
    ds[...] = scaled


def _apply_parameter_scales(model_dir: Path, config: RunConfig) -> None:
    if config.mannings_scale == 1.0 and config.cn_scale == 1.0:
        return

    for hdf_path in (model_dir / "ras_agent_103mi2.g01.hdf", model_dir / "ras_agent_103mi2.p01.hdf"):
        with h5py.File(hdf_path, "r+") as hf:
            area = "Geometry/2D Flow Areas/MainArea"
            if config.mannings_scale != 1.0:
                for path in (
                    f"{area}/Cells Center Manning's n",
                    f"{area}/Mann",
                ):
                    if path in hf:
                        _scale_dataset(hf[path], config.mannings_scale, 0.02, 0.18)
            if config.cn_scale != 1.0:
                path = f"{area}/Infiltration/Curve Number"
                if path in hf:
                    _scale_dataset(hf[path], config.cn_scale, 30.0, 100.0)


def _clear_previous_results(model_dir: Path) -> None:
    """Remove stale output evidence while preserving preprocessed hydraulic tables."""
    for suffix in (
        ".p01.hdf",
        ".p01.tmp.hdf",
        ".bco01",
        ".dss",
        ".p01.computeMsgs.txt",
        ".p01.comp_msgs.txt",
    ):
        path = model_dir / f"{PROJECT_NAME}{suffix}"
        if path.exists():
            path.unlink()


def _prepare_model(run_dir: Path, config: RunConfig, event: dict[str, Any]) -> Path:
    model_dir = run_dir / "model"
    if model_dir.exists():
        shutil.rmtree(model_dir)
    shutil.copytree(SOURCE_MODEL, model_dir)
    _configure_text_files(model_dir, event)
    _apply_parameter_scales(model_dir, config)
    _clear_previous_results(model_dir)
    return model_dir


def _run_hecras(model_dir: Path, run_dir: Path) -> dict[str, Any]:
    from ras_commander import RasCmdr, init_ras_project

    ras_object = init_ras_project(
        model_dir,
        "6.6",
        ras_object="new",
        load_results_summary=False,
    )
    compute_result = RasCmdr.compute_plan(
        PLAN_NUMBER,
        ras_object=ras_object,
        clear_geompre=True,
        force_geompre=True,
        force_rerun=True,
        verify=True,
        num_cores=6,
    )
    result_row = None
    if compute_result.results_df_row is not None:
        result_row = compute_result.results_df_row.to_dict()

    payload = {
        "executor": "ras_commander.RasCmdr.compute_plan",
        "plan_number": PLAN_NUMBER,
        "success": bool(compute_result),
        "result_repr": repr(compute_result),
        "results_df_row": result_row,
    }
    (run_dir / "hecras_compute_result.json").write_text(
        json.dumps(_json_safe(payload), indent=2),
        encoding="utf-8",
    )
    print(json.dumps(_json_safe(payload), indent=2))
    return {
        **payload,
        "returncode": 0 if compute_result else 1,
    }


def _compute_messages(hdf_path: Path) -> str:
    with h5py.File(hdf_path, "r") as hf:
        ds = hf.get("Results/Summary/Compute Messages (text)")
        raw = ds[0] if ds is not None and len(ds) else b""
    hdf_messages = (
        raw.decode("utf-8", errors="ignore")
        if isinstance(raw, bytes)
        else str(raw)
    )

    bco_path = hdf_path.with_name(f"{PROJECT_NAME}.bco01")
    bco_messages = ""
    if bco_path.exists():
        bco_messages = bco_path.read_text(encoding="utf-8", errors="ignore")
    return "\n".join(part for part in (hdf_messages, bco_messages) if part.strip())


def _observed_frame(event: dict[str, Any]) -> pd.DataFrame:
    observed = pd.read_csv(_event_path(event, "observed_csv_path"), parse_dates=["datetime_utc"])
    observed["datetime_utc"] = pd.to_datetime(observed["datetime_utc"], utc=True)
    return observed.set_index("datetime_utc").sort_index()


def _datum_adjust_stage(observed: pd.DataFrame, modeled: pd.DataFrame) -> pd.Series:
    aligned = pd.concat(
        [observed["stage_ft"].rename("obs"), modeled["stage_ft"].rename("mod")],
        axis=1,
        join="inner",
    ).dropna()
    if aligned.empty:
        return modeled["stage_ft"]
    offset = float(aligned["mod"].iloc[0] - aligned["obs"].iloc[0])
    return modeled["stage_ft"] - offset


def _score(observed: pd.DataFrame, modeled: pd.DataFrame) -> dict[str, float]:
    modeled_stage = _datum_adjust_stage(observed, modeled)
    flow_scores = metrics.score_run(observed["flow_cfs"], modeled["flow_cfs"])
    stage_scores = metrics.score_run(observed["stage_ft"], modeled_stage)
    return {
        **{f"flow_{key}": value for key, value in flow_scores.items()},
        **{f"stage_{key}": value for key, value in stage_scores.items()},
    }


def _aligned_frame(observed: pd.DataFrame, modeled: pd.DataFrame) -> pd.DataFrame:
    modeled_stage = _datum_adjust_stage(observed, modeled)
    aligned = pd.concat(
        [
            observed["flow_cfs"].rename("observed_flow_cfs"),
            modeled["flow_cfs"].rename("modeled_flow_cfs"),
            observed["stage_ft"].rename("observed_stage_ft"),
            modeled_stage.rename("modeled_stage_ft"),
        ],
        axis=1,
        join="inner",
    ).dropna()
    aligned["flow_residual_cfs"] = aligned["modeled_flow_cfs"] - aligned["observed_flow_cfs"]
    aligned["stage_residual_ft"] = aligned["modeled_stage_ft"] - aligned["observed_stage_ft"]
    return aligned


def _plot_overlay(aligned: pd.DataFrame, output_path: Path, title: str) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.plot(aligned.index, aligned["observed_flow_cfs"], label="Observed", color="#1f77b4", linewidth=2)
    ax.plot(aligned.index, aligned["modeled_flow_cfs"], label="Modeled", color="#d62728", linewidth=2)
    ax.set_title(title)
    ax.set_ylabel("Flow (cfs)")
    ax.set_xlabel("Datetime (UTC)")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def _plot_residual(aligned: pd.DataFrame, output_path: Path, title: str) -> None:
    fig, ax = plt.subplots(figsize=(10, 3.8))
    ax.axhline(0, color="#333333", linewidth=1)
    ax.plot(aligned.index, aligned["flow_residual_cfs"], color="#9467bd", linewidth=1.8)
    ax.set_title(title)
    ax.set_ylabel("Modeled - observed (cfs)")
    ax.set_xlabel("Datetime (UTC)")
    ax.grid(True, alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def _plot_parameter_map(hdf_path: Path, dataset: str, output_path: Path, title: str, label: str) -> None:
    with h5py.File(hdf_path, "r") as hf:
        xy = hf["Geometry/2D Flow Areas/MainArea/Cells Center Coordinate"][:]
        values = hf[dataset][:]
        if values.ndim == 2:
            values = values[:, 0]
    _plot_cell_value_map(xy, values, output_path, title, label)


def _plot_cell_value_map(
    xy: np.ndarray,
    values: np.ndarray,
    output_path: Path,
    title: str,
    label: str,
) -> None:
    fig, ax = plt.subplots(figsize=(7, 6))
    points = ax.scatter(xy[:, 0], xy[:, 1], c=values, s=2, cmap="viridis")
    ax.set_title(title)
    ax.set_xlabel("EPSG:5070 X")
    ax.set_ylabel("EPSG:5070 Y")
    ax.set_aspect("equal", adjustable="box")
    cbar = fig.colorbar(points, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label(label)
    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def _plot_curve_number_map(
    hdf_path: Path,
    output_path: Path,
    title: str,
    label: str,
    scale: float = 1.0,
) -> None:
    """Sample the CLB-660 CN raster at HEC-RAS cell centers for the map output."""
    import rasterio
    from pyproj import Transformer

    metadata_path = hdf_path.with_name("clb660_spatial_parameter_distribution.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    raster_path = Path(metadata["rasters"]["curve_number"])

    with h5py.File(hdf_path, "r") as hf:
        xy = hf["Geometry/2D Flow Areas/MainArea/Cells Center Coordinate"][:]

    with rasterio.open(raster_path) as src:
        sample_xy = xy
        if src.crs and src.crs.to_epsg() != 5070:
            transformer = Transformer.from_crs("EPSG:5070", src.crs, always_xy=True)
            xs, ys = transformer.transform(xy[:, 0], xy[:, 1])
            sample_xy = np.column_stack([xs, ys])
        values = np.array([row[0] for row in src.sample(sample_xy)], dtype=np.float32)
        invalid = ~np.isfinite(values)
        if src.nodata is not None:
            invalid |= values == src.nodata

    fallback = float(metadata["curve_number"]["mean"])
    values[invalid] = fallback
    values = np.clip(values * scale, 30.0, 100.0)
    _plot_cell_value_map(xy, values, output_path, title, label)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, pd.Timedelta):
        return value.isoformat()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer, np.bool_)):
        return value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _run_one(config: RunConfig, event: dict[str, Any], observed: pd.DataFrame) -> dict[str, Any]:
    artifact_run_dir = OUTPUT_ROOT / "runs" / config.name
    artifact_run_dir.mkdir(parents=True, exist_ok=True)
    work_run_dir = WORK_ROOT / "runs" / config.name
    work_run_dir.mkdir(parents=True, exist_ok=True)
    model_dir = _prepare_model(work_run_dir, config, event)

    print(f"\n=== Running {config.name}: {config.label} ===")
    hecras = _run_hecras(model_dir, artifact_run_dir)
    if hecras["returncode"] != 0:
        raise RuntimeError(f"HEC-RAS failed for {config.name}: return code {hecras['returncode']}")

    hdf_path = model_dir / f"{PROJECT_NAME}.p01.hdf"
    messages = _compute_messages(hdf_path)
    if "Finished Unsteady Flow Simulation" not in messages:
        raise RuntimeError(f"HEC-RAS did not finish unsteady flow simulation for {config.name}")

    modeled = results.extract_point_timeseries(hdf_path, AREA_NAME, GAUGE_LON, GAUGE_LAT)
    modeled.to_csv(artifact_run_dir / "modeled_timeseries.csv")
    score = _score(observed, modeled)
    aligned = _aligned_frame(observed, modeled)
    aligned.to_csv(artifact_run_dir / "aligned_timeseries.csv")

    _plot_overlay(aligned, artifact_run_dir / "overlay_flow.png", f"{config.label}: observed vs modeled flow")
    _plot_residual(aligned, artifact_run_dir / "residual_flow.png", f"{config.label}: flow residual")
    (artifact_run_dir / "compute_messages.txt").write_text(messages, encoding="utf-8")

    artifact_model_dir = artifact_run_dir / "model"
    if artifact_model_dir.exists():
        shutil.rmtree(artifact_model_dir)
    shutil.copytree(model_dir, artifact_model_dir)

    summary = {
        "name": config.name,
        "label": config.label,
        "mannings_scale": config.mannings_scale,
        "cn_scale": config.cn_scale,
        "model_dir": model_dir,
        "artifact_model_dir": artifact_model_dir,
        "plan_hdf": hdf_path,
        "artifact_plan_hdf": artifact_model_dir / f"{PROJECT_NAME}.p01.hdf",
        "hecras": hecras,
        "extract_attrs": dict(modeled.attrs),
        "metrics": score,
        "plots": {
            "overlay": artifact_run_dir / "overlay_flow.png",
            "residual": artifact_run_dir / "residual_flow.png",
        },
    }
    (artifact_run_dir / "run_summary.json").write_text(
        json.dumps(_json_safe(summary), indent=2),
        encoding="utf-8",
    )
    return summary


def run_calibration(overwrite: bool = False) -> dict[str, Any]:
    if overwrite and WORK_ROOT.exists():
        shutil.rmtree(WORK_ROOT)
    if overwrite and OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    event = _read_event()
    observed = _observed_frame(event)
    observed.to_csv(OUTPUT_ROOT / "observed_event.csv")
    shutil.copy2(EVENT_WORKSPACE / "calibration_event.json", OUTPUT_ROOT / "calibration_event.json")

    runs: list[RunConfig] = [RunConfig("initial", "Initial spatial n/CN")]
    summaries = [_run_one(runs[0], event, observed)]

    initial_nse = summaries[0]["metrics"]["flow_nash_sutcliffe"]
    if initial_nse < 0.5:
        initial_peak_error = summaries[0]["metrics"]["flow_peak_error_pct"]
        if initial_peak_error < 0:
            n_scale = 0.9
            cn_scale = 1.1
        else:
            n_scale = 1.1
            cn_scale = 0.9
        runs.extend(
            [
                RunConfig(
                    "mannings_adjusted",
                    f"Manning's n {'-10%' if n_scale < 1 else '+10%'}",
                    mannings_scale=n_scale,
                ),
                RunConfig(
                    "curve_number_adjusted",
                    f"Curve number {'+10%' if cn_scale > 1 else '-10%'}",
                    cn_scale=cn_scale,
                ),
            ]
        )
        for config in runs[1:]:
            summaries.append(_run_one(config, event, observed))

    metrics_rows = []
    for summary in summaries:
        metrics_rows.append(
            {
                "run": summary["name"],
                "label": summary["label"],
                "mannings_scale": summary["mannings_scale"],
                "cn_scale": summary["cn_scale"],
                **summary["metrics"],
            }
        )
    metrics_df = pd.DataFrame(metrics_rows)
    metrics_df.to_csv(OUTPUT_ROOT / "metrics_summary.csv", index=False)
    best_idx = metrics_df["flow_nash_sutcliffe"].astype(float).idxmax()
    best_name = str(metrics_df.loc[best_idx, "run"])
    best_summary = next(summary for summary in summaries if summary["name"] == best_name)

    plots_dir = OUTPUT_ROOT / "plots"
    plots_dir.mkdir(exist_ok=True)
    best_run_dir = OUTPUT_ROOT / "runs" / best_name
    shutil.copy2(best_run_dir / "overlay_flow.png", plots_dir / "best_overlay_flow.png")
    shutil.copy2(best_run_dir / "residual_flow.png", plots_dir / "best_residual_flow.png")
    _plot_parameter_map(
        Path(best_summary["artifact_plan_hdf"]),
        "Geometry/2D Flow Areas/MainArea/Cells Center Manning's n",
        plots_dir / "mannings_n_map.png",
        "Spatial Manning's n values",
        "Manning's n",
    )
    _plot_curve_number_map(
        Path(best_summary["artifact_plan_hdf"]),
        plots_dir / "curve_number_map.png",
        "Spatial SCS curve number values",
        "Curve number",
        scale=float(best_summary["cn_scale"]),
    )

    final = {
        "issue": ISSUE,
        "event": event,
        "run_count": len(summaries),
        "target_nse": 0.5,
        "best_run": best_name,
        "best_metrics": best_summary["metrics"],
        "runs": summaries,
        "metrics_summary_csv": OUTPUT_ROOT / "metrics_summary.csv",
        "output_root": OUTPUT_ROOT,
        "plots": {
            "best_overlay_flow": plots_dir / "best_overlay_flow.png",
            "best_residual_flow": plots_dir / "best_residual_flow.png",
            "mannings_n_map": plots_dir / "mannings_n_map.png",
            "curve_number_map": plots_dir / "curve_number_map.png",
        },
    }
    (OUTPUT_ROOT / "final_summary.json").write_text(
        json.dumps(_json_safe(final), indent=2),
        encoding="utf-8",
    )
    print(json.dumps(_json_safe(final), indent=2))
    return final


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run"], nargs="?", default="run")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if args.command == "run":
        run_calibration(overwrite=args.overwrite)


if __name__ == "__main__":
    main()
