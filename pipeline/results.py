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
import platform
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

import h5py
import numpy as np
import pandas as pd
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

_TIME_SERIES_BASE = (
    "Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series"
)
_TIME_SERIES_BASE_2025 = "Results/Output Blocks/Base Output"

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


def _time_series_base(hf: h5py.File, ras_version: str) -> str:
    """Return the HDF group that contains result timestamps."""
    candidates = []
    if ras_version == "2025":
        candidates.extend(
            [
                _TIME_SERIES_BASE_2025,
                _TIME_SERIES_BASE_2025 + "/Unsteady Time Series",
            ]
        )
    candidates.append(_TIME_SERIES_BASE)

    for path in candidates:
        if hf.get(path) is not None:
            return path
    raise KeyError("Unsteady time-series timestamp group not found in HDF")


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


def _decode_hdf_strings(values: np.ndarray) -> list[str]:
    """Decode HDF byte/string arrays into stripped Python strings."""
    decoded: list[str] = []
    for value in np.asarray(values):
        if isinstance(value, bytes):
            decoded.append(value.decode("utf-8", errors="ignore").strip())
        else:
            decoded.append(str(value).strip())
    return decoded


def _parse_time_stamps(values: np.ndarray) -> pd.DatetimeIndex:
    """Parse common HEC-RAS timestamp strings as UTC datetimes."""
    strings = _decode_hdf_strings(values)
    for fmt in ("%d%b%Y %H:%M:%S:%f", "%d%b%Y %H:%M:%S"):
        parsed = pd.to_datetime(strings, format=fmt, utc=True, errors="coerce")
        if not parsed.isna().any():
            return pd.DatetimeIndex(parsed, name="datetime_utc")

    parsed = pd.to_datetime(strings, utc=True, errors="coerce")
    if parsed.isna().any():
        raise ValueError("Could not parse HEC-RAS time stamps")
    return pd.DatetimeIndex(parsed, name="datetime_utc")


def _load_time_index(hf: h5py.File, ras_version: str) -> pd.DatetimeIndex:
    """Load the model output time index from the HDF result block."""
    base = _time_series_base(hf, ras_version)
    for name in ("Time Date Stamp (ms)", "Time Date Stamp"):
        ds = hf.get(f"{base}/{name}")
        if ds is not None:
            return _parse_time_stamps(ds[:])
    raise KeyError(f"No time-date stamp dataset found under {base}")


def _project_lonlat_to_xy(lon: float, lat: float) -> tuple[float, float]:
    """Project WGS84 longitude/latitude to the pipeline CRS used by Spring Creek."""
    try:
        from pyproj import Transformer
    except ImportError as exc:
        raise ImportError("extract_point_timeseries requires pyproj") from exc

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    x, y = transformer.transform(lon, lat)
    return float(x), float(y)


def _extract_cell_dataset(
    hf: h5py.File,
    base_path: str,
    names: tuple[str, ...],
    cell_index: int,
    n_times: int,
) -> Optional[np.ndarray]:
    """Extract one cell column from the first available cell time-series dataset."""
    for name in names:
        ds = hf.get(f"{base_path}/{name}")
        if ds is None:
            continue
        values = np.asarray(ds)
        if values.ndim == 2:
            if cell_index >= values.shape[1]:
                raise IndexError(f"Cell index {cell_index} is outside dataset {name}")
            return values[:, cell_index].astype(float)
        if values.ndim == 1 and values.shape[0] == n_times:
            return values.astype(float)
        if values.ndim == 1 and cell_index < values.shape[0]:
            return np.full(n_times, float(values[cell_index]))
    return None


def _cell_face_ids(hf: h5py.File, area_name: str, cell_index: int) -> Optional[np.ndarray]:
    """Return face IDs associated with one 2D cell."""
    info_ds = hf.get(f"{_GEOM_BASE.format(area=area_name)}/Cells Face and Orientation Info")
    values_ds = hf.get(f"{_GEOM_BASE.format(area=area_name)}/Cells Face and Orientation Values")
    if info_ds is None or values_ds is None:
        return None

    info = np.asarray(info_ds)
    if cell_index >= info.shape[0]:
        return None
    start, count = [int(v) for v in info[cell_index]]
    if count <= 0:
        return None
    values = np.asarray(values_ds[start:start + count])
    if values.ndim != 2 or values.shape[1] == 0:
        return None
    return values[:, 0].astype(int)


