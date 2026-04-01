"""
results.py — HEC-RAS 2D results extraction and GIS export

Reads HEC-RAS 6.x and 2025 unsteady output HDF5 files via h5py, extracts maximum
depth, water-surface elevation, and velocity fields from 2D flow area results,
interpolates irregular cell-center data onto regular grids, and exports as
Cloud-Optimized GeoTIFFs and GeoPackage/Shapefile flood extent polygons.

All output is in EPSG:5070 (NAD83 Albers Equal Area, meters) — consistent with
the rest of the RAS Agent pipeline.

HEC-RAS 6.x HDF5 result paths:
  /Geometry/2D Flow Areas/<area>/Cells Center Coordinate          (N,2)  float64
  /Geometry/2D Flow Areas/<area>/FacePoints Coordinate            (P,2)  float64
  /Geometry/2D Flow Areas/<area>/Faces FacePoint Indexes          (F,2)  int32
  /Geometry/2D Flow Areas/<area>/Cells Face and Orientation        (N,*)  int32
  /Results/Unsteady/Output/Output Blocks/Base Output/
      Unsteady Time Series/2D Flow Areas/<area>/Depth             (T,N)  float32
  /Results/Unsteady/Output/Output Blocks/Base Output/
      Unsteady Time Series/2D Flow Areas/<area>/Water Surface     (T,N)  float32
  /Results/Unsteady/Output/Output Blocks/Base Output/
      Unsteady Time Series/2D Flow Areas/<area>/Velocity         (T,N)  float32
  /Results/Unsteady/Output/Output Blocks/Base Output/
      Unsteady Time Series/2D Flow Areas/<area>/Face Velocity     (T,F)  float32

HEC-RAS 2025 HDF5 result paths (schema change):
  /Geometry/2D Flow Areas/<area>/Cell Coordinates                 (N,2)  float64
  /Results/Output Blocks/Base Output/2D Flow Areas/<area>/Depth  (T,N)  float32
  /Results/Output Blocks/Base Output/2D Flow Areas/<area>/Water Surface (T,N) float32
  /Results/Output Blocks/Base Output/2D Flow Areas/<area>/Velocity (T,N) float32
  /Results/Output Blocks/Base Output/2D Flow Areas/<area>/Face Velocity (T,F) float32

# HDF path patterns and dataclass design inspired by rivia (github.com/gyanz/rivia, Apache 2.0)

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import h5py
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from scipy.interpolate import griddata
from scipy.spatial import cKDTree
from shapely.geometry import MultiPoint
from shapely.ops import unary_union
import geopandas as gpd

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

TARGET_CRS = CRS.from_epsg(5070)

# ── HDF path templates — HEC-RAS 6.x ─────────────────────────────────────────

_GEOM_BASE = "Geometry/2D Flow Areas/{area}"
_CELL_CENTERS = _GEOM_BASE + "/Cells Center Coordinate"
_FACE_POINTS_COORD = _GEOM_BASE + "/FacePoints Coordinate"
_FACE_POINT_INDEXES = _GEOM_BASE + "/Faces FacePoint Indexes"
_CELL_FACE_INFO = _GEOM_BASE + "/Cells Face and Orientation"

_RESULTS_BASE = (
    "Results/Unsteady/Output/Output Blocks/Base Output/"
    "Unsteady Time Series/2D Flow Areas/{area}"
)
_DEPTH_PATH = _RESULTS_BASE + "/Depth"
_WSE_PATH = _RESULTS_BASE + "/Water Surface"
_VEL_PATH = _RESULTS_BASE + "/Velocity"
_FACE_VEL_PATH = _RESULTS_BASE + "/Face Velocity"

# ── HDF path templates — HEC-RAS 2025 ────────────────────────────────────────

_CELL_CENTERS_2025 = _GEOM_BASE + "/Cell Coordinates"
_RESULTS_BASE_2025 = "Results/Output Blocks/Base Output/2D Flow Areas/{area}"
_DEPTH_PATH_2025 = _RESULTS_BASE_2025 + "/Depth"
_WSE_PATH_2025 = _RESULTS_BASE_2025 + "/Water Surface"
_VEL_PATH_2025 = _RESULTS_BASE_2025 + "/Velocity"
_FACE_VEL_PATH_2025 = _RESULTS_BASE_2025 + "/Face Velocity"

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


# ── Typed Result Dataclasses ──────────────────────────────────────────────────

@dataclass
class FlowAreaGeometry:
    """Geometry of one 2D flow area from a HEC-RAS HDF file.

    Attributes:
        name: 2D flow area name (e.g. 'Perimeter 1').
        cell_centers: (N, 2) array of cell-center x/y coordinates in the project CRS.
        face_points: (P, 2) array of face-point x/y coordinates, or None if not present.
        face_point_indexes: (F, 2) int32 array mapping each face to its two face-point
            indexes, or None if not present.
        cell_face_info: Raw cell-to-face connectivity array, or None if not present.
    """
    name: str
    cell_centers: np.ndarray
    face_points: Optional[np.ndarray]
    face_point_indexes: Optional[np.ndarray]
    cell_face_info: Optional[np.ndarray]


@dataclass
class FlowAreaResults:
    """Maximum-value results for one 2D flow area from a HEC-RAS HDF file.

    Attributes:
        name: 2D flow area name.
        geometry: Associated FlowAreaGeometry (cell centers + face geometry).
        max_depth: (N,) float32 array of per-cell maximum simulated depth (m).
        max_wse: (N,) float32 array of per-cell maximum water-surface elevation (m).
        max_velocity: (N,) float32 array of per-cell maximum velocity (m/s), or None
            if velocity data is not present in the HDF file.
    """
    name: str
    geometry: FlowAreaGeometry
    max_depth: np.ndarray
    max_wse: np.ndarray
    max_velocity: Optional[np.ndarray]


# ── Version Detection ─────────────────────────────────────────────────────────

def detect_ras_version(hdf_path: Union[str, Path]) -> str:
    """Detect the HEC-RAS HDF5 schema version from an output file.

    Checks for RAS 2025's flattened Results path vs. the traditional 6.x nested
    path.  Returns "2025" or "6.x".

    Args:
        hdf_path: Path to a HEC-RAS plan or results HDF file.

    Returns:
        "2025" if the file follows the RAS 2025 schema, otherwise "6.x".
    """
    hdf_path = Path(hdf_path)
    with h5py.File(str(hdf_path), "r") as hf:
        # RAS 2025 uses Results/Output Blocks/ (no "Unsteady" prefix)
        if hf.get("Results/Output Blocks") is not None:
            return "2025"
    return "6.x"


def _results_base(ras_version: str, area_name: str) -> str:
    """Return the HDF results group path for a given RAS version and area."""
    if ras_version == "2025":
        return _RESULTS_BASE_2025.format(area=area_name)
    return _RESULTS_BASE.format(area=area_name)


def _cell_centers_path(hf: h5py.File, area_name: str) -> str:
    """Return the correct HDF path for cell centers, trying both schemas."""
    path_6x = _CELL_CENTERS.format(area=area_name)
    if hf.get(path_6x) is not None:
        return path_6x
    return _CELL_CENTERS_2025.format(area=area_name)


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

    Tries HEC-RAS 6.x path first, falls back to 2025 path.

    Args:
        hf:        Open h5py File object.
        area_name: 2D flow area name.

    Returns:
        Array of shape (N, 2) with (x, y) coordinates in the project CRS.
    """
    path = _cell_centers_path(hf, area_name)
    ds = hf.get(path)
    if ds is None:
        raise KeyError(f"Cell centers not found at HDF path: {path}")
    return np.array(ds, dtype=np.float64)


