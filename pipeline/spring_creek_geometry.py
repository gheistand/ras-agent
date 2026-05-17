"""
spring_creek_geometry.py - Spring Creek low-detail 2D HEC-RAS geometry build.

This module is intentionally Spring Creek-specific. It composes repo-local
workspace data with the geometry-first model builder while keeping reusable
HEC-RAS geometry primitives in ras-commander.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import Point

try:
    import model_builder
except ImportError:  # pragma: no cover - package-style import fallback
    from pipeline import model_builder

logger = logging.getLogger(__name__)

SITE_ID = "05577500"
FLOW_AREA_NAME = "MainArea"
DEFAULT_OUTPUT_SUBDIR = Path("08_model_validation") / "clb_399_2d_mesh"
DEFAULT_CELL_SIZE_M = 125.0
DEFAULT_MAJOR_CHANNEL_MIN_LENGTH_M = 2_000.0
DEFAULT_CHANNEL_NEAR_CELL_SIZE_M = 75.0
DEFAULT_GAUGE_REFINEMENT_RADIUS_M = 600.0
DEFAULT_GAUGE_CELL_SIZE_M = 50.0


def build_spring_creek_2d_geometry(
    workspace_dir: Path,
    *,
    output_dir: Optional[Path] = None,
    cell_size_m: float = DEFAULT_CELL_SIZE_M,
    major_channel_min_length_m: float = DEFAULT_MAJOR_CHANNEL_MIN_LENGTH_M,
    gauge_refinement_radius_m: float = DEFAULT_GAUGE_REFINEMENT_RADIUS_M,
    gauge_cell_size_m: float = DEFAULT_GAUGE_CELL_SIZE_M,
    channel_near_cell_size_m: float = DEFAULT_CHANNEL_NEAR_CELL_SIZE_M,
    try_generate_mesh: bool = False,
    mesh_max_wait: int = 600,
) -> dict:
    """Build the CLB-399 low-detail 2D geometry package for Spring Creek."""
    workspace_dir = Path(workspace_dir)
    output_dir = Path(output_dir) if output_dir else workspace_dir / DEFAULT_OUTPUT_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)

    sources = _resolve_sources(workspace_dir)
    basin_gdf = gpd.read_file(sources["basin_5070"]).to_crs("EPSG:5070")
    if len(basin_gdf) != 1:
        basin_gdf = gpd.GeoDataFrame(geometry=[basin_gdf.geometry.union_all()], crs="EPSG:5070")
    basin_poly = basin_gdf.geometry.iloc[0]

    gauge_gdf = gpd.read_file(sources["gauge_5070"]).to_crs("EPSG:5070")
    gauge_point = gauge_gdf.geometry.iloc[0]

    flowlines_raw = gpd.read_file(sources["flowlines_5070"]).to_crs("EPSG:5070")
    flowlines = _clip_linework_to_basin(flowlines_raw, basin_gdf)
    major_channels = _major_channel_breaklines(
        flowlines,
        min_length_m=major_channel_min_length_m,
        near_cell_size_m=channel_near_cell_size_m,
        far_cell_size_m=cell_size_m,
    )
    gauge_breaklines, gauge_refinement = _gauge_refinement_breaklines(
        flowlines,
        gauge_point,
        basin_gdf,
        radius_m=gauge_refinement_radius_m,
        near_cell_size_m=gauge_cell_size_m,
        far_cell_size_m=min(cell_size_m, gauge_cell_size_m * 2.0),
    )
    breaklines = _combine_breaklines(major_channels, gauge_breaklines)

    boundary_path = output_dir / "flow_area_boundary_5070.geojson"
    major_path = output_dir / "major_channel_breaklines_5070.geojson"
    gauge_path = output_dir / "gauge_refinement_5070.geojson"
    mesh_breaklines_path = output_dir / "mesh_breaklines_5070.geojson"
    _write_geojson(basin_gdf, boundary_path)
    _write_geojson(major_channels, major_path)
    _write_geojson(gauge_refinement, gauge_path)
    _write_geojson(breaklines, mesh_breaklines_path)

    watershed = _make_watershed(
        basin_gdf=basin_gdf,
        streams_gdf=flowlines,
        breaklines_gdf=breaklines,
        gauge_point=gauge_point,
        dem_path=sources["dem"],
        ad8_path=sources.get("ad8"),
    )
    hydro_set = _make_placeholder_hydro_set(
        watershed.characteristics.drainage_area_mi2
    )

    project = model_builder.build_model(
        watershed=watershed,
        hydro_set=hydro_set,
        output_dir=output_dir,
        return_periods=[100],
        mesh_strategy="geometry_first",
        nlcd_raster_path=sources.get("nlcd"),
        water_source_mode="mock_screening",
        cell_size_m=cell_size_m,
        breakline_simplify_ft=25.0,
        breakline_near_repeats=1,
        include_boundary_conditions=False,
    )

    mesh_generation = {"attempted": False, "status": "not_requested"}
    if try_generate_mesh:
        mesh_generation = _try_generate_mesh(
            project,
            cell_size_m=cell_size_m,
            max_wait=mesh_max_wait,
        )

    summary = _build_summary(
        workspace_dir=workspace_dir,
        output_dir=output_dir,
        project=project,
        sources=sources,
        basin_gdf=basin_gdf,
        flowlines=flowlines,
        major_channels=major_channels,
        breaklines=breaklines,
        gauge_refinement=gauge_refinement,
        cell_size_m=cell_size_m,
        major_channel_min_length_m=major_channel_min_length_m,
        gauge_refinement_radius_m=gauge_refinement_radius_m,
        gauge_cell_size_m=gauge_cell_size_m,
        mesh_generation=mesh_generation,
    )

    summary_path = output_dir / "mesh_quality_report.json"
    summary_path.write_text(json.dumps(_to_jsonable(summary), indent=2), encoding="utf-8")
    md_path = output_dir / "mesh_quality_report.md"
    md_path.write_text(_format_summary_markdown(summary), encoding="utf-8")
    _update_manifest(workspace_dir, summary_path, md_path, project.geometry_file)

    summary["artifacts"]["mesh_quality_report_json"] = summary_path
    summary["artifacts"]["mesh_quality_report_md"] = md_path
    return summary


def _resolve_sources(workspace_dir: Path) -> dict[str, Path]:
    paths = {
        "basin_5070": workspace_dir / "02_basin_outline" / f"USGS_{SITE_ID}_nldi_basin_5070.geojson",
        "gauge_5070": workspace_dir / "01_gauge" / f"USGS_{SITE_ID}_point_5070.geojson",
        "flowlines_5070": workspace_dir / "03_nhdplus" / f"USGS_{SITE_ID}_upstream_flowlines_analysis_extent_5070.geojson",
        "dem": workspace_dir / "04_terrain" / "final" / "spring_creek_2d_3dep10m_analysis_extent_5070.tif",
        "nlcd": workspace_dir / "05_landcover_nlcd" / "nlcd_2021_analysis_extent.tif",
        "ad8": workspace_dir / "04_terrain" / "taudem_basin2" / "ad8.tif",
    }
    fallback_flowlines = workspace_dir / "03_nhdplus" / f"USGS_{SITE_ID}_upstream_flowlines_5070.geojson"
    if not paths["flowlines_5070"].exists() and fallback_flowlines.exists():
        paths["flowlines_5070"] = fallback_flowlines
    fallback_dem = workspace_dir / "04_terrain" / "spring_creek_basin_dem_5070.tif"
    if not paths["dem"].exists() and fallback_dem.exists():
        paths["dem"] = fallback_dem

    required = ["basin_5070", "gauge_5070", "flowlines_5070", "dem"]
    missing = [name for name in required if not paths[name].exists()]
    if missing:
        details = ", ".join(f"{name}={paths[name]}" for name in missing)
        raise FileNotFoundError(f"Missing required Spring Creek source artifacts: {details}")

    return {name: path for name, path in paths.items() if path.exists()}


def _clip_linework_to_basin(flowlines: gpd.GeoDataFrame, basin_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    clipped = gpd.clip(flowlines, basin_gdf)
    clipped = clipped[~clipped.geometry.is_empty & clipped.geometry.notna()].copy()
    clipped["length_m"] = clipped.geometry.length
    return clipped.reset_index(drop=True)


def _major_channel_breaklines(
    flowlines: gpd.GeoDataFrame,
    *,
    min_length_m: float,
    near_cell_size_m: float,
    far_cell_size_m: float,
) -> gpd.GeoDataFrame:
    major = flowlines[flowlines["length_m"] >= min_length_m].copy()
    if major.empty and not flowlines.empty:
        major = flowlines.nlargest(min(10, len(flowlines)), "length_m").copy()
    major["breakline_type"] = "major_channel"
    major["name"] = [
        f"NHD{str(comid)[-8:]}" if "nhdplus_comid" in major.columns else f"NHD{idx + 1}"
        for idx, comid in enumerate(major.get("nhdplus_comid", range(len(major))))
    ]
    major["cell_size_near"] = float(near_cell_size_m)
    major["cell_size_far"] = float(far_cell_size_m)
    return major.reset_index(drop=True)


def _gauge_refinement_breaklines(
    flowlines: gpd.GeoDataFrame,
    gauge_point: Point,
    basin_gdf: gpd.GeoDataFrame,
    *,
    radius_m: float,
    near_cell_size_m: float,
    far_cell_size_m: float,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    buffer_geom = gauge_point.buffer(radius_m)
    basin_buffer = gpd.GeoDataFrame(geometry=[buffer_geom], crs="EPSG:5070")
    clipped_buffer = gpd.overlay(basin_buffer, basin_gdf, how="intersection")
    if clipped_buffer.empty:
        clipped_buffer = basin_buffer
    clipped_buffer["site_id"] = SITE_ID
    clipped_buffer["radius_m"] = float(radius_m)
    clipped_buffer["target_cell_size_m"] = float(near_cell_size_m)

    nearby = flowlines[flowlines.geometry.distance(gauge_point) <= radius_m].copy()
    if nearby.empty and not flowlines.empty:
        nearest_idx = flowlines.geometry.distance(gauge_point).idxmin()
        nearby = flowlines.loc[[nearest_idx]].copy()
    if not nearby.empty:
        nearby = gpd.clip(nearby, clipped_buffer[["geometry"]])
    nearby = nearby[~nearby.geometry.is_empty & nearby.geometry.notna()].copy()
    nearby["breakline_type"] = "gauge_refinement"
    nearby["name"] = [f"GaugeRefine{idx + 1}" for idx in range(len(nearby))]
    nearby["cell_size_near"] = float(near_cell_size_m)
    nearby["cell_size_far"] = float(far_cell_size_m)
    nearby["length_m"] = nearby.geometry.length
    return nearby.reset_index(drop=True), clipped_buffer.reset_index(drop=True)


def _combine_breaklines(*frames: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    frames = [frame for frame in frames if frame is not None and not frame.empty]
    if not frames:
        return gpd.GeoDataFrame(
            columns=["breakline_type", "name", "cell_size_near", "cell_size_far", "length_m", "geometry"],
            geometry="geometry",
            crs="EPSG:5070",
        )
    columns = ["breakline_type", "name", "cell_size_near", "cell_size_far", "length_m", "nhdplus_comid", "geometry"]
    merged = gpd.GeoDataFrame(
        [row for frame in frames for _, row in frame.reindex(columns=columns).iterrows()],
        geometry="geometry",
        crs="EPSG:5070",
    )
    return merged.reset_index(drop=True)


def _make_watershed(
    *,
    basin_gdf: gpd.GeoDataFrame,
    streams_gdf: gpd.GeoDataFrame,
    breaklines_gdf: gpd.GeoDataFrame,
    gauge_point: Point,
    dem_path: Path,
    ad8_path: Optional[Path],
):
    basin_poly = basin_gdf.geometry.iloc[0]
    area_km2 = basin_poly.area / 1_000_000.0
    transformer = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)
    centroid_lon, centroid_lat = transformer.transform(basin_poly.centroid.x, basin_poly.centroid.y)
    pour_lon, pour_lat = transformer.transform(gauge_point.x, gauge_point.y)
    main_channel_length_km = float(streams_gdf.geometry.length.max() / 1000.0) if len(streams_gdf) else 0.0

    characteristics = SimpleNamespace(
        drainage_area_km2=area_km2,
        drainage_area_mi2=area_km2 * 0.3861021585424458,
        mean_elevation_m=0.0,
        relief_m=0.0,
        main_channel_length_km=main_channel_length_km,
        main_channel_slope_m_per_m=0.002,
        centroid_lat=centroid_lat,
        centroid_lon=centroid_lon,
        pour_point_lat=pour_lat,
        pour_point_lon=pour_lon,
    )
    artifacts = {}
    if ad8_path is not None:
        artifacts["ad8"] = str(ad8_path)
    return SimpleNamespace(
        basin=basin_gdf,
        streams=streams_gdf,
        subbasins=gpd.GeoDataFrame(geometry=[], crs="EPSG:5070"),
        centerlines=streams_gdf,
        breaklines=breaklines_gdf,
        pour_point=gauge_point,
        characteristics=characteristics,
        dem_clipped=dem_path,
        artifacts=artifacts,
    )


def _make_placeholder_hydro_set(area_mi2: float):
    times = [i * 0.25 for i in range(97)]
    flows = [2_000.0 * math.sin(math.pi * i / 96) for i in range(97)]
    flows[0] = 0.0
    flows[-1] = 0.0
    hydro = SimpleNamespace(
        return_period_yr=100,
        peak_flow_cfs=max(flows),
        time_to_peak_hr=times[flows.index(max(flows))],
        duration_hr=times[-1],
        time_step_hr=0.25,
        times_hr=times,
        flows_cfs=flows,
        baseflow_cfs=0.0,
        source="clb_399_geometry_placeholder",
        metadata={"purpose": "geometry-only low-detail screening scaffold"},
    )
    hydrographs = {100: hydro}
    return SimpleNamespace(
        watershed_area_mi2=area_mi2,
        time_of_concentration_hr=6.0,
        hydrographs=hydrographs,
        get=lambda rp: hydrographs.get(rp),
    )


def _try_generate_mesh(project, *, cell_size_m: float, max_wait: int) -> dict:
    result = {"attempted": True, "status": "started"}
    try:
        try:
            import hecras_readiness
        except ImportError:  # pragma: no cover
            from pipeline import hecras_readiness

        readiness = hecras_readiness.check_hecras_readiness(
            project.project_dir,
            project.plan_hdf,
            project.geom_ext,
            regenerate=True,
            max_wait=max_wait,
            terrain_units="Meters",
        )
        result["readiness"] = readiness.to_dict()

        from ras_commander.geom import GeomMesh

        mesh = GeomMesh.generate(
            project.geometry_file,
            mesh_name=FLOW_AREA_NAME,
            cell_size=cell_size_m,
            near_repeats=1,
            max_iterations=8,
        )
        result.update({
            "status": mesh.status,
            "ok": mesh.ok,
            "cell_count": mesh.cell_count,
            "face_count": mesh.face_count,
            "iterations": mesh.iterations,
            "fixes_applied": list(mesh.fixes_applied),
            "error_message": mesh.error_message,
            "geom_hdf_path": mesh.geom_hdf_path,
        })
    except Exception as exc:
        result.update({"status": "exception", "ok": False, "error_message": str(exc)})
    return result


def _build_summary(
    *,
    workspace_dir: Path,
    output_dir: Path,
    project,
    sources: dict[str, Path],
    basin_gdf: gpd.GeoDataFrame,
    flowlines: gpd.GeoDataFrame,
    major_channels: gpd.GeoDataFrame,
    breaklines: gpd.GeoDataFrame,
    gauge_refinement: gpd.GeoDataFrame,
    cell_size_m: float,
    major_channel_min_length_m: float,
    gauge_refinement_radius_m: float,
    gauge_cell_size_m: float,
    mesh_generation: dict,
) -> dict:
    basin_area_sq_m = float(basin_gdf.geometry.area.sum())
    estimated_cell_count = int(round(basin_area_sq_m / (cell_size_m ** 2)))
    geom_metrics = _inspect_geom_text(project.geometry_file)
    hdf_metrics = _inspect_geom_hdf(project.geometry_file.with_suffix(project.geometry_file.suffix + ".hdf"))

    return {
        "schema_version": "ras-agent-spring-creek-2d-geometry/v1",
        "issue": "CLB-399",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "workspace_dir": workspace_dir,
        "output_dir": output_dir,
        "flow_area": {
            "name": FLOW_AREA_NAME,
            "source": sources["basin_5070"],
            "area_sq_m": basin_area_sq_m,
            "area_sq_mi": basin_area_sq_m / 2_589_988.110336,
            "bounds_5070": [float(v) for v in basin_gdf.total_bounds],
        },
        "mesh_controls": {
            "base_cell_size_m": float(cell_size_m),
            "base_cell_size_target_range_m": [100.0, 200.0],
            "estimated_base_cell_count": estimated_cell_count,
            "major_channel_min_length_m": float(major_channel_min_length_m),
            "channel_near_cell_size_m": DEFAULT_CHANNEL_NEAR_CELL_SIZE_M,
            "gauge_refinement_radius_m": float(gauge_refinement_radius_m),
            "gauge_refinement_cell_size_m": float(gauge_cell_size_m),
        },
        "linework": {
            "source_flowline_count": int(len(flowlines)),
            "major_channel_breakline_count": int(len(major_channels)),
            "gauge_refinement_breakline_count": int((breaklines["breakline_type"] == "gauge_refinement").sum()) if len(breaklines) else 0,
            "total_breakline_count": int(len(breaklines)),
            "total_breakline_length_m": float(breaklines.geometry.length.sum()) if len(breaklines) else 0.0,
            "gauge_refinement_area_sq_m": float(gauge_refinement.geometry.area.sum()) if len(gauge_refinement) else 0.0,
        },
        "geometry_text": geom_metrics,
        "geometry_hdf": hdf_metrics,
        "mesh_generation": mesh_generation,
        "project": {
            "project_dir": project.project_dir,
            "project_name": project.project_name,
            "geometry_file": project.geometry_file,
            "plan_file": project.plan_file,
            "flow_file": project.flow_file,
            "rasmap_file": project.project_dir / f"{project.project_name}.rasmap",
            "metadata": project.metadata,
        },
        "passes": {
            "base_cell_size_in_target_range": 100.0 <= float(cell_size_m) <= 200.0,
            "has_flow_area_boundary": geom_metrics["flow_area_perimeter_vertices"] > 0,
            "has_nhdplus_breaklines": int(len(major_channels)) > 0,
            "has_gauge_refinement": int((breaklines["breakline_type"] == "gauge_refinement").sum()) > 0 if len(breaklines) else False,
            "has_mesh_cell_count": bool(
                hdf_metrics.get("cell_count")
                or geom_metrics.get("storage_area_2d_points", 0) > 0
            ),
            "max_aspect_ratio_ok": (
                hdf_metrics.get("aspect_ratio_max") is not None
                and hdf_metrics.get("aspect_ratio_max") <= 10.0
            ),
        },
        "artifacts": {
            "flow_area_boundary_5070": output_dir / "flow_area_boundary_5070.geojson",
            "major_channel_breaklines_5070": output_dir / "major_channel_breaklines_5070.geojson",
            "gauge_refinement_5070": output_dir / "gauge_refinement_5070.geojson",
            "mesh_breaklines_5070": output_dir / "mesh_breaklines_5070.geojson",
        },
    }


def _inspect_geom_text(geom_file: Path) -> dict:
    text = geom_file.read_text(encoding="utf-8", errors="replace")
    perimeter_match = re.search(r"Storage Area Surface Line=\s*(\d+)", text)
    points_match = re.search(r"Storage Area 2D Points=\s*(\d+)", text)
    point_gen_match = re.search(r"Storage Area Point Generation Data=([^\n]*)", text)
    return {
        "path": geom_file,
        "flow_area_perimeter_vertices": int(perimeter_match.group(1)) if perimeter_match else 0,
        "storage_area_2d_points": int(points_match.group(1)) if points_match else 0,
        "point_generation_data": point_gen_match.group(1).strip() if point_gen_match else "",
        "breakline_count": text.count("BreakLine Name="),
        "bc_line_count": text.count("BC Line Name="),
    }


def _inspect_geom_hdf(hdf_path: Path) -> dict:
    if not hdf_path.exists():
        return {"path": hdf_path, "exists": False, "status": "missing"}
    try:
        import h5py
        import numpy as np
    except ImportError as exc:
        return {"path": hdf_path, "exists": True, "status": "unreadable", "error": str(exc)}

    metrics = {"path": hdf_path, "exists": True, "status": "present", "size_bytes": hdf_path.stat().st_size}
    try:
        with h5py.File(hdf_path, "r") as hf:
            base = f"Geometry/2D Flow Areas/{FLOW_AREA_NAME}"
            if f"{base}/Cells Center Coordinate" in hf:
                centers = hf[f"{base}/Cells Center Coordinate"]
                metrics["cell_count"] = int(centers.shape[0])
            elif "Geometry/2D Flow Areas/Cell Points" in hf:
                metrics["cell_count"] = int(hf["Geometry/2D Flow Areas/Cell Points"].shape[0])

            if f"{base}/Faces FacePoint Indexes" in hf:
                metrics["face_count"] = int(hf[f"{base}/Faces FacePoint Indexes"].shape[0])

            face_idx_key = f"{base}/Cells FacePoint Indexes"
            face_pts_key = f"{base}/FacePoints Coordinate"
            if face_idx_key in hf and face_pts_key in hf:
                face_indexes = hf[face_idx_key][:]
                face_points = hf[face_pts_key][:]
                ratios = []
                face_counts = []
                for row in face_indexes:
                    idx = row[row >= 0]
                    idx = idx[idx < len(face_points)]
                    face_counts.append(int(len(idx)))
                    if len(idx) < 3:
                        continue
                    pts = face_points[idx]
                    width = float(np.nanmax(pts[:, 0]) - np.nanmin(pts[:, 0]))
                    height = float(np.nanmax(pts[:, 1]) - np.nanmin(pts[:, 1]))
                    if width > 0 and height > 0:
                        ratios.append(max(width / height, height / width))
                if ratios:
                    arr = np.asarray(ratios, dtype=float)
                    metrics.update({
                        "aspect_ratio_min": float(np.nanmin(arr)),
                        "aspect_ratio_p50": float(np.nanpercentile(arr, 50)),
                        "aspect_ratio_p95": float(np.nanpercentile(arr, 95)),
                        "aspect_ratio_max": float(np.nanmax(arr)),
                    })
                if face_counts:
                    counts = np.asarray(face_counts, dtype=int)
                    metrics.update({
                        "faces_per_cell_min": int(counts.min()),
                        "faces_per_cell_p50": float(np.percentile(counts, 50)),
                        "faces_per_cell_max": int(counts.max()),
                    })
    except Exception as exc:
        metrics.update({"status": "unreadable", "error": str(exc)})
    return metrics


def _format_summary_markdown(summary: dict) -> str:
    hdf = summary["geometry_hdf"]
    aspect = hdf.get("aspect_ratio_max")
    aspect_text = f"{aspect:.2f}" if aspect is not None else "not available until compiled mesh HDF exists"
    return (
        "# CLB-399 Spring Creek 2D Geometry\n\n"
        f"- Flow area: {summary['flow_area']['name']} from `{summary['flow_area']['source']}`\n"
        f"- Area: {summary['flow_area']['area_sq_mi']:.2f} sq mi\n"
        f"- Base cell size: {summary['mesh_controls']['base_cell_size_m']:.1f} m\n"
        f"- Estimated base cell count: {summary['mesh_controls']['estimated_base_cell_count']:,}\n"
        f"- NHDPlus source flowlines clipped to basin: {summary['linework']['source_flowline_count']}\n"
        f"- Major channel breaklines: {summary['linework']['major_channel_breakline_count']}\n"
        f"- Gauge refinement breaklines: {summary['linework']['gauge_refinement_breakline_count']}\n"
        f"- Geometry text 2D seed points: {summary['geometry_text']['storage_area_2d_points']:,}\n"
        f"- Geometry HDF status: {hdf.get('status')}\n"
        f"- HDF cell count: {hdf.get('cell_count', 'not available')}\n"
        f"- HDF max aspect ratio: {aspect_text}\n"
        f"- Mesh generation status: {summary['mesh_generation'].get('status')}\n"
    )


def _update_manifest(workspace_dir: Path, summary_path: Path, md_path: Path, geom_file: Path) -> None:
    manifest_path = workspace_dir / "00_metadata" / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    downloads = manifest.setdefault("downloads", {})
    notes = manifest.setdefault("notes", {})
    downloads.update({
        "clb_399_2d_geometry_file": str(geom_file),
        "clb_399_2d_mesh_quality_report": str(summary_path),
        "clb_399_2d_mesh_quality_markdown": str(md_path),
    })
    notes["clb_399_2d_geometry_status"] = (
        "Low-detail Spring Creek 2D flow area geometry, NHDPlus breaklines, "
        "and gauge refinement controls were generated for CLB-399."
    )
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _write_geojson(gdf: gpd.GeoDataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if gdf.empty:
        gpd.GeoDataFrame(geometry=[], crs="EPSG:5070").to_file(path, driver="GeoJSON")
    else:
        gdf.to_file(path, driver="GeoJSON")


def _to_jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value
