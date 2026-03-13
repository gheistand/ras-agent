"""
terrain.py — ILHMP GeoTIFF terrain data pipeline

Downloads and mosaics LiDAR-derived GeoTIFFs from the Illinois Height
Modernization Program (ILHMP) clearinghouse. Reprojects to EPSG:5070
(Albers Equal Area, CONUS) — consistent datum for all pipeline operations.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import os
import math
import logging
import tempfile
from pathlib import Path
from typing import Optional

import requests
import httpx
import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.crs import CRS
from pyproj import Transformer
from shapely.geometry import box, mapping

logger = logging.getLogger(__name__)


class TerrainError(RuntimeError):
    """Raised when a terrain or land cover data download or processing step fails."""


# ── Constants ────────────────────────────────────────────────────────────────

# Target CRS for all pipeline operations
# EPSG:5070 = NAD83 / Conus Albers — meters, equal area, good for IL
TARGET_CRS = CRS.from_epsg(5070)

# ILHMP clearinghouse — ArcGIS REST tile service for 1/3 arc-second DEM tiles
# Source: https://clearinghouse.isgs.illinois.edu/data/elevation/illinois-height-modernization-ilhmp
ILHMP_TILE_INDEX_URL = (
    "https://clearinghouse.isgs.illinois.edu/arcgis/rest/services/"
    "Elevation/IL_Height_Modernization_DEM/MapServer/0/query"
)

# Fallback: USGS 3DEP 1/3 arc-second national coverage via The National Map
USGS_3DEP_URL = "https://tnmapi.usgs.gov/api/products"
USGS_3DEP_DATASET = "National Elevation Dataset (NED) 1/3 arc-second"


# ── Tile Discovery ───────────────────────────────────────────────────────────

def find_ilhmp_tiles(bbox_wgs84: tuple[float, float, float, float]) -> list[dict]:
    """
    Query the ILHMP clearinghouse tile index for tiles intersecting a bounding box.

    Args:
        bbox_wgs84: (west, south, east, north) in WGS84 decimal degrees

    Returns:
        List of tile metadata dicts with 'name', 'download_url', 'geometry'
    """
    west, south, east, north = bbox_wgs84
    logger.info(f"Querying ILHMP tiles for bbox: {bbox_wgs84}")

    params = {
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "f": "json",
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(ILHMP_TILE_INDEX_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.warning(f"ILHMP tile query failed: {e}. Falling back to USGS 3DEP.")
        return find_usgs_3dep_tiles(bbox_wgs84)

    features = data.get("features", [])
    if not features:
        logger.warning("No ILHMP tiles found for bbox. Falling back to USGS 3DEP.")
        return find_usgs_3dep_tiles(bbox_wgs84)

    tiles = []
    for f in features:
        attrs = f.get("attributes", {})
        url = attrs.get("download_url") or attrs.get("DownloadURL") or attrs.get("URL")
        name = attrs.get("tile_name") or attrs.get("Name") or attrs.get("OBJECTID")
        if url:
            tiles.append({"name": str(name), "download_url": url, "source": "ILHMP"})

    logger.info(f"Found {len(tiles)} ILHMP tiles")
    return tiles


def find_usgs_3dep_tiles(bbox_wgs84: tuple[float, float, float, float]) -> list[dict]:
    """
    Query the USGS National Map API for 1/3 arc-second DEM tiles.
    Fallback when ILHMP data is unavailable (outside Illinois, etc.)
    """
    west, south, east, north = bbox_wgs84
    logger.info("Querying USGS 3DEP tiles (fallback)")

    params = {
        "datasets": USGS_3DEP_DATASET,
        "bbox": f"{west},{south},{east},{north}",
        "outputFormat": "JSON",
        "max": 50,
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(USGS_3DEP_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        raise RuntimeError(f"USGS 3DEP tile query also failed: {e}") from e

    items = data.get("items", [])
    tiles = []
    for item in items:
        url = item.get("downloadURL")
        title = item.get("title", "unknown")
        if url:
            tiles.append({"name": title, "download_url": url, "source": "USGS_3DEP"})

    logger.info(f"Found {len(tiles)} USGS 3DEP tiles")
    return tiles


# ── Download ─────────────────────────────────────────────────────────────────

def download_tile(tile: dict, output_dir: Path) -> Optional[Path]:
    """
    Download a single DEM tile to output_dir. Returns path on success.
    Skips if already downloaded (idempotent).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    fname = output_dir / f"{tile['name']}.tif"

    if fname.exists():
        logger.debug(f"Tile already downloaded: {fname.name}")
        return fname

    url = tile["download_url"]
    logger.info(f"Downloading tile: {tile['name']} from {tile['source']}")

    try:
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(fname, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)
        logger.info(f"Downloaded: {fname.name} ({fname.stat().st_size / 1e6:.1f} MB)")
        return fname
    except Exception as e:
        logger.error(f"Failed to download {tile['name']}: {e}")
        return None