def _interp_face_areas(
    hf: h5py.File,
    area_name: str,
    face_ids: np.ndarray,
    water_surface: np.ndarray,
) -> Optional[np.ndarray]:
    """Interpolate face hydraulic areas at each supplied water surface stage."""
    info_ds = hf.get(f"{_GEOM_BASE.format(area=area_name)}/Faces Area Elevation Info")
    values_ds = hf.get(f"{_GEOM_BASE.format(area=area_name)}/Faces Area Elevation Values")
    if info_ds is None or values_ds is None:
        return None

    info = np.asarray(info_ds)
    values = np.asarray(values_ds)
    areas = np.zeros((len(water_surface), len(face_ids)), dtype=float)

    for j, face_id in enumerate(face_ids):
        if face_id < 0 or face_id >= info.shape[0]:
            areas[:, j] = np.nan
            continue
        start, count = [int(v) for v in info[face_id]]
        table = values[start:start + count]
        if table.size == 0:
            areas[:, j] = np.nan
            continue
        elevations = table[:, 0].astype(float)
        face_area = table[:, 1].astype(float)
        areas[:, j] = np.interp(
            water_surface,
            elevations,
            face_area,
            left=0.0,
            right=float(face_area[-1]),
        )
    return areas


def _estimate_cell_flow_from_faces(
    hf: h5py.File,
    area_name: str,
    base_path: str,
    cell_index: int,
    water_surface: np.ndarray,
) -> Optional[np.ndarray]:
    """
    Estimate point flow from the dominant adjacent face flux.

    HEC-RAS 2D cells do not store a canonical cell discharge. When a direct
    cell-flow dataset is absent, use the largest absolute flux through any face
    touching the nearest cell as a local point-flow proxy.
    """
    face_ids = _cell_face_ids(hf, area_name, cell_index)
    if face_ids is None or len(face_ids) == 0:
        return None

    face_vel_ds = hf.get(f"{base_path}/Face Velocity")
    if face_vel_ds is None:
        return None
    face_vel = np.asarray(face_vel_ds[:, face_ids], dtype=float)
    face_area = _interp_face_areas(hf, area_name, face_ids, water_surface)
    if face_area is None:
        return None

    face_flow = face_vel * face_area
    if face_flow.size == 0 or np.isnan(face_flow).all():
        return None
    return np.nanmax(np.abs(face_flow), axis=1)


