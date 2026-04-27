"""
Tests for pipeline/context_layers.py.
"""

import io
import json
import os
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from shapely.geometry import LineString, box

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import context_layers


def _write_geojson(gdf: gpd.GeoDataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path, driver="GeoJSON")
    return path


def _make_nlcd_raster(path: Path, *, west: float, south: float, east: float, north: float) -> Path:
    transform = from_bounds(west, south, east, north, 12, 12)
    data = np.full((1, 12, 12), 82, dtype=np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        dtype="uint8",
        width=12,
        height=12,
        count=1,
        crs=CRS.from_epsg(4326),
        transform=transform,
    ) as ds:
        ds.write(data)
    return path


def _build_workspace_fixture(tmp_path: Path) -> Path:
    workspace_dir = tmp_path / "Spring Creek"
    for rel in ("00_metadata", "02_basin_outline", "03_nhdplus", "05_landcover_nlcd", "06_soils"):
        (workspace_dir / rel).mkdir(parents=True, exist_ok=True)

    basin_wgs84 = gpd.GeoDataFrame(
        {"identifier": ["basin"]},
        geometry=[box(-89.80, 39.695, -89.78, 39.710)],
        crs="EPSG:4326",
    )
    basin_5070 = basin_wgs84.to_crs("EPSG:5070")
    basin_5070 = gpd.GeoDataFrame(
        basin_5070[["identifier"]].copy(),
        geometry=basin_5070.geometry,
        crs="EPSG:5070",
    )
    _write_geojson(
        basin_wgs84,
        workspace_dir / "02_basin_outline" / "USGS_05577500_nldi_basin.geojson",
    )
    _write_geojson(
        basin_5070,
        workspace_dir / "02_basin_outline" / "USGS_05577500_nldi_basin_5070.geojson",
    )

    flowlines = gpd.GeoDataFrame(
        {"nhdplus_comid": [1]},
        geometry=[LineString([(-89.8, 39.7), (-89.79, 39.705)])],
        crs="EPSG:4326",
    )
    _write_geojson(flowlines, workspace_dir / "03_nhdplus" / "USGS_05577500_upstream_flowlines.geojson")
    _write_geojson(flowlines.to_crs("EPSG:5070"), workspace_dir / "03_nhdplus" / "USGS_05577500_upstream_flowlines_5070.geojson")

    hucs = gpd.GeoDataFrame(
        {"huc12": ["071300080203"], "huc12_name": ["Archer Creek-Spring Creek"], "states": ["IL"]},
        geometry=[box(-89.81, 39.695, -89.77, 39.715)],
        crs="EPSG:4326",
    )
    _write_geojson(hucs, workspace_dir / "03_nhdplus" / "basin_intersecting_huc12.geojson")
    _write_geojson(hucs.to_crs("EPSG:5070"), workspace_dir / "03_nhdplus" / "basin_intersecting_huc12_5070.geojson")

    soils = gpd.GeoDataFrame(
        {
            "musym": ["A", "B"],
            "nationalmusym": ["aa", "bb"],
            "muareaacres": [10.0, 12.0],
        },
        geometry=[
            box(-89.81, 39.695, -89.78, 39.715),
            box(-89.78, 39.700, -89.76, 39.710),
        ],
        crs="EPSG:4326",
    )
    _write_geojson(soils, workspace_dir / "06_soils" / "ssurgo_mapunitpoly_bbox.geojson")

    manifest = {
        "downloads": {},
        "sources": {
            "nlcd_wcs": "https://example.test/nlcd",
            "soils_wfs": "https://example.test/soils",
        },
        "notes": {},
    }
    (workspace_dir / "00_metadata" / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return workspace_dir


def test_build_analysis_extent_writes_buffered_files(tmp_path):
    workspace_dir = _build_workspace_fixture(tmp_path)
    basin_5070 = gpd.read_file(workspace_dir / "02_basin_outline" / "USGS_05577500_nldi_basin_5070.geojson").to_crs("EPSG:5070")

    outputs = context_layers.build_analysis_extent(workspace_dir, buffer_m=250.0)

    summary = json.loads(outputs["analysis_extent_summary"].read_text(encoding="utf-8"))
    extent_5070 = gpd.read_file(outputs["analysis_extent_5070"]).to_crs("EPSG:5070")

    assert outputs["analysis_extent"].exists()
    assert outputs["analysis_extent_5070"].exists()
    assert summary["buffer_m"] == 250.0
    assert extent_5070.total_bounds[0] == pytest.approx(basin_5070.total_bounds[0] - 250.0)
    assert extent_5070.total_bounds[2] == pytest.approx(basin_5070.total_bounds[2] + 250.0)


def test_refresh_workspace_context_layers_updates_manifest(tmp_path, monkeypatch):
    workspace_dir = _build_workspace_fixture(tmp_path)

    raw_nlcd = _make_nlcd_raster(
        workspace_dir / "05_landcover_nlcd" / "fixture_raw_nlcd.tif",
        west=-89.82,
        south=39.69,
        east=-89.75,
        north=39.72,
    )
    monkeypatch.setattr(context_layers.terrain, "download_nlcd", lambda bbox_wgs84, output_dir, year=2021: raw_nlcd)

    outputs = context_layers.refresh_workspace_context_layers(workspace_dir, buffer_m=100.0, nlcd_year=2021)

    manifest = json.loads((workspace_dir / "00_metadata" / "manifest.json").read_text(encoding="utf-8"))
    soils_analysis = gpd.read_file(outputs["soils_analysis_extent_5070"]).to_crs("EPSG:5070")
    analysis_summary = json.loads(outputs["analysis_extent_summary"].read_text(encoding="utf-8"))

    assert outputs["nlcd_analysis_extent"].exists()
    assert outputs["nhdplus_flowlines_analysis_extent"].exists()
    assert outputs["soils_analysis_extent"].exists()
    assert "analysis_extent_summary" in manifest["downloads"]
    assert "analysis_extent_status" in manifest["notes"]
    assert len(soils_analysis) > 0
    assert soils_analysis.total_bounds[0] >= analysis_summary["bbox_5070"][0]
    assert soils_analysis.total_bounds[2] <= analysis_summary["bbox_5070"][2]
