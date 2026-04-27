"""
watershed.py — TauDEM-based watershed delineation

Delineates watershed boundaries, stream networks, and basin characteristics
from a DEM using direct TauDEM CLI execution. All default outputs remain in
EPSG:5070 (NAD83 Albers) for Illinois-first processing.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.features import shapes as rio_shapes
from shapely.geometry import LineString, Point, shape
from shapely.ops import unary_union

from taudem import TauDem

logger = logging.getLogger(__name__)

TARGET_CRS = CRS.from_epsg(5070)


@dataclass
class BasinCharacteristics:
    """Basin characteristics needed for hydrology calculations."""

    drainage_area_km2: float
    drainage_area_mi2: float
    mean_elevation_m: float
    relief_m: float
    main_channel_length_km: float
    main_channel_slope_m_per_m: float
    centroid_lat: float
    centroid_lon: float
    pour_point_lat: float
    pour_point_lon: float
    extra: dict = field(default_factory=dict)


@dataclass
class WatershedResult:
    """Complete TauDEM-backed watershed delineation result."""

    basin: "gpd.GeoDataFrame"
    streams: "gpd.GeoDataFrame"
    subbasins: "gpd.GeoDataFrame"
    centerlines: "gpd.GeoDataFrame"
    breaklines: "gpd.GeoDataFrame"
    pour_point: Point
    characteristics: BasinCharacteristics
    dem_clipped: Path
    artifacts: dict[str, Path] = field(default_factory=dict)


def delineate_watershed(
    dem_path: Path,
    pour_point_lon: float,
    pour_point_lat: float,
    snap_threshold_m: float = 300.0,
    min_stream_area_km2: float = 2.0,
    working_dir: Optional[Path] = None,
    taudem_executable_dir: Optional[Path] = None,
    taudem_processes: int = 1,
) -> WatershedResult:
    """
    Delineate a watershed from a DEM given a pour point (outlet).

    Args:
        dem_path:              Path to DEM GeoTIFF (typically EPSG:5070)
        pour_point_lon:        Outlet longitude (WGS84)
        pour_point_lat:        Outlet latitude (WGS84)
        snap_threshold_m:      Max distance to snap pour point to stream (meters)
        min_stream_area_km2:   Minimum contributing area to define a stream
        working_dir:           Directory for TauDEM intermediate artifacts
        taudem_executable_dir: Optional TauDEM executable directory override
        taudem_processes:      Number of TauDEM MPI processes to request

    Returns:
        WatershedResult with polygon, streams, subbasins, and model-building
        linework derived from TauDEM outputs.
    """
    dem_path = Path(dem_path)
    if not dem_path.exists():
        raise FileNotFoundError(f"DEM not found: {dem_path}")

    logger.info(
        "Delineating watershed with TauDEM for pour point %.4fN, %.4fW",
        pour_point_lat,
        pour_point_lon,
    )

    TauDem.validate_environment(
        required=[
            "PitRemove",
            "D8FlowDir",
            "AreaD8",
            "Threshold",
            "MoveOutletsToStreams",
            "StreamNet",
            "Gridnet",
        ],
        executable_dir=taudem_executable_dir,
    )

    with rasterio.open(dem_path) as src:
        dem_crs = src.crs
        if dem_crs is None:
            raise RuntimeError(f"DEM has no CRS: {dem_path}")
        res_m = abs(src.transform.a)
        bounds = src.bounds
    if not dem_crs.is_projected:
        raise RuntimeError(
            f"TauDEM requires a projected DEM. Found non-projected CRS: {dem_crs}"
        )

    work_dir = Path(working_dir) if working_dir else dem_path.parent / f"{dem_path.stem}_taudem"
    work_dir.mkdir(parents=True, exist_ok=True)

    cell_area_km2 = (res_m / 1000.0) ** 2
    threshold_cells = max(int(min_stream_area_km2 / max(cell_area_km2, 1e-9)), 1)
    snap_cells = max(int(snap_threshold_m / max(res_m, 1e-9)), 1)

    outlet_path = work_dir / "outlet.shp"
    snapped_outlet_path = work_dir / "outlet_snapped.shp"
    transformer = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)
    pp_x, pp_y = transformer.transform(pour_point_lon, pour_point_lat)
    _write_outlet_shapefile(outlet_path, dem_crs, pp_x, pp_y)

    fel = work_dir / "fel.tif"
    p = work_dir / "p.tif"
    sd8 = work_dir / "sd8.tif"
    ad8 = work_dir / "ad8.tif"
    src = work_dir / "src.tif"
    plen = work_dir / "plen.tif"
    tlen = work_dir / "tlen.tif"
    gord = work_dir / "gord.tif"
    ord_grid = work_dir / "ord.tif"
    tree = work_dir / "tree.dat"
    coord = work_dir / "coord.dat"
    net = work_dir / "net.shp"
    w = work_dir / "w.tif"

    TauDem.pit_remove(
        dem_path,
        fel,
        executable_dir=taudem_executable_dir,
        processes=taudem_processes,
    )
    TauDem.d8_flow_dir(
        fel,
        p,
        sd8,
        executable_dir=taudem_executable_dir,
        processes=taudem_processes,
    )
    TauDem.area_d8(
        p,
        ad8,
        edge_contamination=False,
        executable_dir=taudem_executable_dir,
        processes=taudem_processes,
    )
    TauDem.threshold(
        ad8,
        src,
        threshold_cells,
        executable_dir=taudem_executable_dir,
        processes=taudem_processes,
    )
    TauDem.move_outlets_to_streams(
        p,
        src,
        outlet_path,
        snapped_outlet_path,
        maxdist=snap_cells,
        executable_dir=taudem_executable_dir,
        processes=taudem_processes,
    )
    TauDem.grid_net(
        p,
        plen,
        tlen,
        gord,
        outletfile=snapped_outlet_path,
        maskfile=src,
        threshold=1,
        executable_dir=taudem_executable_dir,
        processes=taudem_processes,
    )
    TauDem.stream_net(
        fel,
        p,
        ad8,
        src,
        ord_grid,
        tree,
        coord,
        net,
        w,
        outletfile=snapped_outlet_path,
        executable_dir=taudem_executable_dir,
        processes=taudem_processes,
    )

    streams_gdf = _read_stream_network(net, dem_crs)
    subbasins_gdf = _polygonize_watershed_grid(w, dem_crs)
    if subbasins_gdf.empty:
        raise RuntimeError("TauDEM produced no subbasin polygons; check DEM and outlet inputs.")

    basin_shape = unary_union(list(subbasins_gdf.geometry))
    basin_gdf = gpd.GeoDataFrame({"name": ["watershed"]}, geometry=[basin_shape], crs=dem_crs)

    snapped_outlet_gdf = gpd.read_file(snapped_outlet_path)
    if snapped_outlet_gdf.crs is None:
        snapped_outlet_gdf = snapped_outlet_gdf.set_crs(dem_crs)
    snapped_point = snapped_outlet_gdf.to_crs(dem_crs).geometry.iloc[0]

    from terrain import clip_to_watershed

    clipped_dem_path = work_dir / f"{dem_path.stem}_watershed.tif"
    clipped_dem = clip_to_watershed(dem_path, basin_shape, clipped_dem_path)

    centerlines_gdf = _build_centerlines(streams_gdf)
    breaklines_gdf = _build_breaklines(basin_shape, centerlines_gdf, dem_crs)
    characteristics = _compute_basin_characteristics(
        basin_shape=basin_shape,
        basin_crs=dem_crs,
        clipped_dem_path=clipped_dem,
        streams_gdf=streams_gdf,
        pour_point=snapped_point,
        pour_point_lon=pour_point_lon,
        pour_point_lat=pour_point_lat,
        cell_area_km2=cell_area_km2,
        threshold_cells=threshold_cells,
        source_bounds=bounds,
    )

    artifacts = {
        "fel": fel,
        "p": p,
        "sd8": sd8,
        "ad8": ad8,
        "src": src,
        "plen": plen,
        "tlen": tlen,
        "gord": gord,
        "ord": ord_grid,
        "tree": tree,
        "coord": coord,
        "net": net,
        "w": w,
        "outlet": outlet_path,
        "snapped_outlet": snapped_outlet_path,
        "dem_clipped": clipped_dem,
    }

    logger.info(
        "TauDEM watershed complete: %.1f km², %d stream reaches, %d subbasins",
        characteristics.drainage_area_km2,
        len(streams_gdf),
        len(subbasins_gdf),
    )

    return WatershedResult(
        basin=basin_gdf,
        streams=streams_gdf,
        subbasins=subbasins_gdf,
        centerlines=centerlines_gdf,
        breaklines=breaklines_gdf,
        pour_point=snapped_point,
        characteristics=characteristics,
        dem_clipped=clipped_dem,
        artifacts=artifacts,
    )


def _write_outlet_shapefile(outlet_path: Path, crs, x: float, y: float) -> None:
    outlet_gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(x, y)], crs=crs)
    outlet_gdf.to_file(outlet_path, driver="ESRI Shapefile")


def _read_stream_network(net_path: Path, crs) -> "gpd.GeoDataFrame":
    streams = gpd.read_file(net_path)
    if streams.crs is None:
        streams = streams.set_crs(crs)
    if "stream_id" not in streams.columns:
        streams["stream_id"] = range(1, len(streams) + 1)
    return streams.to_crs(crs)


def _polygonize_watershed_grid(w_path: Path, crs) -> "gpd.GeoDataFrame":
    with rasterio.open(w_path) as src:
        data = src.read(1)
        mask = np.isfinite(data) & (data > 0)
        features = []
        for geom, value in rio_shapes(data.astype(np.int32), mask=mask, transform=src.transform):
            value_int = int(value)
            if value_int <= 0:
                continue
            features.append({"wsno": value_int, "geometry": shape(geom)})
    if not features:
        return gpd.GeoDataFrame({"wsno": []}, geometry=[], crs=crs)
    gdf = gpd.GeoDataFrame(features, crs=crs)
    return gdf.dissolve(by="wsno", as_index=False)


def _build_centerlines(streams_gdf: "gpd.GeoDataFrame") -> "gpd.GeoDataFrame":
    centerlines = streams_gdf.copy()
    if "centerline_id" not in centerlines.columns:
        centerlines["centerline_id"] = range(1, len(centerlines) + 1)
    return centerlines


def _build_breaklines(
    basin_shape,
    centerlines_gdf: "gpd.GeoDataFrame",
    crs,
) -> "gpd.GeoDataFrame":
    breakline_geoms = []
    breakline_types = []

    for geom in centerlines_gdf.geometry:
        breakline_geoms.append(geom)
        breakline_types.append("stream")

    boundary = basin_shape.boundary
    if isinstance(boundary, LineString):
        breakline_geoms.append(boundary)
        breakline_types.append("boundary")
    else:
        for geom in getattr(boundary, "geoms", []):
            breakline_geoms.append(geom)
            breakline_types.append("boundary")

    return gpd.GeoDataFrame(
        {"breakline_type": breakline_types},
        geometry=breakline_geoms,
        crs=crs,
    )


def _compute_basin_characteristics(
    basin_shape,
    basin_crs,
    clipped_dem_path: Path,
    streams_gdf: "gpd.GeoDataFrame",
    pour_point: Point,
    pour_point_lon: float,
    pour_point_lat: float,
    cell_area_km2: float,
    threshold_cells: int,
    source_bounds,
) -> BasinCharacteristics:
    area_km2 = basin_shape.area / 1e6
    area_mi2 = area_km2 * 0.386102

    with rasterio.open(clipped_dem_path) as src:
        dem = src.read(1, masked=True)
        valid = np.asarray(dem.compressed(), dtype=float)

    if valid.size > 0:
        mean_elev = float(valid.mean())
        relief = float(valid.max() - valid.min())
    else:
        mean_elev = 0.0
        relief = 0.0

    slope = 0.001
    main_channel_km = math.sqrt(max(area_km2, cell_area_km2))
    if len(streams_gdf) > 0:
        streams = streams_gdf.copy()
        streams["length_km"] = streams.geometry.length / 1000.0
        idx = streams["length_km"].idxmax()
        main_channel_km = float(streams.loc[idx, "length_km"])
        slope_candidates = []
        for col in ("Slope", "strmDrop", "Length"):
            if col in streams.columns:
                slope_candidates.append(col)
        if "Slope" in streams.columns:
            slope_value = streams.loc[idx, "Slope"]
            if slope_value is not None and np.isfinite(float(slope_value)):
                slope = max(float(slope_value), 1e-6)
        elif "strmDrop" in streams.columns and "Length" in streams.columns:
            drop = float(streams.loc[idx, "strmDrop"])
            length = float(streams.loc[idx, "Length"])
            if length > 0:
                slope = max(drop / length, 1e-6)
        elif relief > 0 and main_channel_km > 0:
            slope = max(relief / (main_channel_km * 1000.0), 1e-6)

    centroid = basin_shape.centroid
    to_wgs84 = Transformer.from_crs(basin_crs, "EPSG:4326", always_xy=True)
    centroid_lon, centroid_lat = to_wgs84.transform(centroid.x, centroid.y)

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
        extra={
            "threshold_cells": threshold_cells,
            "cell_area_km2": cell_area_km2,
            "source_bounds": tuple(source_bounds),
            "snapped_pour_point_x": float(pour_point.x),
            "snapped_pour_point_y": float(pour_point.y),
        },
    )


def save_watershed(result: WatershedResult, output_dir: Path) -> dict[str, Path]:
    """Save watershed outputs and return the written paths."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths = {}

    basin_path = output_dir / "watershed_boundary.gpkg"
    result.basin.to_file(basin_path, driver="GPKG")
    paths["basin"] = basin_path

    streams_path = output_dir / "stream_network.gpkg"
    result.streams.to_file(streams_path, driver="GPKG")
    paths["streams"] = streams_path

    subbasins_path = output_dir / "subbasins.gpkg"
    result.subbasins.to_file(subbasins_path, driver="GPKG")
    paths["subbasins"] = subbasins_path

    centerlines_path = output_dir / "river_centerlines.gpkg"
    result.centerlines.to_file(centerlines_path, driver="GPKG")
    paths["centerlines"] = centerlines_path

    breaklines_path = output_dir / "breaklines.gpkg"
    result.breaklines.to_file(breaklines_path, driver="GPKG")
    paths["breaklines"] = breaklines_path

    paths.update(result.artifacts)

    logger.info("Saved watershed outputs to %s", output_dir)
    return paths


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(description="Delineate watershed from DEM using TauDEM")
    parser.add_argument("--dem", required=True, help="Path to DEM GeoTIFF")
    parser.add_argument("--lon", type=float, required=True, help="Pour point longitude (WGS84)")
    parser.add_argument("--lat", type=float, required=True, help="Pour point latitude (WGS84)")
    parser.add_argument("--output", default="data/watershed")
    parser.add_argument("--snap", type=float, default=300.0, help="Snap threshold in meters")
    parser.add_argument("--stream-area", type=float, default=2.0, help="Minimum stream area in km^2")
    parser.add_argument("--taudem-dir", default=None, help="Optional TauDEM executable directory")
    parser.add_argument("--processes", type=int, default=1, help="TauDEM MPI process count")
    args = parser.parse_args()

    result = delineate_watershed(
        dem_path=Path(args.dem),
        pour_point_lon=args.lon,
        pour_point_lat=args.lat,
        snap_threshold_m=args.snap,
        min_stream_area_km2=args.stream_area,
        taudem_executable_dir=Path(args.taudem_dir) if args.taudem_dir else None,
        taudem_processes=args.processes,
    )
    paths = save_watershed(result, Path(args.output))
    chars = result.characteristics
    print("\nWatershed delineated:")
    print(f"  Area:           {chars.drainage_area_km2:.2f} km^2 ({chars.drainage_area_mi2:.2f} mi^2)")
    print(f"  Relief:         {chars.relief_m:.1f} m")
    print(f"  Channel length: {chars.main_channel_length_km:.2f} km")
    print(f"  Channel slope:  {chars.main_channel_slope_m_per_m:.5f} m/m")
    print(f"  Outputs:        {paths}")
