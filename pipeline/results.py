"""
results.py — HEC-RAS 2D results extraction and GIS export

Reads HEC-RAS 6.x unsteady output HDF5 files via h5py, extracts maximum
depth, water-surface elevation, and velocity fields from 2D flow area results,
interpolates irregular cell-center data onto regular grids, and exports as
Cloud-Optimized GeoTIFFs and GeoPackage/Shapefile flood extent polygons.

All output is in EPSG:5070 (NAD83 Albers Equal Area, meters) — consistent with
the rest of the RAS Agent pipeline.

HEC-RAS 6.x HDF5 result paths:
  /Geometry/2D Flow Areas/<area>/Cells Center Coordinate   (N,2) float64
  /Results/Unsteady/Output/Output Blocks/Base Output/
      Unsteady Time Series/2D Flow Areas/<area>/Depth       (T,N) float32
  /Results/Unsteady/Output/Output Blocks/Base Output/
      Unsteady Time Series/2D Flow Areas/<area>/Water Surface (T,N) float32
  /Results/Unsteady/Output/Output Blocks/Base Output/
      Unsteady Time Series/2D Flow Areas/<area>/Velocity    (T,N) float32

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import logging
from pathlib import Path
from typing import Optional

import h5py
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from scipy.interpolate import griddata
from shapely.geometry import MultiPoint
from shapely.ops import unary_union
import geopandas as gpd

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

TARGET_CRS = CRS.from_epsg(5070)

# HDF path templates
_GEOM_BASE = "Geometry/2D Flow Areas/{area}"
_CELL_CENTERS = _GEOM_BASE + "/Cells Center Coordinate"
_RESULTS_BASE = (
    "Results/Unsteady/Output/Output Blocks/Base Output/"
    "Unsteady Time Series/2D Flow Areas/{area}"
)
_DEPTH_PATH = _RESULTS_BASE + "/Depth"
_WSE_PATH = _RESULTS_BASE + "/Water Surface"
_VEL_PATH = _RESULTS_BASE + "/Velocity"

# COG creation options
_COG_OPTIONS = {
    "driver": "GTiff",
    "compress": "lzw",
    "tiled": True,
    "blockxsize": 256,
    "blockysize": 256,
    "BIGTIFF": "IF_SAFER",
}
NODATA = -9999.0
_OVERVIEW_LEVELS = [2, 4, 8, 16]


# ── Area Discovery ────────────────────────────────────────────────────────────

def get_2d_area_names(hdf_path: Path) -> list[str]:
    """
    List all 2D flow area names defined in the geometry section.

    Uses ras-commander HdfMesh.get_mesh_area_names() if available,
    falls back to direct h5py.

    Args:
        hdf_path: Path to a HEC-RAS plan or geometry HDF file.

    Returns:
        List of area name strings (e.g. ['Perimeter 1', 'TestArea']).
    """
    # Try ras-commander first (skip if result is empty — may be mock HDF without
    # the Attributes dataset that RC expects)
    try:
        from ras_commander.hdf import HdfMesh
        names = HdfMesh.get_mesh_area_names(hdf_path)
        if names:
            logger.debug(f"Found {len(names)} 2D area(s) via RC in {hdf_path.name}: {names}")
            return names
        # RC returned empty — fall through to h5py which handles simpler HDF layouts
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"RC get_mesh_area_names failed ({e}), falling back to h5py")

    # Fallback: direct h5py
    with h5py.File(str(hdf_path), "r") as hf:
        grp = hf.get("Geometry/2D Flow Areas")
        if grp is None:
            logger.warning(f"No 'Geometry/2D Flow Areas' group in {hdf_path.name}")
            return []
        names = list(grp.keys())
    logger.debug(f"Found {len(names)} 2D area(s) in {hdf_path.name}: {names}")
    return names


# ── Data Extraction ───────────────────────────────────────────────────────────

def _load_cell_centers(hf: h5py.File, area_name: str) -> np.ndarray:
    """
    Load cell-center coordinates for one 2D flow area.

    Args:
        hf:        Open h5py File object.
        area_name: 2D flow area name.

    Returns:
        Array of shape (N, 2) with (x, y) coordinates in the project CRS.
    """
    path = _CELL_CENTERS.format(area=area_name)
    ds = hf.get(path)
    if ds is None:
        raise KeyError(f"Cell centers not found at HDF path: {path}")
    return np.array(ds, dtype=np.float64)


def extract_max_depth(
    hdf_path: Path,
    area_name: str,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Extract the maximum simulated depth for each 2D mesh cell.

    Args:
        hdf_path:  Path to the HEC-RAS output HDF file.
        area_name: Name of the 2D flow area to extract.

    Returns:
        Tuple of:
          - cell_centers_xy: (N, 2) float64 array of cell-center coordinates.
          - max_depth:       (N,)   float32 array of maximum depth per cell (m).

    Raises:
        KeyError: If depth data is not found for the specified area.
    """
    with h5py.File(str(hdf_path), "r") as hf:
        xy = _load_cell_centers(hf, area_name)
        path = _DEPTH_PATH.format(area=area_name)
        ds = hf.get(path)
        if ds is None:
            raise KeyError(f"Depth dataset not found at HDF path: {path}")
        depths = np.array(ds, dtype=np.float32)   # shape (T, N)

    max_depth = np.max(depths, axis=0)   # (N,)
    logger.debug(
        f"extract_max_depth({area_name}): "
        f"{xy.shape[0]} cells, peak max_depth={max_depth.max():.2f} m"
    )
    return xy, max_depth