def extract_point_timeseries(
    hdf_path: Union[str, Path],
    area_name: str,
    lon: float,
    lat: float,
) -> pd.DataFrame:
    """
    Extract modeled stage and flow at the mesh cell nearest a gauge location.

    Args:
        hdf_path: HEC-RAS plan/results HDF path.
        area_name: 2D flow area name.
        lon: Gauge longitude in WGS84.
        lat: Gauge latitude in WGS84.

    Returns:
        Datetime-indexed DataFrame with observed-data-compatible columns:
        ``flow_cfs`` and ``stage_ft``. The frame also includes
        ``water_surface_ft`` and ``depth_ft`` when those can be derived. HEC-RAS
        2D output does not define a canonical per-cell discharge; ``flow_cfs``
        is read from a direct cell-flow dataset when present, otherwise it is
        estimated from the dominant adjacent face flux.
    """
    hdf_path = Path(hdf_path)
    ras_ver = detect_ras_version(hdf_path)
    base_path = _results_base(ras_ver, area_name)
    x, y = _project_lonlat_to_xy(lon, lat)

    with h5py.File(str(hdf_path), "r") as hf:
        time_index = _load_time_index(hf, ras_ver)
        centers = _load_cell_centers(hf, area_name)
        distance_m, cell_index = cKDTree(centers).query([x, y])
        cell_index = int(cell_index)

        n_times = len(time_index)
        water_surface = _extract_cell_dataset(
            hf,
            base_path,
            ("Water Surface", "Water Surface Elevation"),
            cell_index,
            n_times,
        )
        if water_surface is None:
            raise KeyError(f"Water Surface dataset not found at HDF path: {base_path}")

        depth = _extract_cell_dataset(
            hf,
            base_path,
            ("Depth", "Cell Depth"),
            cell_index,
            n_times,
        )
        if depth is None:
            min_elev_ds = hf.get(f"{_GEOM_BASE.format(area=area_name)}/Cells Minimum Elevation")
            if min_elev_ds is not None and cell_index < min_elev_ds.shape[0]:
                depth = water_surface - float(min_elev_ds[cell_index])
            else:
                depth = np.full(n_times, np.nan)

        flow = _extract_cell_dataset(
            hf,
            base_path,
            ("Flow", "Cell Flow", "Cells Flow", "Flow CFS"),
            cell_index,
            n_times,
        )
        flow_source = "cell_dataset"
        if flow is None:
            flow = _estimate_cell_flow_from_faces(
                hf,
                area_name,
                base_path,
                cell_index,
                water_surface,
            )
            flow_source = "dominant_adjacent_face_flux"
        if flow is None:
            flow = np.full(n_times, np.nan)
            flow_source = "unavailable"

    frame = pd.DataFrame(
        {
            "flow_cfs": flow,
            "stage_ft": water_surface,
            "water_surface_ft": water_surface,
            "depth_ft": depth,
        },
        index=time_index,
    )
    frame.index.name = "datetime_utc"
    frame.attrs.update(
        {
            "area_name": area_name,
            "cell_index": cell_index,
            "nearest_cell_distance_m": float(distance_m),
            "gauge_lon": float(lon),
            "gauge_lat": float(lat),
            "gauge_x_5070": x,
            "gauge_y_5070": y,
            "flow_source": flow_source,
        }
    )
    return frame


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
    min_depth_ft: Optional[float] = None,
    ras_object=None,
) -> dict[str, Path]:
    """
    Run the full results export pipeline for all 2D areas in an HDF file.

    For each 2D flow area:
      1. Extract maximum depth  → depth_grid.tif    (Cloud-Optimized GeoTIFF)
      2. Extract maximum WSE    → wse_grid.tif      (Cloud-Optimized GeoTIFF)
      3. Extract maximum velocity → velocity_grid.tif (if velocity data present)
      4. Extract flood extent   → flood_extent.gpkg + flood_extent.shp

    When ``min_depth_ft`` is set, also generates terrain-aligned filtered
    depth and WSE rasters via RASMapper (see ``export_filtered_rasters``).

    Single area: all files are written directly to output_dir.
    Multiple areas: each area gets its own subdirectory (output_dir/{area_name}/).

    Args:
        hdf_path:      Path to the HEC-RAS output HDF file.
        output_dir:    Directory to write all output files.
        crs:           Output CRS (defaults to EPSG:5070).
        resolution_m:  Raster grid resolution in CRS units (default 3.0 m).
        r2_config:     Optional R2Config for Cloudflare R2 upload.
        min_depth_ft:  When set, produce filtered_depth.tif and filtered_wse.tif
                       using RASMapper-aligned rasters with this threshold (ft).
        ras_object:    Optional initialized ``RasPrj`` instance for filtered
                       raster export (only used when ``min_depth_ft`` is set).

    Returns:
        Dict mapping output name to output Path.  Single-area keys:
          'depth_grid', 'wse_grid', 'velocity_grid' (if present),
          'flood_extent_gpkg', 'flood_extent_shp'.
        Multi-area keys are prefixed: '{area_name}/depth_grid', etc.
        When ``min_depth_ft`` is set, also includes 'filtered_depth' and
        'filtered_wse'.

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

    # Optional filtered rasters via RASMapper
    if min_depth_ft is not None:
        try:
            filtered_dir = output_dir / "filtered"
            filtered = export_filtered_rasters(
                hdf_path=hdf_path,
                output_dir=filtered_dir,
                min_depth_ft=min_depth_ft,
                target_crs=crs,
                ras_object=ras_object,
            )
            outputs.update(filtered)
        except Exception as exc:
            logger.warning(f"Filtered raster export failed (non-fatal): {exc}")

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


# ── RASMapper Headless Export (RasMap) ────────────────────────────────────────

PlanNumber = Union[str, int, float]


def _normalize_plan_number(plan_number: PlanNumber) -> str:
    """Return a RAS plan number string suitable for ``p##`` result files."""
    if isinstance(plan_number, float):
        if not plan_number.is_integer():
            raise ValueError(f"Plan number must be an integer-like value: {plan_number}")
        plan_text = str(int(plan_number))
    else:
        plan_text = str(plan_number).strip()

    if not plan_text:
        raise ValueError("Plan number cannot be blank")

    if plan_text.lower().startswith("p") and plan_text[1:].isdigit():
        plan_text = plan_text[1:]

    if plan_text.isdigit() and len(plan_text) == 1:
        return plan_text.zfill(2)
    return plan_text


