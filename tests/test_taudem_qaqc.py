"""
Tests for TauDEM delineation QAQC bundle generation.

These tests use small synthetic projected geometry and do not require TauDEM.
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, box

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import taudem_qaqc


def _small_watershed(tmp_path: Path):
    basin_geom = box(0.0, 0.0, 1000.0, 1000.0)
    basin = gpd.GeoDataFrame({"name": ["small"]}, geometry=[basin_geom], crs="EPSG:5070")
    streams = gpd.GeoDataFrame(
        {"stream_id": [1, 2], "Order": [1, 2]},
        geometry=[
            LineString([(500.0, 1000.0), (500.0, 500.0)]),
            LineString([(500.0, 500.0), (500.0, 0.0)]),
        ],
        crs="EPSG:5070",
    )
    subbasins = gpd.GeoDataFrame(
        {"wsno": [1, 2]},
        geometry=[
            box(0.0, 500.0, 1000.0, 1000.0),
            box(0.0, 0.0, 1000.0, 500.0),
        ],
        crs="EPSG:5070",
    )
    snapped = Point(500.0, 0.0)
    outlet = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(500.0, -50.0)], crs="EPSG:5070")
    outlet_path = tmp_path / "outlet.shp"
    outlet.to_file(outlet_path, driver="ESRI Shapefile")

    fel = tmp_path / "fel.tif"
    ad8 = tmp_path / "ad8.tif"
    fel.write_bytes(b"filled-dem")
    ad8.write_bytes(b"area-grid")

    chars = SimpleNamespace(
        drainage_area_km2=1.0,
        drainage_area_mi2=0.386102,
        mean_elevation_m=100.0,
        relief_m=4.0,
        main_channel_length_km=1.0,
        main_channel_slope_m_per_m=0.0004,
        centroid_lat=40.0,
        centroid_lon=-89.0,
        pour_point_lat=40.0,
        pour_point_lon=-89.0,
        extra={
            "threshold_cells": 20,
            "cell_area_km2": 0.0001,
            "source_bounds": (-10.0, -10.0, 1010.0, 1010.0),
        },
    )
    return SimpleNamespace(
        basin=basin,
        streams=streams,
        subbasins=subbasins,
        centerlines=streams.copy(),
        breaklines=streams.copy(),
        pour_point=snapped,
        characteristics=chars,
        dem_clipped=tmp_path / "dem_clipped.tif",
        artifacts={
            "fel": fel,
            "ad8": ad8,
            "outlet": outlet_path,
            "snapped_outlet": tmp_path / "outlet_snapped.shp",
        },
    )


def test_generate_taudem_qaqc_bundle_writes_review_artifacts(tmp_path):
    watershed = _small_watershed(tmp_path)
    command = SimpleNamespace(
        executable="Threshold",
        command=["Threshold", "-ssa", "ad8.tif", "-src", "src.tif", "-thresh", "20"],
        outputs={"src": tmp_path / "src.tif"},
        returncode=0,
        stdout="ok",
        stderr="",
    )

    outputs = taudem_qaqc.generate_taudem_qaqc_bundle(
        watershed,
        tmp_path / "qaqc_bundle",
        source_dem=tmp_path / "dem.tif",
        snap_threshold_m=100.0,
        min_stream_area_km2=0.002,
        taudem_commands=[command],
    )

    assert outputs["manifest"].exists()
    assert outputs["diagnostics"].exists()
    assert outputs["report_html"].exists()
    assert outputs["review_prompts"].exists()
    assert outputs["signoff"].exists()
    assert outputs["table_artifacts"].exists()
    assert outputs["map_outlet_snapping"].exists()

    manifest = json.loads(outputs["manifest"].read_text(encoding="utf-8"))
    diagnostics = json.loads(outputs["diagnostics"].read_text(encoding="utf-8"))
    signoff = json.loads(outputs["signoff"].read_text(encoding="utf-8"))
    report_html = outputs["report_html"].read_text(encoding="utf-8")

    assert manifest["schema_version"] == taudem_qaqc.QAQC_SCHEMA_VERSION
    assert manifest["detail_level"] == "first_pass"
    assert manifest["production_promotion"]["allowed"] is False
    assert diagnostics["summary"]["human_signoff_required"] is True
    assert {check["id"] for check in diagnostics["checks"]} == set(taudem_qaqc.REVIEW_TOPICS)
    assert any(check["status"] == "needs_attention" for check in diagnostics["checks"])
    assert signoff["status"] == "pending"
    assert signoff["approved_for_production"] is False
    assert "Production promotion is blocked" in report_html
    assert "Outlet Snapping" in report_html


def test_taudem_qaqc_signoff_gate_requires_human_approval(tmp_path):
    watershed = _small_watershed(tmp_path)
    outputs = taudem_qaqc.generate_taudem_qaqc_bundle(watershed, tmp_path / "qaqc_bundle")

    with pytest.raises(RuntimeError, match="not been approved"):
        taudem_qaqc.require_taudem_qaqc_signoff(outputs["bundle_dir"])

    taudem_qaqc.record_taudem_qaqc_signoff(
        outputs["bundle_dir"],
        reviewer="Bill Reviewer",
        reviewer_role="engineer",
        decision="approved",
        notes="Synthetic small-basin fixture approved for test.",
        approved_for_production=True,
    )

    signoff = taudem_qaqc.require_taudem_qaqc_signoff(outputs["bundle_dir"])
    assert signoff["approved_for_production"] is True
    assert signoff["production_promotion"]["allowed"] is True