def _load_face_geometry(
    hf: h5py.File,
    area_name: str,
) -> tuple[Optional[np.ndarray], Optional[np.ndarray], Optional[np.ndarray]]:
    """
    Load face-point geometry for a 2D flow area (optional datasets).

    Args:
        hf:        Open h5py File object.
        area_name: 2D flow area name.

    Returns:
        Tuple of (face_points, face_point_indexes, cell_face_info), each
        None if the corresponding dataset is absent from the HDF file.
    """
    face_points = None
    face_point_indexes = None
    cell_face_info = None

    fp_ds = hf.get(_FACE_POINTS_COORD.format(area=area_name))
    if fp_ds is not None:
        face_points = np.array(fp_ds, dtype=np.float64)

    idx_ds = hf.get(_FACE_POINT_INDEXES.format(area=area_name))
    if idx_ds is not None:
        face_point_indexes = np.array(idx_ds, dtype=np.int32)

    cfi_ds = hf.get(_CELL_FACE_INFO.format(area=area_name))
    if cfi_ds is not None:
        cell_face_info = np.array(cfi_ds)

    return face_points, face_point_indexes, cell_face_info


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
    ras_ver = detect_ras_version(hdf_path)
    depth_path = (_DEPTH_PATH_2025 if ras_ver == "2025" else _DEPTH_PATH).format(area=area_name)

    with h5py.File(str(hdf_path), "r") as hf:
        xy = _load_cell_centers(hf, area_name)
        ds = hf.get(depth_path)
        if ds is None:
            raise KeyError(f"Depth dataset not found at HDF path: {depth_path}")
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
    ras_ver = detect_ras_version(hdf_path)
    wse_path = (_WSE_PATH_2025 if ras_ver == "2025" else _WSE_PATH).format(area=area_name)

    with h5py.File(str(hdf_path), "r") as hf:
        xy = _load_cell_centers(hf, area_name)
        ds = hf.get(wse_path)
        if ds is None:
            raise KeyError(f"Water Surface dataset not found at HDF path: {wse_path}")
        wse = np.array(ds, dtype=np.float32)   # shape (T, N)

    max_wse = np.max(wse, axis=0)   # (N,)
    logger.debug(
        f"extract_max_wse({area_name}): "
        f"{xy.shape[0]} cells, peak max_wse={max_wse.max():.2f} m"
    )
    return xy, max_wse