def download_tiles(tiles: list[dict], output_dir: Path) -> list[Path]:
    """Download all tiles, return list of successfully downloaded paths."""
    paths = []
    for tile in tiles:
        path = download_tile(tile, output_dir)
        if path:
            paths.append(path)
    return paths


# ── Mosaic & Reproject ────────────────────────────────────────────────────────

def mosaic_tiles(tile_paths: list[Path], output_path: Path,
                 target_crs: CRS = TARGET_CRS,
                 resolution_m: float = 3.0) -> Path:
    """
    Merge multiple GeoTIFF tiles into a single mosaic, reproject to target CRS.

    Args:
        tile_paths:    List of input GeoTIFF paths
        output_path:   Output mosaic GeoTIFF path
        target_crs:    Target CRS (default: EPSG:5070 Albers)
        resolution_m:  Output resolution in meters (default: 3m ≈ 1/3 arc-second)

    Returns:
        Path to the output mosaic GeoTIFF
    """
    if not tile_paths:
        raise ValueError("No tile paths provided to mosaic_tiles")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Mosaicking {len(tile_paths)} tiles → {output_path.name}")

    # Open all source datasets
    datasets = [rasterio.open(p) for p in tile_paths]

    try:
        # Merge (mosaic) — uses first tile's CRS as intermediate
        mosaic, mosaic_transform = merge(datasets)
        src_crs = datasets[0].crs

        # Reproject to target CRS
        transform, width, height = calculate_default_transform(
            src_crs, target_crs,
            mosaic.shape[2], mosaic.shape[1],
            *rasterio.transform.array_bounds(
                mosaic.shape[1], mosaic.shape[2], mosaic_transform
            ),
            resolution=resolution_m,
        )

        reprojected = np.empty(
            (mosaic.shape[0], height, width), dtype=mosaic.dtype
        )

        reproject(
            source=mosaic,
            destination=reprojected,
            src_transform=mosaic_transform,
            src_crs=src_crs,
            dst_transform=transform,
            dst_crs=target_crs,
            resampling=Resampling.bilinear,
        )

        # Write output
        profile = {
            "driver": "GTiff",
            "dtype": reprojected.dtype,
            "width": width,
            "height": height,
            "count": 1,
            "crs": target_crs,
            "transform": transform,
            "compress": "lzw",
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
            "predictor": 2,  # horizontal differencing — good for elevation data
            "nodata": -9999.0,
        }

        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(reprojected[0], 1)

        logger.info(
            f"Mosaic written: {output_path.name} "
            f"({width}×{height} px, {resolution_m}m resolution, "
            f"CRS: EPSG:{target_crs.to_epsg()})"
        )
        return output_path

    finally:
        for ds in datasets:
            ds.close()


# ── Clip to Watershed ─────────────────────────────────────────────────────────

def clip_to_watershed(dem_path: Path, watershed_geom, output_path: Path) -> Path:
    """
    Clip a DEM mosaic to a watershed polygon boundary with a buffer.

    Args:
        dem_path:       Input DEM GeoTIFF (EPSG:5070)
        watershed_geom: Shapely geometry of watershed boundary (EPSG:5070)
        output_path:    Output clipped GeoTIFF path

    Returns:
        Path to the clipped DEM
    """
    from rasterio.mask import mask

    # Buffer 500m beyond watershed for model stability
    buffered = watershed_geom.buffer(500)

    with rasterio.open(dem_path) as src:
        out_image, out_transform = mask(
            src, [mapping(buffered)], crop=True, nodata=-9999.0
        )
        out_meta = src.meta.copy()
        out_meta.update({
            "height": out_image.shape[1],
            "width": out_image.shape[2],
            "transform": out_transform,
            "nodata": -9999.0,
            "compress": "lzw",
        })

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(output_path, "w", **out_meta) as dst:
        dst.write(out_image)

    logger.info(f"Clipped DEM: {output_path.name}")
    return output_path


# ── High-level entry point ────────────────────────────────────────────────────

def get_terrain(
    bbox_wgs84: tuple[float, float, float, float],
    output_dir: Path,
    resolution_m: float = 3.0,
) -> Path:
    """
    Full terrain pipeline: discover tiles → download → mosaic → reproject.

    Args:
        bbox_wgs84:    (west, south, east, north) bounding box in WGS84
        output_dir:    Directory for tile downloads and output mosaic
        resolution_m:  Output DEM resolution in meters

    Returns:
        Path to the output mosaic GeoTIFF
    """
    output_dir = Path(output_dir)
    tiles_dir = output_dir / "tiles"
    mosaic_path = output_dir / "dem_mosaic.tif"

    if mosaic_path.exists():
        logger.info(f"Mosaic already exists: {mosaic_path}")
        return mosaic_path

    tiles = find_ilhmp_tiles(bbox_wgs84)
    if not tiles:
        raise RuntimeError("No terrain tiles found for the specified bounding box.")

    tile_paths = download_tiles(tiles, tiles_dir)
    if not tile_paths:
        raise RuntimeError("All tile downloads failed.")

    return mosaic_tiles(tile_paths, mosaic_path, resolution_m=resolution_m)


