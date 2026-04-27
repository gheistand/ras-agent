"""
context_layers.py - Shared buffered extent helpers for base-data workspaces.

These helpers keep informational layers aligned to a single analysis extent so
terrain-adjacent context products do not drift by source-specific buffering
rules. The current implementation is intentionally Spring Creek-shaped because
the workspace contract is still Spring Creek-specific elsewhere in the repo.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import geopandas as gpd

import terrain

logger = logging.getLogger(__name__)

DEFAULT_ANALYSIS_BUFFER_M = 500.0

ANALYSIS_EXTENT_GEOJSON = "analysis_extent.geojson"
ANALYSIS_EXTENT_5070_GEOJSON = "analysis_extent_5070.geojson"
ANALYSIS_EXTENT_SUMMARY_JSON = "analysis_extent_summary.json"


def _read_manifest(workspace_dir: Path) -> dict:
    manifest_path = Path(workspace_dir) / "00_metadata" / "manifest.json"
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _write_manifest(workspace_dir: Path, manifest: dict) -> Path:
    manifest_path = Path(workspace_dir) / "00_metadata" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def _write_geojson(gdf: gpd.GeoDataFrame, output_path: Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    gdf.to_file(output_path, driver="GeoJSON")
    return output_path


def _analysis_extent_paths(workspace_dir: Path) -> dict[str, Path]:
    meta_dir = Path(workspace_dir) / "00_metadata"
    return {
        "analysis_extent": meta_dir / ANALYSIS_EXTENT_GEOJSON,
        "analysis_extent_5070": meta_dir / ANALYSIS_EXTENT_5070_GEOJSON,
        "analysis_extent_summary": meta_dir / ANALYSIS_EXTENT_SUMMARY_JSON,
    }


def _load_analysis_extent(workspace_dir: Path) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, dict]:
    paths = _analysis_extent_paths(workspace_dir)
    analysis_extent = gpd.read_file(paths["analysis_extent"]).to_crs("EPSG:4326")
    analysis_extent_5070 = gpd.read_file(paths["analysis_extent_5070"]).to_crs("EPSG:5070")
    summary = json.loads(paths["analysis_extent_summary"].read_text(encoding="utf-8"))
    return analysis_extent, analysis_extent_5070, summary


def build_analysis_extent(
    workspace_dir: Path,
    *,
    buffer_m: float = DEFAULT_ANALYSIS_BUFFER_M,
) -> dict[str, Path]:
    """
    Create a shared buffered analysis extent from the official basin polygon.
    """
    workspace_dir = Path(workspace_dir)
    basin_path = workspace_dir / "02_basin_outline" / "USGS_05577500_nldi_basin_5070.geojson"
    basin_5070 = gpd.read_file(basin_path).to_crs("EPSG:5070")

    analysis_extent_5070 = basin_5070.copy()
    analysis_extent_5070["analysis_buffer_m"] = float(buffer_m)
    analysis_extent_5070["source_boundary"] = str(basin_path)
    analysis_extent_5070["geometry"] = basin_5070.geometry.buffer(buffer_m)

    analysis_extent = analysis_extent_5070.to_crs("EPSG:4326")
    paths = _analysis_extent_paths(workspace_dir)
    _write_geojson(analysis_extent, paths["analysis_extent"])
    _write_geojson(analysis_extent_5070, paths["analysis_extent_5070"])

    summary = {
        "buffer_m": float(buffer_m),
        "source_boundary": str(basin_path),
        "bbox_wgs84": [round(float(v), 6) for v in analysis_extent.total_bounds],
        "bbox_5070": [round(float(v), 3) for v in analysis_extent_5070.total_bounds],
        "area_sqkm": round(float(analysis_extent_5070.geometry.area.iloc[0] / 1_000_000.0), 3),
    }
    paths["analysis_extent_summary"].write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return paths


def _resolve_vector_source(candidates: list[Path], manifest: Optional[dict] = None, manifest_keys: Optional[list[str]] = None) -> Path:
    manifest = manifest or {}
    downloads = manifest.get("downloads", {})

    for key in manifest_keys or []:
        raw_value = downloads.get(key)
        if not raw_value:
            continue
        candidate = Path(raw_value)
        if candidate.exists():
            return candidate

    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(f"Could not resolve a vector source from candidates: {candidates}")


def refresh_workspace_context_layers(
    workspace_dir: Path,
    *,
    buffer_m: float = DEFAULT_ANALYSIS_BUFFER_M,
    nlcd_year: int = 2021,
) -> dict[str, Path]:
    """
    Refresh informational context layers against the shared buffered analysis extent.
    """
    workspace_dir = Path(workspace_dir)
    manifest = _read_manifest(workspace_dir)

    outputs = build_analysis_extent(workspace_dir, buffer_m=buffer_m)
    analysis_extent, analysis_extent_5070, summary = _load_analysis_extent(workspace_dir)

    landcover_dir = workspace_dir / "05_landcover_nlcd"
    raw_nlcd = terrain.download_nlcd(tuple(summary["bbox_wgs84"]), landcover_dir / "nlcd_raw", year=nlcd_year)
    nlcd_bbox_5070 = landcover_dir / f"nlcd_{nlcd_year}_analysis_extent_bbox_5070.tif"
    nlcd_analysis_extent = landcover_dir / f"nlcd_{nlcd_year}_analysis_extent.tif"
    reprojected_nlcd = terrain.reproject_nlcd(raw_nlcd, terrain.TARGET_CRS, nlcd_bbox_5070)
    terrain.clip_nlcd_to_watershed(
        reprojected_nlcd,
        analysis_extent_5070,
        nlcd_analysis_extent,
        buffer_m=0.0,
    )
    outputs["nlcd_analysis_extent_bbox_5070"] = nlcd_bbox_5070
    outputs["nlcd_analysis_extent"] = nlcd_analysis_extent

    soils_dir = workspace_dir / "06_soils"
    soils_source = _resolve_vector_source(
        [
            soils_dir / "ssurgo_mapunitpoly_bbox.gml",
            soils_dir / "ssurgo_mapunitpoly_bbox.geojson",
            soils_dir / "ssurgo_mapunitpoly_bbox.gpkg",
            soils_dir / "ssurgo_mapunitpoly_bbox.shp",
        ],
        manifest=manifest,
        manifest_keys=["soils_raw_gml"],
    )
    soils_raw = gpd.read_file(soils_source).to_crs("EPSG:4326")
    soils_analysis_extent = gpd.clip(soils_raw, analysis_extent)
    soils_analysis_extent_5070 = soils_analysis_extent.to_crs("EPSG:5070")
    outputs["soils_analysis_extent"] = _write_geojson(
        soils_analysis_extent,
        soils_dir / "ssurgo_mapunitpoly_analysis_extent.geojson",
    )
    outputs["soils_analysis_extent_5070"] = _write_geojson(
        soils_analysis_extent_5070,
        soils_dir / "ssurgo_mapunitpoly_analysis_extent_5070.geojson",
    )

    nhd_dir = workspace_dir / "03_nhdplus"
    flowlines = gpd.read_file(nhd_dir / "USGS_05577500_upstream_flowlines.geojson").to_crs("EPSG:4326")
    flowlines_analysis_extent = gpd.clip(flowlines, analysis_extent)
    outputs["nhdplus_flowlines_analysis_extent"] = _write_geojson(
        flowlines_analysis_extent,
        nhd_dir / "USGS_05577500_upstream_flowlines_analysis_extent.geojson",
    )
    outputs["nhdplus_flowlines_analysis_extent_5070"] = _write_geojson(
        flowlines_analysis_extent.to_crs("EPSG:5070"),
        nhd_dir / "USGS_05577500_upstream_flowlines_analysis_extent_5070.geojson",
    )

    hucs = gpd.read_file(nhd_dir / "basin_intersecting_huc12.geojson").to_crs("EPSG:4326")
    hucs_analysis_extent = gpd.clip(hucs, analysis_extent)
    outputs["nhdplus_huc12_analysis_extent"] = _write_geojson(
        hucs_analysis_extent,
        nhd_dir / "basin_intersecting_huc12_analysis_extent.geojson",
    )
    outputs["nhdplus_huc12_analysis_extent_5070"] = _write_geojson(
        hucs_analysis_extent.to_crs("EPSG:5070"),
        nhd_dir / "basin_intersecting_huc12_analysis_extent_5070.geojson",
    )

    downloads = manifest.setdefault("downloads", {})
    downloads.update({
        "analysis_extent_geojson": str(outputs["analysis_extent"]),
        "analysis_extent_geojson_5070": str(outputs["analysis_extent_5070"]),
        "analysis_extent_summary": str(outputs["analysis_extent_summary"]),
        "nlcd_analysis_extent_bbox_5070": str(outputs["nlcd_analysis_extent_bbox_5070"]),
        "nlcd_analysis_extent": str(outputs["nlcd_analysis_extent"]),
        "soils_analysis_extent": str(outputs["soils_analysis_extent"]),
        "soils_analysis_extent_5070": str(outputs["soils_analysis_extent_5070"]),
        "nhdplus_flowlines_analysis_extent": str(outputs["nhdplus_flowlines_analysis_extent"]),
        "nhdplus_flowlines_analysis_extent_5070": str(outputs["nhdplus_flowlines_analysis_extent_5070"]),
        "nhdplus_huc12_analysis_extent": str(outputs["nhdplus_huc12_analysis_extent"]),
        "nhdplus_huc12_analysis_extent_5070": str(outputs["nhdplus_huc12_analysis_extent_5070"]),
    })

    notes = manifest.setdefault("notes", {})
    notes["analysis_extent_status"] = (
        f"Informational layers refreshed against a shared {buffer_m:.0f} m buffered basin extent "
        "so terrain, NLCD, soils, and NHDPlus context use the same analysis envelope."
    )
    _write_manifest(workspace_dir, manifest)
    outputs["manifest"] = workspace_dir / "00_metadata" / "manifest.json"
    logger.info("Refreshed workspace context layers for shared buffered analysis extent: %s", workspace_dir)
    return outputs
