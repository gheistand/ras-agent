"""
Tests for terrain.py — NLCD land cover download, reproject, and clip functions.

All tests use unittest.mock to avoid real network calls.
Synthetic GeoTIFF fixtures are built with rasterio.MemoryFile.
"""

import io
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'pipeline'))

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from shapely.geometry import box

from terrain import (
    TerrainError,
    find_champ_image_service,
    download_nlcd,
    reproject_nlcd,
    clip_nlcd_to_watershed,
    get_nlcd,
    get_terrain,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_nlcd_bytes(
    west: float, south: float, east: float, north: float,
    crs_epsg: int = 4326,
    width: int = 10,
    height: int = 10,
) -> bytes:
    """Create a minimal uint8 GeoTIFF as in-memory bytes for mock responses."""
    transform = from_bounds(west, south, east, north, width, height)
    data = np.full((1, height, width), 82, dtype=np.uint8)  # 82 = Cultivated Crops

    buf = io.BytesIO()
    with rasterio.open(
        buf, "w",
        driver="GTiff",
        dtype="uint8",
        width=width,
        height=height,
        count=1,
        crs=CRS.from_epsg(crs_epsg),
        transform=transform,
    ) as ds:
        ds.write(data)
    return buf.getvalue()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_get_nlcd_downloads_and_reprojects(tmp_path):
    """Mock WCS response → verify output file is valid raster in EPSG:5070."""
    bbox = (-89.1, 40.1, -89.0, 40.2)
    # The download adds 0.1-deg buffer: west=−89.2, south=40.0, east=−88.9, north=40.3
    nlcd_bytes = _make_nlcd_bytes(-89.2, 40.0, -88.9, 40.3)

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content = nlcd_bytes

    with patch("terrain.requests.get", return_value=mock_resp) as mock_get:
        result = get_nlcd(bbox_wgs84=bbox, output_dir=tmp_path, year=2021)

    assert mock_get.call_count == 1, "requests.get should be called once"
    assert result.exists(), "Output file must exist"

    with rasterio.open(result) as ds:
        assert ds.crs == CRS.from_epsg(5070), "Output CRS must be EPSG:5070"
        assert ds.dtypes[0] == "uint8", "dtype must be uint8"
        assert ds.count == 1


def test_download_nlcd_idempotent(tmp_path):
    """If output file already exists, download is skipped (requests.get not called)."""
    bbox = (-89.1, 40.1, -89.0, 40.2)
    raw_dir = tmp_path / "nlcd_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Pre-create the expected output file
    west, south, east, north = bbox
    expected = raw_dir / f"nlcd_2021_{west:.2f}_{south:.2f}_{east:.2f}_{north:.2f}.tif"
    nlcd_bytes = _make_nlcd_bytes(-89.2, 40.0, -88.9, 40.3)
    expected.write_bytes(nlcd_bytes)

    with patch("terrain.requests.get") as mock_get:
        result = download_nlcd(bbox_wgs84=bbox, output_dir=raw_dir, year=2021)

    mock_get.assert_not_called()
    assert result == expected


def test_clip_nlcd_to_watershed(tmp_path):
    """Create small synthetic NLCD raster + simple polygon; verify clip output."""
    # Write a 20x20 uint8 raster in EPSG:5070 (Albers meters)
    # Using a small area near Springfield, IL in projected coords
    left, bottom, right, top = 100_000.0, 1_900_000.0, 120_000.0, 1_920_000.0
    width, height = 20, 20
    transform = from_bounds(left, bottom, right, top, width, height)

    nlcd_path = tmp_path / "nlcd_synthetic.tif"
    data = np.arange(1, width * height + 1, dtype=np.uint8).reshape(1, height, width)
    with rasterio.open(
        nlcd_path, "w",
        driver="GTiff",
        dtype="uint8",
        width=width, height=height,
        count=1,
        crs=CRS.from_epsg(5070),
        transform=transform,
    ) as ds:
        ds.write(data)

    # Watershed polygon: interior 5km box, no buffer needed
    watershed_geom = box(105_000.0, 1_905_000.0, 115_000.0, 1_915_000.0)

    output_path = tmp_path / "nlcd_clipped.tif"
    result = clip_nlcd_to_watershed(
        nlcd_path=nlcd_path,
        watershed_geom=watershed_geom,
        output_path=output_path,
        buffer_m=0.0,  # no extra buffer so output fits within our synthetic raster
    )

    assert result.exists(), "Clipped output file must exist"
    with rasterio.open(result) as ds:
        assert ds.dtypes[0] == "uint8"
        assert ds.crs == CRS.from_epsg(5070)
        arr = ds.read(1)
        assert arr.size > 0, "Clipped raster must have pixels"


def test_find_champ_image_service_returns_intersecting_service(monkeypatch):
    bbox = (-89.8, 39.6, -89.4, 40.0)
    metadata = {
        "fullExtent": {
            "xmin": -90.0,
            "ymin": 39.4,
            "xmax": -89.0,
            "ymax": 40.4,
            "spatialReference": {"wkid": 4326},
        },
        "maxImageWidth": 4096,
        "maxImageHeight": 4096,
    }

    monkeypatch.setattr("terrain._get_arcgis_service_metadata", lambda url: metadata)
    result = find_champ_image_service(bbox, candidate_urls=["https://example.test/ImageServer"])

    assert result is not None
    assert result["url"] == "https://example.test/ImageServer"


def test_get_terrain_prefers_champ_image_service(tmp_path, monkeypatch):
    bbox = (-89.8, 39.6, -89.4, 40.0)
    output_dir = tmp_path / "terrain"
    champ_service = {
        "url": "https://example.test/ImageServer",
        "metadata": {"maxImageWidth": 4096, "maxImageHeight": 4096},
    }

    monkeypatch.setattr("terrain.find_champ_image_service", lambda bbox_wgs84: champ_service)

    def _fake_export(service_url, bbox_wgs84, output_path, **kwargs):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-dem")
        return output_path

    monkeypatch.setattr("terrain.export_champ_image_service_dem", _fake_export)

    def _unexpected_tiles(*args, **kwargs):
        raise AssertionError("Legacy tile discovery should not be used when CHAMP succeeds")

    monkeypatch.setattr("terrain.find_ilhmp_tiles", _unexpected_tiles)

    result = get_terrain(bbox_wgs84=bbox, output_dir=output_dir, resolution_m=3.0)

    assert result.exists()
    assert result.name == "dem_mosaic.tif"