# ── NLCD Land Cover ───────────────────────────────────────────────────────────

# MRLC WCS endpoints — year-keyed; update when new NLCD releases are published
NLCD_WCS_URLS: dict[int, str] = {
    2019: "https://www.mrlc.gov/geoserver/mrlc_download/NLCD_2019_Land_Cover_L48/wcs",
    2021: "https://www.mrlc.gov/geoserver/mrlc_download/NLCD_2021_Land_Cover_L48/wcs",
}


def download_nlcd(
    bbox_wgs84: tuple[float, float, float, float],
    output_dir: Path,
    year: int = 2021,
) -> Path:
    """
    Download NLCD land cover raster for a bounding box.

    Args:
        bbox_wgs84: (west, south, east, north) in WGS84 decimal degrees
        output_dir: Directory to save downloaded raster
        year: NLCD year (default 2021; supported: 2019, 2021)

    Returns:
        Path to downloaded GeoTIFF

    Notes:
        Uses MRLC WCS endpoint. Adds 0.1-degree buffer to bbox to ensure
        full watershed coverage. Downloads are idempotent (skips if exists).
    """
    if year not in NLCD_WCS_URLS:
        raise TerrainError(
            f"Unsupported NLCD year: {year}. Supported: {sorted(NLCD_WCS_URLS)}"
        )

    west, south, east, north = bbox_wgs84
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Cache key uses the original (unbuffered) bbox
    fname = output_dir / f"nlcd_{year}_{west:.2f}_{south:.2f}_{east:.2f}_{north:.2f}.tif"
    if fname.exists():
        logger.debug(f"NLCD already downloaded: {fname.name}")
        return fname

    # Expand bbox by 0.1 degrees on each side for WCS request
    buf = 0.1
    w, s, e, n = west - buf, south - buf, east + buf, north + buf

    base_url = NLCD_WCS_URLS[year]
    coverage_id = f"NLCD_{year}_Land_Cover_L48"

    # WCS 2.0.1 requires two SUBSET params — use list of tuples to allow duplication
    params = [
        ("SERVICE", "WCS"),
        ("VERSION", "2.0.1"),
        ("REQUEST", "GetCoverage"),
        ("COVERAGEID", coverage_id),
        ("SUBSET", f"Long({w},{e})"),
        ("SUBSET", f"Lat({s},{n})"),
        ("FORMAT", "image/tiff"),
    ]

    logger.info(
        f"Downloading NLCD {year} for bbox: "
        f"({w:.3f},{s:.3f},{e:.3f},{n:.3f}) from MRLC WCS"
    )

    try:
        resp = requests.get(base_url, params=params, timeout=120)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise TerrainError(
            f"NLCD WCS download failed: {exc}\n"
            f"URL: {base_url}\n"
            f"SUBSET Long({w:.4f},{e:.4f}) Lat({s:.4f},{n:.4f})"
        ) from exc

    with open(fname, "wb") as f:
        f.write(resp.content)

    logger.info(f"Downloaded NLCD: {fname.name} ({fname.stat().st_size / 1e6:.1f} MB)")
    return fname


def reproject_nlcd(
    nlcd_path: Path,
    target_crs: CRS,
    output_path: Path = None,
    resampling: str = "nearest",
) -> Path:
    """
    Reproject NLCD raster to target CRS (EPSG:5070 for pipeline use).
    Preserves integer dtype (uint8) — use nearest neighbor resampling.

    Args:
        nlcd_path:   Input NLCD GeoTIFF
        target_crs:  Target CRS (e.g. EPSG:5070)
        output_path: Output path; defaults to nlcd_path stem + "_epsg<N>.tif"
        resampling:  Resampling algorithm name (default: "nearest")

    Returns:
        output_path
    """
    nlcd_path = Path(nlcd_path)
    if output_path is None:
        epsg = target_crs.to_epsg() or "reprojected"
        output_path = nlcd_path.with_name(f"{nlcd_path.stem}_epsg{epsg}.tif")
    output_path = Path(output_path)

    if output_path.exists():
        logger.debug(f"Reprojected NLCD already exists: {output_path.name}")
        return output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    resamp = getattr(Resampling, resampling)

    with rasterio.open(nlcd_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, target_crs,
            src.width, src.height,
            *src.bounds,
        )

        profile = src.meta.copy()
        profile.update({
            "crs": target_crs,
            "transform": transform,
            "width": width,
            "height": height,
            "dtype": "uint8",
            "nodata": 0,
            "compress": "lzw",
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
        })

        with rasterio.open(output_path, "w", **profile) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=target_crs,
                    resampling=resamp,
                )

    logger.info(
        f"Reprojected NLCD: {output_path.name} "
        f"({width}×{height} px, CRS: EPSG:{target_crs.to_epsg()})"
    )
    return output_path


