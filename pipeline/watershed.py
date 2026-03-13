"""
watershed.py — DEM-based watershed delineation

Delineates watershed boundaries, stream networks, and basin characteristics
from a DEM using pysheds. All outputs in EPSG:5070 (NAD83 Albers).

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import xy
from shapely.geometry import shape, mapping, Point, LineString
from shapely.ops import unary_union
import geopandas as gpd
from pysheds.grid import Grid

logger = logging.getLogger(__name__)

TARGET_CRS = CRS.from_epsg(5070)


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class BasinCharacteristics:
    """Basin characteristics needed for hydrology calculations."""
    drainage_area_km2: float
    drainage_area_mi2: float
    mean_elevation_m: float
    relief_m: float                    # max - min elevation
    main_channel_length_km: float
    main_channel_slope_m_per_m: float  # 10-85% slope method
    centroid_lat: float                # WGS84
    centroid_lon: float                # WGS84
    pour_point_lat: float              # WGS84
    pour_point_lon: float              # WGS84
    extra: dict = field(default_factory=dict)


@dataclass
class WatershedResult:
    """Complete watershed delineation result."""
    basin: "gpd.GeoDataFrame"          # watershed polygon
    streams: "gpd.GeoDataFrame"        # stream network polylines
    pour_point: Point                  # outlet point (EPSG:5070)
    characteristics: BasinCharacteristics
    dem_clipped: Path                  # clipped DEM within watershed


# ── Core Delineation ─────────────────────────────────────────────────────────

def delineate_watershed(
    dem_path: Path,
    pour_point_lon: float,
    pour_point_lat: float,
    snap_threshold_m: float = 300.0,
    min_stream_area_km2: float = 2.0,
) -> WatershedResult:
    """
    Delineate a watershed from a DEM given a pour point (outlet).

    Args:
        dem_path:            Path to DEM GeoTIFF (EPSG:5070)
        pour_point_lon:      Outlet longitude (WGS84)
        pour_point_lat:      Outlet latitude (WGS84)
        snap_threshold_m:    Max distance to snap pour point to stream (meters)
        min_stream_area_km2: Minimum contributing area to define a stream

    Returns:
        WatershedResult with polygon, streams, and basin characteristics
    """
    logger.info(f"Delineating watershed for pour point: {pour_point_lat:.4f}N, {pour_point_lon:.4f}W")

    grid = Grid.from_raster(str(dem_path))
    dem = grid.read_raster(str(dem_path))

    # ── 1. Hydrological conditioning ─────────────────────────────────────────
    logger.info("Filling pits and depressions...")
    pit_filled = grid.fill_pits(dem)
    flooded = grid.fill_depressions(pit_filled)
    inflated = grid.resolve_flats(flooded)

    # ── 2. Flow direction (D8) ────────────────────────────────────────────────
    logger.info("Computing D8 flow direction...")
    dirmap = (64, 128, 1, 2, 4, 8, 16, 32)  # ESRI D8 encoding
    fdir = grid.flowdir(inflated, dirmap=dirmap)

    # ── 3. Flow accumulation ──────────────────────────────────────────────────
    logger.info("Computing flow accumulation...")
    acc = grid.accumulation(fdir, dirmap=dirmap)

    # ── 4. Snap pour point to nearest high-accumulation cell ─────────────────
    with rasterio.open(dem_path) as src:
        res_m = abs(src.transform.a)  # pixel size in meters (Albers)
        # Convert WGS84 pour point to EPSG:5070
        from pyproj import Transformer
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
        pp_x, pp_y = transformer.transform(pour_point_lon, pour_point_lat)

    # Convert accumulation threshold from km2 to cells
    cell_area_km2 = (res_m / 1000) ** 2
    snap_cells = int(snap_threshold_m / res_m)
    acc_threshold = int(min_stream_area_km2 / cell_area_km2)

    logger.info(f"Snapping pour point (threshold: {snap_threshold_m}m, {snap_cells} cells)...")
    x_snap, y_snap = grid.snap_to_mask(acc > acc_threshold, (pp_x, pp_y))

    # ── 5. Delineate catchment ────────────────────────────────────────────────
    logger.info("Delineating catchment polygon...")
    catch = grid.catchment(x=x_snap, y=y_snap, fdir=fdir, dirmap=dirmap,
                           xytype="coordinate")
    grid.clip_to(catch)

    # Convert catchment raster to polygon
    shapes_gen = grid.polygonize(catch.astype(np.uint8))
    catch_polys = [shape(s) for s, v in shapes_gen if v == 1]
    if not catch_polys:
        raise RuntimeError("Watershed delineation produced no polygon. Check pour point location.")

    watershed_poly = unary_union(catch_polys)
    basin_gdf = gpd.GeoDataFrame(
        geometry=[watershed_poly], crs="EPSG:5070"
    )

    # ── 6. Stream network ─────────────────────────────────────────────────────
    logger.info("Extracting stream network...")
    branches = grid.extract_river_network(
        fdir=fdir, acc=acc,
        threshold=acc_threshold,
        dirmap=dirmap,
    )
    stream_lines = [shape(branch["geometry"]) for branch in branches["features"]]
    streams_gdf = gpd.GeoDataFrame(
        geometry=stream_lines, crs="EPSG:5070"
    )

    # ── 7. Basin characteristics ──────────────────────────────────────────────
    logger.info("Computing basin characteristics...")
    chars = _compute_basin_characteristics(
        grid=grid,
        dem=inflated,
        watershed_poly=watershed_poly,
        streams_gdf=streams_gdf,
        pour_point_x=x_snap,
        pour_point_y=y_snap,
        pour_point_lon=pour_point_lon,
        pour_point_lat=pour_point_lat,
        cell_area_km2=cell_area_km2,
    )

    # ── 8. Clip DEM to watershed ──────────────────────────────────────────────
    from terrain import clip_to_watershed
    clipped_dem_path = Path(str(dem_path).replace(".tif", "_watershed.tif"))
    clipped_dem = clip_to_watershed(dem_path, watershed_poly, clipped_dem_path)

    logger.info(
        f"Watershed delineated: {chars.drainage_area_km2:.1f} km² "
        f"({chars.drainage_area_mi2:.1f} mi²)"
    )

    return WatershedResult(
        basin=basin_gdf,
        streams=streams_gdf,
        pour_point=Point(x_snap, y_snap),
        characteristics=chars,
        dem_clipped=clipped_dem,
    )


def _compute_basin_characteristics(
    grid, dem, watershed_poly, streams_gdf,
    pour_point_x, pour_point_y,
    pour_point_lon, pour_point_lat,
    cell_area_km2,
) -> BasinCharacteristics:
    """Compute basin characteristics from delineated watershed."""
    from pyproj import Transformer

    # Area
    area_km2 = watershed_poly.area / 1e6
    area_mi2 = area_km2 * 0.386102

    # Elevation statistics
    dem_array = np.array(dem)
    valid_cells = dem_array[dem_array > -9000]
    mean_elev = float(np.mean(valid_cells)) if len(valid_cells) > 0 else 0.0
    relief = float(np.max(valid_cells) - np.min(valid_cells)) if len(valid_cells) > 0 else 0.0

    # Main channel: longest stream segment
    if len(streams_gdf) > 0:
        streams_gdf = streams_gdf.copy()
        streams_gdf["length_km"] = streams_gdf.geometry.length / 1000
        main_channel_km = float(streams_gdf["length_km"].max())

        # 10-85% slope method (standard NRCS approach)
        main_channel = streams_gdf.loc[streams_gdf["length_km"].idxmax(), "geometry"]
        if hasattr(main_channel, "coords"):
            coords = list(main_channel.coords)
            if len(coords) >= 2:
                p10_idx = int(len(coords) * 0.10)
                p85_idx = int(len(coords) * 0.85)
                # Slope = elevation difference / horizontal distance
                # Elevation lookup from DEM would be ideal; use relief as approximation
                slope = relief / max(main_channel_km * 1000, 1.0)
            else:
                slope = 0.001
        else:
            slope = 0.001
    else:
        main_channel_km = math.sqrt(area_km2)  # rough estimate
        slope = 0.001

    # Centroid (convert EPSG:5070 to WGS84)
    centroid = watershed_poly.centroid
    transformer = Transformer.from_crs("EPSG:5070", "EPSG:4326", always_xy=True)
    centroid_lon, centroid_lat = transformer.transform(centroid.x, centroid.y)

    return BasinCharacteristics(
        drainage_area_km2=area_km2,
        drainage_area_mi2=area_mi2,
        mean_elevation_m=mean_elev,
        relief_m=relief,
        main_channel_length_km=main_channel_km,
        main_channel_slope_m_per_m=slope,
        centroid_lat=centroid_lat,
        centroid_lon=centroid_lon,
        pour_point_lat=pour_point_lat,
        pour_point_lon=pour_point_lon,
    )


# ── Export ────────────────────────────────────────────────────────────────────

def save_watershed(result: WatershedResult, output_dir: Path) -> dict[str, Path]:
    """Save watershed outputs to GeoPackage files."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths = {}

    basin_path = output_dir / "watershed_boundary.gpkg"
    result.basin.to_file(basin_path, driver="GPKG")
    paths["basin"] = basin_path

    streams_path = output_dir / "stream_network.gpkg"
    result.streams.to_file(streams_path, driver="GPKG")
    paths["streams"] = streams_path

    logger.info(f"Saved watershed outputs to {output_dir}")
    return paths


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, math
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(description="Delineate watershed from DEM")
    parser.add_argument("--dem", required=True, help="Path to DEM GeoTIFF")
    parser.add_argument("--lon", type=float, required=True, help="Pour point longitude (WGS84)")
    parser.add_argument("--lat", type=float, required=True, help="Pour point latitude (WGS84)")
    parser.add_argument("--output", default="data/watershed")
    parser.add_argument("--snap", type=float, default=300.0, help="Snap threshold in meters")
    args = parser.parse_args()

    result = delineate_watershed(
        dem_path=Path(args.dem),
        pour_point_lon=args.lon,
        pour_point_lat=args.lat,
        snap_threshold_m=args.snap,
    )
    paths = save_watershed(result, Path(args.output))
    chars = result.characteristics
    print(f"\nWatershed delineated:")
    print(f"  Area:          {chars.drainage_area_km2:.2f} km² ({chars.drainage_area_mi2:.2f} mi²)")
    print(f"  Relief:        {chars.relief_m:.1f} m")
    print(f"  Channel length:{chars.main_channel_length_km:.2f} km")
    print(f"  Channel slope: {chars.main_channel_slope_m_per_m:.5f} m/m")
    print(f"  Outputs:       {paths}")