def extract_max_wse(
    hdf_path: Path,
    area_name: str,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Extract the maximum water-surface elevation for each 2D mesh cell.

    Uses ras-commander HdfResultsMesh.get_mesh_max_ws() if available,
    falls back to direct h5py.

    Args:
        hdf_path:  Path to the HEC-RAS output HDF file.
        area_name: Name of the 2D flow area to extract.

    Returns:
        Tuple of:
          - cell_centers_xy: (N, 2) float64 array of cell-center coordinates.
          - max_wse:         (N,)   float32 array of maximum WSE per cell (m).

    Raises:
        KeyError: If water surface data is not found for the specified area.
    """
    # Try ras-commander first (may fail on mock HDF files without full structure)
    try:
        from ras_commander.hdf import HdfResultsMesh
        gdf = HdfResultsMesh.get_mesh_max_ws(hdf_path)
        if gdf is not None and len(gdf) > 0:
            # Filter to requested area if mesh_name column exists
            if "mesh_name" in gdf.columns:
                area_gdf = gdf[gdf["mesh_name"] == area_name]
                if len(area_gdf) == 0:
                    area_gdf = gdf  # Fall through if no match
            else:
                area_gdf = gdf
            xy = np.column_stack([area_gdf.geometry.x, area_gdf.geometry.y])
            max_wse = area_gdf["maximum_water_surface"].values.astype(np.float32)
            logger.debug(
                f"extract_max_wse({area_name}) via RC: "
                f"{len(xy)} cells, peak max_wse={max_wse.max():.2f} m"
            )
            return xy, max_wse
    except ImportError:
        pass
    except Exception:
        pass  # Fall through to h5py

    # Fallback: direct h5py
    with h5py.File(str(hdf_path), "r") as hf:
        xy = _load_cell_centers(hf, area_name)
        path = _WSE_PATH.format(area=area_name)
        ds = hf.get(path)
        if ds is None:
            raise KeyError(f"Water Surface dataset not found at HDF path: {path}")
        wse = np.array(ds, dtype=np.float32)   # shape (T, N)

    max_wse = np.max(wse, axis=0)   # (N,)
    logger.debug(
        f"extract_max_wse({area_name}): "
        f"{xy.shape[0]} cells, peak max_wse={max_wse.max():.2f} m"
    )
    return xy, max_wse


# ── Raster Export ─────────────────────────────────────────────────────────────

def cells_to_raster(
    cell_centers_xy: np.ndarray,
    values: np.ndarray,
    crs: CRS,
    resolution_m: float = 3.0,
    output_path: Optional[Path] = None,
) -> Path:
    """
    Interpolate irregular cell-center point data onto a regular grid and write
    a Cloud-Optimized GeoTIFF (COG).

    Uses scipy.interpolate.griddata with method='linear' for the interpolation.
    Areas with no nearby input points are filled with nodata (-9999.0).

    Args:
        cell_centers_xy: (N, 2) array of (x, y) coordinates in the output CRS.
        values:          (N,) array of scalar values at each cell center.
        crs:             Coordinate reference system for the output raster.
        resolution_m:    Grid cell size in CRS units (meters for EPSG:5070).
        output_path:     Write the COG to this path.  A temporary file is used
                         if None; the caller is responsible for cleaning it up.

    Returns:
        Path to the written GeoTIFF file.
    """
    if output_path is None:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        output_path = Path(tmp.name)
        tmp.close()

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    x = cell_centers_xy[:, 0]
    y = cell_centers_xy[:, 1]

    x_min, x_max = x.min(), x.max()
    y_min, y_max = y.min(), y.max()

    # Build regular grid
    cols = max(2, int(np.ceil((x_max - x_min) / resolution_m)) + 1)
    rows = max(2, int(np.ceil((y_max - y_min) / resolution_m)) + 1)
    grid_x = np.linspace(x_min, x_max, cols)
    grid_y = np.linspace(y_max, y_min, rows)   # top-to-bottom (raster convention)
    gx, gy = np.meshgrid(grid_x, grid_y)

    # Interpolate — areas outside convex hull of input points become NaN
    grid_vals = griddata(
        points=cell_centers_xy,
        values=values.astype(np.float64),
        xi=(gx, gy),
        method="linear",
        fill_value=NODATA,
    ).astype(np.float32)

    # Replace NaN with nodata
    grid_vals = np.where(np.isnan(grid_vals), NODATA, grid_vals)

    transform = from_bounds(x_min, y_min, x_max, y_max, cols, rows)

    with rasterio.open(
        str(output_path),
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype=np.float32,
        crs=crs,
        transform=transform,
        nodata=NODATA,
        compress="lzw",
        tiled=True,
        blockxsize=256,
        blockysize=256,
        BIGTIFF="IF_SAFER",
    ) as dst:
        dst.write(grid_vals, 1)

        # Add reduced-resolution overviews (makes it a proper COG)
        dst.build_overviews(_OVERVIEW_LEVELS, rasterio.enums.Resampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")

    logger.info(f"Wrote raster ({rows}×{cols} px, {resolution_m}m): {output_path}")
    return output_path


# ── Flood Extent Polygon ──────────────────────────────────────────────────────

def extract_flood_extent(
    hdf_path: Path,
    area_name: str,
    depth_threshold_m: float = 0.1,
) -> gpd.GeoDataFrame:
    """
    Derive a flood extent polygon from all cells whose maximum depth exceeds
    a threshold.

    Each flooded cell center is buffered by half the approximate cell spacing
    and then merged into a single polygon using shapely unary_union.

    Args:
        hdf_path:         Path to the HEC-RAS output HDF file.
        area_name:        Name of the 2D flow area.
        depth_threshold_m: Minimum depth (m) to classify a cell as flooded.

    Returns:
        GeoDataFrame with one row (the flood extent polygon) in EPSG:5070.
        Returns an empty GeoDataFrame if no cells exceed the threshold.
    """
    xy, max_depth = extract_max_depth(hdf_path, area_name)

    flooded_mask = max_depth > depth_threshold_m
    flooded_xy = xy[flooded_mask]
    logger.debug(
        f"extract_flood_extent: {flooded_mask.sum()} / {len(max_depth)} cells flooded "
        f"(threshold={depth_threshold_m} m)"
    )

    if len(flooded_xy) == 0:
        logger.warning("No flooded cells found; returning empty flood extent.")
        return gpd.GeoDataFrame(geometry=[], crs=TARGET_CRS)

    # Estimate approximate cell spacing from overall point density
    x_range = xy[:, 0].max() - xy[:, 0].min()
    y_range = xy[:, 1].max() - xy[:, 1].min()
    approx_spacing = np.sqrt((x_range * y_range) / max(len(xy), 1))
    buffer_dist = approx_spacing * 0.6   # slightly more than half-cell

    # Buffer each flooded cell center and union into a single polygon
    points = MultiPoint(flooded_xy.tolist())
    flood_poly = points.buffer(buffer_dist).simplify(buffer_dist * 0.1)
    flood_poly = unary_union(flood_poly)

    gdf = gpd.GeoDataFrame(
        {"area_name": [area_name], "depth_threshold_m": [depth_threshold_m]},
        geometry=[flood_poly],
        crs=TARGET_CRS,
    )
    logger.info(
        f"Flood extent polygon: "
        f"{flood_poly.area / 1e6:.3f} km² "
        f"({flooded_mask.sum()} flooded cells)"
    )
    return gdf


# ── Full Export Pipeline ──────────────────────────────────────────────────────

def export_results(
    hdf_path: Path,
    output_dir: Path,
    crs: CRS = None,
    resolution_m: float = 3.0,
    r2_config=None,
) -> dict[str, Path]:
    """
    Run the full results export pipeline for all 2D areas in an HDF file.

    For each 2D flow area:
      1. Extract maximum depth  → depth_grid.tif  (Cloud-Optimized GeoTIFF)
      2. Extract maximum WSE    → wse_grid.tif    (Cloud-Optimized GeoTIFF)
      3. Extract flood extent   → flood_extent.gpkg + flood_extent.shp

    Args:
        hdf_path:     Path to the HEC-RAS output HDF file.
        output_dir:   Directory to write all output files.
        crs:          Output CRS (defaults to EPSG:5070).
        resolution_m: Raster grid resolution in CRS units (default 3.0 m).

    Returns:
        Dict mapping output name to output Path:
          {
            'depth_grid': Path('…/depth_grid.tif'),
            'wse_grid':   Path('…/wse_grid.tif'),
            'flood_extent_gpkg': Path('…/flood_extent.gpkg'),
            'flood_extent_shp':  Path('…/flood_extent.shp'),
          }

    Raises:
        RuntimeError: If the HDF file contains no 2D flow areas.
    """
    if crs is None:
        crs = TARGET_CRS

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    area_names = get_2d_area_names(hdf_path)
    if not area_names:
        raise RuntimeError(f"No 2D flow areas found in {hdf_path}")

    # Use the first area for primary outputs; log if multiple areas present
    if len(area_names) > 1:
        logger.warning(
            f"Multiple 2D areas found: {area_names}. "
            f"Exporting primary area '{area_names[0]}' only."
        )
    area_name = area_names[0]

    outputs: dict[str, Path] = {}

    # ── Max depth raster ──────────────────────────────────────────────────────
    logger.info(f"Exporting max depth raster for area '{area_name}'")
    xy, max_depth = extract_max_depth(hdf_path, area_name)
    depth_path = output_dir / "depth_grid.tif"
    cells_to_raster(xy, max_depth, crs, resolution_m, depth_path)
    outputs["depth_grid"] = depth_path

    # ── Max WSE raster ────────────────────────────────────────────────────────
    logger.info(f"Exporting max WSE raster for area '{area_name}'")
    _, max_wse = extract_max_wse(hdf_path, area_name)
    wse_path = output_dir / "wse_grid.tif"
    cells_to_raster(xy, max_wse, crs, resolution_m, wse_path)
    outputs["wse_grid"] = wse_path

    # ── Flood extent polygon ──────────────────────────────────────────────────
    logger.info(f"Exporting flood extent polygon for area '{area_name}'")
    flood_gdf = extract_flood_extent(hdf_path, area_name)

    gpkg_path = output_dir / "flood_extent.gpkg"
    flood_gdf.to_file(str(gpkg_path), driver="GPKG")
    outputs["flood_extent_gpkg"] = gpkg_path

    shp_path = output_dir / "flood_extent.shp"
    flood_gdf.to_file(str(shp_path), driver="ESRI Shapefile")
    outputs["flood_extent_shp"] = shp_path

    logger.info(f"Results export complete → {output_dir}")
    for name, path in outputs.items():
        logger.info(f"  {name}: {path}")

    # Optional R2 upload
    if r2_config is not None:
        try:
            from pipeline.storage import upload_results_dir
            run_name = output_dir.name
            r2_urls = upload_results_dir(output_dir, run_name, r2_config)
            logger.info(f"Uploaded {len(r2_urls)} result files to R2")
        except Exception as e:
            logger.warning(f"R2 upload failed (results still saved locally): {e}")

    return outputs


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_cli():
    try:
        import click
    except ImportError:
        return None

    @click.command()
    @click.argument("hdf_path", type=click.Path(exists=True))
    @click.argument("output_dir", type=click.Path())
    @click.option("--resolution", default=3.0, show_default=True,
                  help="Grid resolution in meters.")
    def cli(hdf_path, output_dir, resolution):
        """Export HEC-RAS 2D results to GeoTIFF and GeoPackage."""
        outputs = export_results(Path(hdf_path), Path(output_dir), resolution_m=resolution)
        for name, path in outputs.items():
            click.echo(f"{name}: {path}")

    return cli


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli = _build_cli()
    if cli:
        cli()