def extract_max_velocity(
    hdf_path: Path,
    area_name: str,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Extract the maximum cell-center velocity magnitude for each 2D mesh cell.

    Reads the cell-center Velocity dataset (T, N) and takes the per-cell maximum
    across all timesteps.  This mirrors the pattern used by extract_max_depth()
    and extract_max_wse().

    Args:
        hdf_path:  Path to the HEC-RAS output HDF file.
        area_name: Name of the 2D flow area to extract.

    Returns:
        Tuple of:
          - cell_centers_xy: (N, 2) float64 array of cell-center coordinates.
          - max_velocity:    (N,)   float32 array of maximum velocity per cell (m/s).

    Raises:
        KeyError: If velocity data is not found for the specified area.
    """
    ras_ver = detect_ras_version(hdf_path)
    vel_path = (_VEL_PATH_2025 if ras_ver == "2025" else _VEL_PATH).format(area=area_name)

    with h5py.File(str(hdf_path), "r") as hf:
        xy = _load_cell_centers(hf, area_name)
        ds = hf.get(vel_path)
        if ds is None:
            raise KeyError(f"Velocity dataset not found at HDF path: {vel_path}")
        velocities = np.array(ds, dtype=np.float32)

    # Handle both (T, N) time series and pre-computed (N,) max arrays
    if velocities.ndim == 2:
        max_velocity = np.max(velocities, axis=0)
    else:
        max_velocity = velocities

    logger.debug(
        f"extract_max_velocity({area_name}): "
        f"{xy.shape[0]} cells, peak max_velocity={max_velocity.max():.3f} m/s"
    )
    return xy, max_velocity


def extract_flow_area_results(
    hdf_path: Path,
    area_name: str,
) -> FlowAreaResults:
    """
    Extract all results for one 2D flow area into a typed FlowAreaResults dataclass.

    Reads geometry (cell centers + face points if present), maximum depth, maximum
    WSE, and maximum velocity (if available) in a single pass.  Provides a
    convenient alternative to calling extract_max_depth + extract_max_wse separately.

    Args:
        hdf_path:  Path to the HEC-RAS output HDF file.
        area_name: Name of the 2D flow area to extract.

    Returns:
        FlowAreaResults with all fields populated.  max_velocity is None if
        velocity data is absent from the HDF file.

    Raises:
        KeyError: If required datasets (depth or WSE) are not found.
    """
    ras_ver = detect_ras_version(hdf_path)

    depth_path = (_DEPTH_PATH_2025 if ras_ver == "2025" else _DEPTH_PATH).format(area=area_name)
    wse_path = (_WSE_PATH_2025 if ras_ver == "2025" else _WSE_PATH).format(area=area_name)
    vel_path = (_VEL_PATH_2025 if ras_ver == "2025" else _VEL_PATH).format(area=area_name)

    with h5py.File(str(hdf_path), "r") as hf:
        xy = _load_cell_centers(hf, area_name)
        face_pts, face_pt_idxs, cell_face_info = _load_face_geometry(hf, area_name)

        depth_ds = hf.get(depth_path)
        if depth_ds is None:
            raise KeyError(f"Depth dataset not found at HDF path: {depth_path}")
        depths = np.array(depth_ds, dtype=np.float32)

        wse_ds = hf.get(wse_path)
        if wse_ds is None:
            raise KeyError(f"Water Surface dataset not found at HDF path: {wse_path}")
        wse = np.array(wse_ds, dtype=np.float32)

        vel_ds = hf.get(vel_path)
        velocities = np.array(vel_ds, dtype=np.float32) if vel_ds is not None else None

    max_depth = np.max(depths, axis=0) if depths.ndim == 2 else depths
    max_wse = np.max(wse, axis=0) if wse.ndim == 2 else wse

    if velocities is not None:
        max_velocity = np.max(velocities, axis=0) if velocities.ndim == 2 else velocities
    else:
        max_velocity = None

    geometry = FlowAreaGeometry(
        name=area_name,
        cell_centers=xy,
        face_points=face_pts,
        face_point_indexes=face_pt_idxs,
        cell_face_info=cell_face_info,
    )

    logger.debug(
        f"extract_flow_area_results({area_name}): {xy.shape[0]} cells, "
        f"peak depth={max_depth.max():.2f} m, "
        f"{'velocity present' if max_velocity is not None else 'no velocity'}"
    )
    return FlowAreaResults(
        name=area_name,
        geometry=geometry,
        max_depth=max_depth,
        max_wse=max_wse,
        max_velocity=max_velocity,
    )


# ── Raster Export ─────────────────────────────────────────────────────────────

def _idw_interpolate(
    points: np.ndarray,
    values: np.ndarray,
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    k: int = 8,
    power: float = 2.0,
) -> np.ndarray:
    """
    Inverse distance weighting (IDW) interpolation onto a regular grid.

    More robust than linear griddata near mesh boundaries — avoids NaN halos
    outside the convex hull and produces smooth transitions matching RASMapper's
    rendering approach.

    Args:
        points:  (N, 2) source x/y coordinates.
        values:  (N,) scalar values at each source point.
        grid_x:  (rows, cols) meshgrid x coordinates.
        grid_y:  (rows, cols) meshgrid y coordinates.
        k:       Number of nearest neighbors to use (default 8).
        power:   Distance-weighting exponent (default 2.0, classic IDW).

    Returns:
        (rows, cols) float32 array of interpolated values.
    """
    tree = cKDTree(points)
    k = min(k, len(points))
    query_pts = np.column_stack([grid_x.ravel(), grid_y.ravel()])
    dists, idxs = tree.query(query_pts, k=k)

    # Guard against exact-match points (zero distance)
    dists = np.where(dists == 0.0, 1e-12, dists)
    weights = 1.0 / np.power(dists, power)
    weights /= weights.sum(axis=1, keepdims=True)

    interpolated = np.sum(weights * values[idxs], axis=1)
    return interpolated.reshape(grid_x.shape).astype(np.float32)


def cells_to_raster(
    cell_centers_xy: np.ndarray,
    values: np.ndarray,
    crs: CRS,
    resolution_m: float = 3.0,
    output_path: Optional[Path] = None,
    method: str = "linear",
) -> Path:
    """
    Interpolate irregular point data onto a regular grid and write a
    Cloud-Optimized GeoTIFF (COG).

    Args:
        cell_centers_xy: (N, 2) array of (x, y) coordinates in the output CRS.
            For method='face_weighted', pass face-point coordinates for improved
            boundary accuracy.
        values:          (N,) array of scalar values at each input point.
        crs:             Coordinate reference system for the output raster.
        resolution_m:    Grid cell size in CRS units (meters for EPSG:5070).
        output_path:     Write the COG to this path.  A temporary file is used
                         if None; the caller is responsible for cleaning it up.
        method:          Interpolation method — one of:
                         - "linear" (default): scipy griddata linear triangulation.
                         - "nearest": scipy griddata nearest-neighbor.
                         - "face_weighted": IDW interpolation; use with face-point
                           coordinates for RASMapper-equivalent boundary rendering.

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
    grid_x_1d = np.linspace(x_min, x_max, cols)
    grid_y_1d = np.linspace(y_max, y_min, rows)   # top-to-bottom (raster convention)
    gx, gy = np.meshgrid(grid_x_1d, grid_y_1d)

    if method == "face_weighted":
        grid_vals = _idw_interpolate(cell_centers_xy, values.astype(np.float64), gx, gy)
        grid_vals = np.where(np.isnan(grid_vals), NODATA, grid_vals)
    else:
        # "linear" or "nearest" via scipy griddata
        scipy_method = method if method in ("linear", "nearest") else "linear"
        grid_vals = griddata(
            points=cell_centers_xy,
            values=values.astype(np.float64),
            xi=(gx, gy),
            method=scipy_method,
            fill_value=NODATA,
        ).astype(np.float32)
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

    logger.info(f"Wrote raster ({rows}×{cols} px, {resolution_m}m, method={method}): {output_path}")
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

def _export_single_area(
    hdf_path: Path,
    area_name: str,
    area_out: Path,
    crs: CRS,
    resolution_m: float,
    key_prefix: str,
) -> dict[str, Path]:
    """Export all rasters and vectors for one 2D flow area.

    Args:
        hdf_path:     HEC-RAS output HDF file.
        area_name:    Name of the 2D flow area.
        area_out:     Output directory for this area's files.
        crs:          Output CRS.
        resolution_m: Raster resolution in meters.
        key_prefix:   Prefix for return-dict keys (empty for single-area).

    Returns:
        Dict of output-name → Path for this area.
    """
    area_out.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}

    # Max depth raster
    logger.info(f"Exporting max depth raster for area '{area_name}'")
    xy, max_depth = extract_max_depth(hdf_path, area_name)
    depth_path = area_out / "depth_grid.tif"
    cells_to_raster(xy, max_depth, crs, resolution_m, depth_path)
    outputs[f"{key_prefix}depth_grid"] = depth_path

    # Max WSE raster
    logger.info(f"Exporting max WSE raster for area '{area_name}'")
    _, max_wse = extract_max_wse(hdf_path, area_name)
    wse_path = area_out / "wse_grid.tif"
    cells_to_raster(xy, max_wse, crs, resolution_m, wse_path)
    outputs[f"{key_prefix}wse_grid"] = wse_path

    # Max velocity raster (optional — skip gracefully if not in HDF)
    try:
        _, max_vel = extract_max_velocity(hdf_path, area_name)
        vel_path = area_out / "velocity_grid.tif"
        cells_to_raster(xy, max_vel, crs, resolution_m, vel_path)
        outputs[f"{key_prefix}velocity_grid"] = vel_path
        logger.info(f"Exporting max velocity raster for area '{area_name}'")
    except KeyError:
        logger.debug(f"No velocity data for area '{area_name}' — skipping velocity_grid.tif")

    # Flood extent polygon
    logger.info(f"Exporting flood extent polygon for area '{area_name}'")
    flood_gdf = extract_flood_extent(hdf_path, area_name)

    gpkg_path = area_out / "flood_extent.gpkg"
    flood_gdf.to_file(str(gpkg_path), driver="GPKG")
    outputs[f"{key_prefix}flood_extent_gpkg"] = gpkg_path

    shp_path = area_out / "flood_extent.shp"
    flood_gdf.to_file(str(shp_path), driver="ESRI Shapefile")
    outputs[f"{key_prefix}flood_extent_shp"] = shp_path

    return outputs


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
      1. Extract maximum depth  → depth_grid.tif    (Cloud-Optimized GeoTIFF)
      2. Extract maximum WSE    → wse_grid.tif      (Cloud-Optimized GeoTIFF)
      3. Extract maximum velocity → velocity_grid.tif (if velocity data present)
      4. Extract flood extent   → flood_extent.gpkg + flood_extent.shp

    Single area: all files are written directly to output_dir.
    Multiple areas: each area gets its own subdirectory (output_dir/{area_name}/).

    Args:
        hdf_path:     Path to the HEC-RAS output HDF file.
        output_dir:   Directory to write all output files.
        crs:          Output CRS (defaults to EPSG:5070).
        resolution_m: Raster grid resolution in CRS units (default 3.0 m).
        r2_config:    Optional R2Config for Cloudflare R2 upload.

    Returns:
        Dict mapping output name to output Path.  Single-area keys:
          'depth_grid', 'wse_grid', 'velocity_grid' (if present),
          'flood_extent_gpkg', 'flood_extent_shp'.
        Multi-area keys are prefixed: '{area_name}/depth_grid', etc.

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

    multi = len(area_names) > 1
    if multi:
        logger.info(f"Multiple 2D areas found: {area_names} — exporting each to a subdirectory.")

    outputs: dict[str, Path] = {}

    for area_name in area_names:
        area_out = output_dir / area_name if multi else output_dir
        key_prefix = f"{area_name}/" if multi else ""
        area_outputs = _export_single_area(
            hdf_path, area_name, area_out, crs, resolution_m, key_prefix
        )
        outputs.update(area_outputs)

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
    @click.option("--method", default="linear", show_default=True,
                  type=click.Choice(["linear", "nearest", "face_weighted"]),
                  help="Raster interpolation method.")
    def cli(hdf_path, output_dir, resolution, method):
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