def clip_nlcd_to_watershed(
    nlcd_path: Path,
    watershed_geom,
    output_path: Path = None,
    buffer_m: float = 500.0,
) -> Path:
    """
    Clip NLCD raster to watershed geometry with buffer.
    watershed_geom must be in same CRS as nlcd_path.

    Args:
        nlcd_path:      Input NLCD GeoTIFF
        watershed_geom: Shapely geometry or GeoDataFrame (same CRS as nlcd_path)
        output_path:    Output path; defaults to nlcd_path stem + "_clipped.tif"
        buffer_m:       Buffer distance in map units (meters for EPSG:5070)

    Returns:
        output_path
    """
    from rasterio.mask import mask as rio_mask

    nlcd_path = Path(nlcd_path)
    if output_path is None:
        output_path = nlcd_path.with_name(f"{nlcd_path.stem}_clipped.tif")
    output_path = Path(output_path)

    if output_path.exists():
        logger.debug(f"Clipped NLCD already exists: {output_path.name}")
        return output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Accept GeoDataFrame — extract first geometry
    if hasattr(watershed_geom, "geometry"):
        geom = watershed_geom.geometry.iloc[0]
    else:
        geom = watershed_geom

    buffered = geom.buffer(buffer_m)

    with rasterio.open(nlcd_path) as src:
        out_image, out_transform = rio_mask(
            src, [mapping(buffered)], crop=True, nodata=0
        )
        out_meta = src.meta.copy()
        out_meta.update({
            "height": out_image.shape[1],
            "width": out_image.shape[2],
            "transform": out_transform,
            "nodata": 0,
            "dtype": "uint8",
            "compress": "lzw",
        })

    with rasterio.open(output_path, "w", **out_meta) as dst:
        dst.write(out_image)

    logger.info(f"Clipped NLCD: {output_path.name}")
    return output_path


def get_nlcd(
    bbox_wgs84: tuple[float, float, float, float],
    output_dir: Path,
    watershed_geom=None,
    target_crs: CRS = None,
    year: int = 2021,
) -> Path:
    """
    Full NLCD pipeline: download → reproject → clip (if watershed_geom provided).

    This is the primary entry point for NLCD data acquisition.

    Args:
        bbox_wgs84:     (west, south, east, north) in WGS84 decimal degrees
        output_dir:     Working directory for downloaded and processed files
        watershed_geom: Shapely geometry or GeoDataFrame; if provided, clip output
                        to watershed (must be in target_crs)
        target_crs:     Reproject to this CRS; defaults to EPSG:5070 (Albers)
        year:           NLCD year (default 2021; supported: 2019, 2021)

    Returns:
        Path to processed NLCD GeoTIFF ready for model_builder.py
    """
    output_dir = Path(output_dir)

    if target_crs is None:
        target_crs = TARGET_CRS

    # Step 1: Download raw WGS84 raster
    raw_path = download_nlcd(bbox_wgs84, output_dir / "nlcd_raw", year=year)

    # Step 2: Reproject to target CRS (nearest-neighbor — categorical data)
    epsg = target_crs.to_epsg() or "reprojected"
    reprojected_path = output_dir / f"nlcd_{year}_epsg{epsg}.tif"
    reprojected = reproject_nlcd(raw_path, target_crs, reprojected_path)

    # Step 3: Clip to watershed if geometry provided
    if watershed_geom is not None:
        clipped_path = output_dir / f"nlcd_{year}_watershed.tif"
        return clip_nlcd_to_watershed(reprojected, watershed_geom, clipped_path)

    return reprojected


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(description="Download and mosaic terrain tiles")
    parser.add_argument("--west",  type=float, required=True)
    parser.add_argument("--south", type=float, required=True)
    parser.add_argument("--east",  type=float, required=True)
    parser.add_argument("--north", type=float, required=True)
    parser.add_argument("--output", type=str, default="data/terrain")
    parser.add_argument("--resolution", type=float, default=3.0,
                        help="Output DEM resolution in meters (default: 3.0)")
    args = parser.parse_args()

    result = get_terrain(
        bbox_wgs84=(args.west, args.south, args.east, args.north),
        output_dir=Path(args.output),
        resolution_m=args.resolution,
    )
    print(f"Terrain ready: {result}")