def _coerce_plan_numbers(
    plan_numbers: Union[PlanNumber, Sequence[PlanNumber]]
) -> list[str]:
    if isinstance(plan_numbers, (str, int, float)):
        raw_plans = [plan_numbers]
    else:
        raw_plans = list(plan_numbers)

    if not raw_plans:
        raise ValueError("At least one plan number is required")
    return [_normalize_plan_number(plan) for plan in raw_plans]


def _find_plan_hdf(project_dir: Path, plan_number: str) -> Path:
    """Find the result HDF for a plan using common HEC-RAS p##/p### names."""
    candidates: list[Path] = []
    search_numbers = [plan_number]
    if plan_number.isdigit():
        for padded in (plan_number.zfill(2), plan_number.zfill(3)):
            if padded not in search_numbers:
                search_numbers.append(padded)

    for plan_text in search_numbers:
        candidates.extend(sorted(project_dir.glob(f"*.p{plan_text}.hdf")))

    unique = sorted({path.resolve() for path in candidates})
    if not unique:
        raise FileNotFoundError(
            f"No HEC-RAS result HDF found for plan p{plan_number} in {project_dir}"
        )
    return unique[0]


def _unique_existing_paths(paths: Sequence[Union[str, Path]]) -> list[Path]:
    existing: list[Path] = []
    seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            existing.append(path)
    return sorted(existing, key=lambda p: str(p).lower())


def _collect_rasmapper_rasters(ras_map, plan_number: str, ras_object) -> list[Path]:
    """Collect VRT and GeoTIFF outputs from a RASMapper plan results folder."""
    collected: list[Union[str, Path]] = []

    for variable_name in ("Depth", "WSE", "Velocity"):
        try:
            collected.append(
                ras_map.get_results_raster(
                    plan_number,
                    variable_name,
                    ras_object=ras_object,
                )
            )
        except Exception as exc:
            logger.debug(
                "RASMapper raster lookup skipped for plan %s variable %s: %s",
                plan_number,
                variable_name,
                exc,
            )

    results_folder = Path(
        ras_map.get_results_folder(plan_number, ras_object=ras_object)
    )
    for pattern in ("*.vrt", "*.tif", "*.tiff"):
        collected.extend(results_folder.rglob(pattern))

    return _unique_existing_paths(collected)


def _export_plan_via_python(project_dir: Path, plan_number: str) -> list[Path]:
    """Fallback to the local h5py/rasterio exporter for one plan."""
    hdf_path = _find_plan_hdf(project_dir, plan_number)
    output_dir = project_dir / "results" / f"p{plan_number}"
    outputs = export_results(hdf_path=hdf_path, output_dir=output_dir)
    return list(outputs.values())


def _export_plans_via_python(
    project_dir: Path,
    plan_numbers: Sequence[str],
) -> dict[str, list[Path]]:
    return {
        plan_number: _export_plan_via_python(project_dir, plan_number)
        for plan_number in plan_numbers
    }


def export_via_rasmapper(
    project_dir: Union[str, Path],
    plan_numbers: Union[PlanNumber, Sequence[PlanNumber]],
    timeout: int = 600,
) -> dict[str, list[Path]]:
    """Export result rasters with RASMapper on Windows, otherwise fall back to Python.

    On Windows this initializes the HEC-RAS project through ``ras-commander``,
    calls ``RasMap.store_all_maps()`` once per requested plan, and returns the
    generated ``.vrt``/GeoTIFF outputs from each plan's RASMapper results
    folder.  On Linux or when RASMapper/ras-commander is unavailable, it uses
    the existing ``export_results()`` HDF5/rasterio path.

    Args:
        project_dir: HEC-RAS project directory containing the ``.prj`` and
            plan result HDF files.
        plan_numbers: One plan number or a sequence of plan numbers.
        timeout: Per-plan timeout passed through to ``RasMap.store_all_maps``.

    Returns:
        Dict mapping normalized plan numbers (for example ``"01"``) to the
        generated VRT/GeoTIFF paths.
    """
    if timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout}")

    project_dir = Path(project_dir)
    plan_list = _coerce_plan_numbers(plan_numbers)

    if platform.system().lower() != "windows":
        logger.info("RASMapper export is unavailable on this platform; using Python exporter")
        return _export_plans_via_python(project_dir, plan_list)

    try:
        from ras_commander import RasMap, init_ras_project  # type: ignore[import]
    except Exception as exc:
        logger.warning(
            "ras-commander/RASMapper is unavailable; using Python exporter: %s",
            exc,
        )
        return _export_plans_via_python(project_dir, plan_list)

    try:
        try:
            ras_object = init_ras_project(project_dir, load_results_summary=False)
        except TypeError:
            ras_object = init_ras_project(project_dir)
    except Exception as exc:
        logger.warning(
            "Could not initialize RAS project for RASMapper export; "
            "using Python exporter: %s",
            exc,
        )
        return _export_plans_via_python(project_dir, plan_list)

    outputs: dict[str, list[Path]] = {}
    for plan_number in plan_list:
        try:
            result = RasMap.store_all_maps(
                plan_number=plan_number,
                ras_object=ras_object,
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning(
                "RASMapper export failed for plan p%s; using Python exporter: %s",
                plan_number,
                exc,
            )
            outputs[plan_number] = _export_plan_via_python(project_dir, plan_number)
            continue

        plan_result = {}
        if isinstance(result, dict):
            plan_result = result.get("plans", {}).get(plan_number, {})
        plan_failed = isinstance(plan_result, dict) and plan_result.get("success") is False
        if plan_failed:
            logger.warning(
                "RASMapper export did not complete for plan p%s; "
                "using Python exporter: %s",
                plan_number,
                plan_result.get("error", "unknown error"),
            )
            outputs[plan_number] = _export_plan_via_python(project_dir, plan_number)
            continue

        rasters = _collect_rasmapper_rasters(RasMap, plan_number, ras_object)
        if not rasters:
            logger.warning(
                "RASMapper export completed for plan p%s but produced no "
                "VRT/GeoTIFF outputs; using Python exporter",
                plan_number,
            )
            rasters = _export_plan_via_python(project_dir, plan_number)
        outputs[plan_number] = rasters

    return outputs


# ── Filtered Raster Export (RasProcess / RASMapper) ──────────────────────


def _parse_plan_from_hdf(hdf_path: Path) -> tuple[Path, str]:
    """Extract project directory and plan number from an HDF path.

    HDF files are named ``<ProjectName>.p<NN>.hdf``.
    """
    project_dir = hdf_path.parent
    stem = hdf_path.stem  # e.g. "SpringCreek.p01"
    parts = stem.rsplit(".p", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        raise ValueError(
            f"Cannot parse plan number from HDF filename: {hdf_path.name}"
        )
    return project_dir, parts[1]


def _write_filtered_cog(
    data: np.ndarray,
    profile: dict,
    output_path: Path,
    metadata: dict[str, str],
) -> Path:
    """Write a masked float32 array as a Cloud-Optimized GeoTIFF with overviews."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_profile = {
        "crs": profile["crs"],
        "transform": profile["transform"],
        "width": profile["width"],
        "height": profile["height"],
        "dtype": np.float32,
        "nodata": NODATA,
        "count": 1,
        **_COG_OPTIONS,
    }
    with rasterio.open(str(output_path), "w", **write_profile) as dst:
        dst.write(data.astype(np.float32), 1)
        safe_levels = [
            lv for lv in _OVERVIEW_LEVELS
            if dst.height // lv >= 1 and dst.width // lv >= 1
        ]
        if safe_levels:
            dst.build_overviews(safe_levels, rasterio.enums.Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")
        dst.update_tags(**metadata)
    return output_path


def export_filtered_rasters(
    hdf_path: Union[str, Path],
    output_dir: Union[str, Path],
    min_depth_ft: float = 0.5,
    target_crs: Optional[CRS] = None,
    ras_object=None,
) -> dict[str, Path]:
    """Export depth and WSE rasters filtered by a minimum depth threshold.

    Uses ``RasProcess.store_maps()`` to generate terrain-aligned rasters via
    RASMapper, then applies a single depth-based mask to both outputs.  Because
    RASMapper renders both depth and WSE from the same terrain grid, the
    rasters are pixel-aligned by construction — no independent interpolation.

    Args:
        hdf_path:     Path to the HEC-RAS output HDF file (``Project.p01.hdf``).
                      The project directory and plan number are derived from
                      this path.
        output_dir:   Directory for filtered output rasters.
        min_depth_ft: Minimum depth threshold in feet (default 0.5).  Cells
                      with depth below this become NODATA in both outputs.
        target_crs:   Optional CRS to reproject outputs to.  If None, outputs
                      retain the native CRS from RASMapper (terrain CRS).
        ras_object:   Optional initialized ``RasPrj`` instance.  If None, one
                      is created via ``init_ras_project()``.

    Returns:
        Dict with keys ``'filtered_depth'`` and ``'filtered_wse'`` mapping to
        output COG GeoTIFF paths.

    Raises:
        ValueError: If ``min_depth_ft`` is negative.
        RuntimeError: If RasProcess fails to produce the required rasters.
    """
    hdf_path = Path(hdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if min_depth_ft < 0:
        raise ValueError(f"min_depth_ft must be non-negative, got {min_depth_ft}")
    min_depth_m = min_depth_ft * 0.3048

    from ras_commander import RasProcess

    project_dir, plan_number = _parse_plan_from_hdf(hdf_path)

    if ras_object is None:
        from ras_commander import init_ras_project
        ras_object = init_ras_project(project_dir)

    # Generate terrain-aligned rasters via RASMapper
    staging_dir = tempfile.mkdtemp(prefix="ras_filtered_")
    try:
        map_results = RasProcess.store_maps(
            plan_number=plan_number,
            output_path=staging_dir,
            profile="Max",
            wse=True,
            depth=True,
            velocity=False,
            fix_georef=True,
            ras_object=ras_object,
        )

        depth_tifs = map_results.get("depth", [])
        wse_tifs = map_results.get("wse", [])
        if not depth_tifs or not wse_tifs:
            raise RuntimeError(
                "RasProcess.store_maps did not produce depth and/or WSE rasters"
            )

        # Read both rasters into memory while staging dir exists
        with rasterio.open(str(depth_tifs[0])) as depth_src:
            depth_data = depth_src.read(1)
            src_profile = depth_src.profile.copy()
            depth_nodata = depth_src.nodata

        with rasterio.open(str(wse_tifs[0])) as wse_src:
            wse_data = wse_src.read(1)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    # Build mask from depth: below threshold, existing nodata, or NaN
    mask = (depth_data < min_depth_m) | np.isnan(depth_data)
    if depth_nodata is not None:
        mask |= np.isclose(depth_data, depth_nodata)

    depth_data[mask] = NODATA
    wse_data[mask] = NODATA

    metadata = {
        "min_depth_threshold_ft": str(min_depth_ft),
        "source_hdf": hdf_path.name,
        "source_plan": f"p{plan_number}",
    }

    # Optional reprojection
    write_profile = src_profile
    if target_crs is not None and target_crs != src_profile.get("crs"):
        from rasterio.warp import (
            calculate_default_transform,
            reproject as rio_reproject,
            Resampling,
        )

        src_crs = src_profile["crs"]
        transform, width, height = calculate_default_transform(
            src_crs,
            target_crs,
            src_profile["width"],
            src_profile["height"],
            *rasterio.transform.array_bounds(
                src_profile["height"],
                src_profile["width"],
                src_profile["transform"],
            ),
        )
        write_profile = src_profile.copy()
        write_profile.update(
            crs=target_crs, transform=transform, width=width, height=height,
        )

        for arr_name in ("depth_data", "wse_data"):
            src_arr = depth_data if arr_name == "depth_data" else wse_data
            dst_arr = np.full((height, width), NODATA, dtype=np.float32)
            rio_reproject(
                source=src_arr,
                destination=dst_arr,
                src_transform=src_profile["transform"],
                src_crs=src_crs,
                dst_transform=transform,
                dst_crs=target_crs,
                resampling=Resampling.bilinear,
                src_nodata=NODATA,
                dst_nodata=NODATA,
            )
            if arr_name == "depth_data":
                depth_data = dst_arr
            else:
                wse_data = dst_arr

    outputs: dict[str, Path] = {}
    for name, data in [("filtered_depth", depth_data), ("filtered_wse", wse_data)]:
        out_path = output_dir / f"{name}.tif"
        _write_filtered_cog(data, write_profile, out_path, metadata)
        outputs[name] = out_path

    cells_filtered = int(mask.sum())
    cells_total = int(mask.size)
    logger.info(
        f"Filtered rasters: {cells_filtered}/{cells_total} cells below "
        f"{min_depth_ft} ft ({min_depth_m:.3f} m) → {output_dir}"
    )
    return outputs


# ── Cloud-Native Export (ras2cng) ─────────────────────────────────────────────

def export_cloud_native(
    project_dir: Union[str, Path],
    output_dir: Union[str, Path],
    include_results: bool = True,
    include_terrain: bool = True,
    r2_config=None,
) -> Optional[Path]:
    """
    Export HEC-RAS project to cloud-native GeoParquet archive via ras2cng.

    Graceful degradation: returns None if ras2cng is not installed, logging a
    warning rather than raising.  Never hard-fails on an optional dependency.

    Args:
        project_dir:      Path to the HEC-RAS project directory.
        output_dir:       Directory to write the archive (will be created).
        include_results:  If True, include plan result HDF5 data in archive.
        include_terrain:  If True, include terrain data in archive.
        r2_config:        Optional R2Config for uploading archive to Cloudflare R2.

    Returns:
        Path to the archive directory, or None if ras2cng is not available or
        the export fails.
    """
    try:
        from ras2cng import archive_project  # type: ignore[import]
    except ImportError:
        logger.warning(
            "ras2cng not installed — skipping cloud-native export. "
            "Install with: pip install ras2cng"
        )
        return None

    project_dir = Path(project_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        manifest = archive_project(
            project_dir,
            output_dir,
            include_results=include_results,
            include_terrain=include_terrain,
        )
        # Log manifest summary
        geom_count = len(manifest.get("geometries", []))
        plan_count = len(manifest.get("plans", []))
        files = manifest.get("files", [])
        total_bytes = sum(
            Path(f).stat().st_size
            for f in files
            if isinstance(f, (str, Path)) and Path(f).exists()
        )
        logger.info(
            "[ras2cng] Archive complete — %d geometries, %d plans, %.1f MB → %s",
            geom_count,
            plan_count,
            total_bytes / 1e6,
            output_dir,
        )
    except Exception as exc:
        logger.warning("[ras2cng] archive_project failed (non-fatal): %s", exc)
        return None

    # Optional R2 upload
    if r2_config is not None:
        try:
            from storage import upload_results_dir  # type: ignore[import]
            run_name = output_dir.name
            r2_urls = upload_results_dir(output_dir, run_name, r2_config)
            logger.info("[ras2cng] Uploaded %d archive files to R2", len(r2_urls))
        except Exception as exc:
            logger.warning("[ras2cng] R2 upload failed (archive still saved locally): %s", exc)

    return output_dir


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
