"""
model_builder.py — HEC-RAS 6.6 project builder

Builds a HEC-RAS 6.6 project directory from watershed delineation and
design hydrograph results. Supports three mesh strategies:

  template_clone  — clone an existing template project, swap terrain/BCs/
                    Manning's n, then write Cartesian cell centers so the
                    HEC-RAS 6.6 geometry preprocessor regenerates the mesh
                    via Voronoi tessellation from those centers (no RASMapper
                    or GUI required; approach by Bill Katzenmeyer / CLB
                    Engineering, April 2026 — see docs/KNOWLEDGE.md)
  hdf5_direct     — write geometry HDF5 from scratch (future)
  ras2025         — use RAS2025 API for mesh generation (future)

The interface is path-agnostic from day one; only template_clone is
implemented in Phase 2. No RAS Commander import — RAS Commander use
is deferred until after clarification from Bill Katzenmeyer.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import logging
import math
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class HecRasProject:
    """A fully configured HEC-RAS project directory ready for runner.py."""
    project_dir: Path
    project_name: str
    prj_file: Path           # .prj
    geometry_file: Path      # .g01
    flow_file: Path          # .u01
    plan_file: Path          # .p01
    plan_hdf: Path           # .p01.hdf (after first GUI run) or .p01.tmp.hdf
    geom_ext: str            # "g01"
    mesh_strategy: str       # "template_clone", "hdf5_direct", "ras2025"
    return_periods: list
    metadata: dict = field(default_factory=dict)
    cell_count: Optional[int] = None        # number of 2D cells generated (Cartesian mesh)
    cell_size_m: Optional[float] = None     # cell size used (meters)
    grid_shift: Optional[tuple] = None     # (dx_shift, dy_shift) grid origin offset used


@dataclass
class TemplateConfig:
    """Configuration for a watershed template project."""
    name: str                # e.g. "small_il_agricultural"
    template_dir: Path       # path to template HEC-RAS project
    target_area_mi2: float   # representative drainage area
    description: str = ""


# ── Template Registry ─────────────────────────────────────────────────────────

# Default template configs — populated when Glenn builds templates on Windows.
# Keys: "small" (~50 mi²), "medium" (~200 mi²), "large" (~800 mi²)
TEMPLATE_REGISTRY: dict = {}


def register_template(
    key: str,
    template_dir: Path,
    target_area_mi2: float,
    description: str = "",
) -> None:
    """
    Add a template to TEMPLATE_REGISTRY.

    Args:
        key:               Registry key (e.g. "small", "medium", "large")
        template_dir:      Path to the template HEC-RAS project directory
        target_area_mi2:   Representative drainage area (mi²)
        description:       Human-readable description
    """
    template_dir = Path(template_dir)
    if not template_dir.exists():
        raise ValueError(f"Template directory does not exist: {template_dir}")
    prj_files = list(template_dir.glob("*.prj"))
    if not prj_files:
        raise ValueError(
            f"No .prj file found in template directory: {template_dir}"
        )
    cfg = TemplateConfig(
        name=key,
        template_dir=template_dir,
        target_area_mi2=target_area_mi2,
        description=description,
    )
    TEMPLATE_REGISTRY[key] = cfg
    logger.info(
        f"Registered template '{key}': {target_area_mi2:.0f} mi² at {template_dir}"
    )


def select_template(drainage_area_mi2: float) -> Optional[TemplateConfig]:
    """
    Return the template whose target area is closest on a log scale.

    Args:
        drainage_area_mi2:  Watershed drainage area (mi²)

    Returns:
        Closest TemplateConfig, or None if registry is empty.
    """
    if not TEMPLATE_REGISTRY:
        return None
    log_target = math.log(max(drainage_area_mi2, 0.01))
    best_key = min(
        TEMPLATE_REGISTRY,
        key=lambda k: abs(math.log(max(TEMPLATE_REGISTRY[k].target_area_mi2, 0.01)) - log_target),
    )
    chosen = TEMPLATE_REGISTRY[best_key]
    logger.info(
        f"Selected template '{best_key}' ({chosen.target_area_mi2:.0f} mi²) "
        f"for watershed {drainage_area_mi2:.1f} mi²"
    )
    return chosen


# ── Manning's n Lookup ────────────────────────────────────────────────────────

# NLCD 2019 class codes → Manning n values for Illinois streams
# Reference: standard HEC-RAS 2D friction values for IL land cover
NLCD_MANNINGS_N: dict = {
    11: 0.035,  # Open Water
    21: 0.080,  # Developed, Open Space
    22: 0.080,  # Developed, Low Intensity
    23: 0.100,  # Developed, Medium Intensity
    24: 0.120,  # Developed, High Intensity
    31: 0.030,  # Barren Land
    41: 0.120,  # Deciduous Forest
    42: 0.120,  # Evergreen Forest
    43: 0.120,  # Mixed Forest
    52: 0.060,  # Shrub/Scrub
    71: 0.035,  # Grassland/Herbaceous
    81: 0.033,  # Pasture/Hay
    82: 0.037,  # Cultivated Crops
    90: 0.075,  # Woody Wetlands
    95: 0.075,  # Emergent Herbaceous Wetlands
}
DEFAULT_MANNINGS_N = 0.040


def get_mannings_n(nlcd_class: int) -> float:
    """Return Manning's n for an NLCD 2019 class code."""
    return NLCD_MANNINGS_N.get(nlcd_class, DEFAULT_MANNINGS_N)


def dominant_mannings_n_from_raster(nlcd_raster_path: Path, watershed_geom) -> float:
    """
    Clip NLCD raster to watershed polygon and return Manning's n for the
    dominant (most common) land cover class.

    Args:
        nlcd_raster_path:  Path to NLCD 2019 GeoTIFF
        watershed_geom:    Shapely geometry (watershed boundary polygon)

    Returns:
        Manning's n float; DEFAULT_MANNINGS_N if raster unavailable.
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.mask import mask as rio_mask

        with rasterio.open(nlcd_raster_path) as src:
            out_image, _ = rio_mask(src, [watershed_geom], crop=True, nodata=0)
        data = out_image.flatten()
        data = data[data > 0]
        if data.size == 0:
            logger.warning("NLCD raster clip returned no valid pixels; using default n")
            return DEFAULT_MANNINGS_N
        unique, counts = np.unique(data, return_counts=True)
        dominant_class = int(unique[counts.argmax()])
        n_value = get_mannings_n(dominant_class)
        logger.info(
            f"Dominant NLCD class: {dominant_class} → Manning's n = {n_value:.3f}"
        )
        return n_value
    except Exception as exc:
        logger.warning(f"Could not read NLCD raster ({exc}); using default n={DEFAULT_MANNINGS_N}")
        return DEFAULT_MANNINGS_N


# ── HEC-RAS ASCII File Writers ────────────────────────────────────────────────

def _write_unsteady_flow_file(
    flow_file_path: Path,
    hydro_set,
    return_period: int,
    bc_slope: float,
    river: str = "RAS_AGENT",
    reach: str = "MAIN",
    us_station: str = "1.0",
    ds_station: str = "0.0",
) -> None:
    """
    Write a HEC-RAS .u## unsteady flow file for a single return period.

    Format follows HEC-RAS 6.6 ASCII unsteady flow file specification.
    Upstream boundary: flow hydrograph.
    Downstream boundary: normal depth with given slope.

    Args:
        flow_file_path:  Output path for .u## file
        hydro_set:       HydrographSet from hydrograph.py
        return_period:   Return period to write (must exist in hydro_set)
        bc_slope:        Normal depth boundary condition slope (m/m)
        river:           River name (placeholder if geometry names unknown)
        reach:           Reach name
        us_station:      Upstream cross-section station
        ds_station:      Downstream cross-section station
    """
    hydro = hydro_set.get(return_period)
    if hydro is None:
        raise ValueError(
            f"Return period {return_period} not found in HydrographSet. "
            f"Available: {sorted(hydro_set.hydrographs.keys())}"
        )

    flows = hydro.flows_cfs
    n_points = len(flows)

    # Build flow values block: 10 values per line
    flow_lines = []
    for i in range(0, n_points, 10):
        chunk = flows[i:i + 10]
        flow_lines.append("".join(f"{v:10.2f}" for v in chunk))
    flow_block = "\n".join(flow_lines)

    title = f"T={return_period}yr Hydrograph"
    content = (
        f"Flow Title={title}\n"
        f"Program Version=6.60\n"
        f"BEGIN FILE DESCRIPTION:\n"
        f"END FILE DESCRIPTION:\n"
        f"\n"
        f"Boundary Location={river},{reach},{us_station}\n"
        f"Interval=15MIN\n"
        f"Flow Hydrograph= {n_points}\n"
        f"{flow_block}\n"
        f"\n"
        f"Boundary Location={river},{reach},{ds_station}\n"
        f"Normal Depth={bc_slope:.6f}\n"
    )

    flow_file_path.write_text(content)
    logger.info(f"Wrote unsteady flow file: {flow_file_path} ({n_points} points)")


def _write_plan_file(
    plan_file_path: Path,
    geom_file: str,
    flow_file: str,
    simulation_duration_hr: float,
    warm_up_hr: float = 12.0,
    plan_title: str = "RAS Agent Plan",
    short_id: str = "RASAGENT",
) -> None:
    """
    Write a HEC-RAS .p## plan file.

    Simulation starts at 01 Jan 2000, 00:00 with a warm-up period followed
    by the full hydrograph duration.

    Args:
        plan_file_path:        Output path for .p## file
        geom_file:             Geometry file extension (e.g. "g01")
        flow_file:             Unsteady flow file extension (e.g. "u01")
        simulation_duration_hr: Hydrograph duration (hours)
        warm_up_hr:            Warm-up period before hydrograph (hours)
        plan_title:            Plan title string
        short_id:              Short plan identifier (≤8 chars)
    """
    total_hr = warm_up_hr + simulation_duration_hr
    start_dt = datetime(2000, 1, 1, 0, 0)
    end_dt = start_dt + timedelta(hours=total_hr)

    start_str = start_dt.strftime("%d%b%Y") + "," + start_dt.strftime("%H%M")
    end_str = end_dt.strftime("%d%b%Y") + "," + end_dt.strftime("%H%M")

    content = (
        f"Plan Title={plan_title}\n"
        f"Program Version=6.60\n"
        f"Geom File={geom_file}\n"
        f"Flow File={flow_file}\n"
        f"Run HTab= 0\n"
        f"Run UNet= -1\n"
        f"Run Sed= 0\n"
        f"Run WQ= 0\n"
        f"Short ID={short_id}\n"
        f"Simulation Date={start_str},{end_str}\n"
        f"Computation Interval=30SEC\n"
        f"Output Interval=1HOUR\n"
        f"Instantaneous Interval=15MIN\n"
    )

    plan_file_path.write_text(content)
    logger.info(
        f"Wrote plan file: {plan_file_path} "
        f"(duration={total_hr:.1f} hr: {warm_up_hr:.0f} hr warm-up + {simulation_duration_hr:.1f} hr hydrograph)"
    )


def _update_terrain_reference(geom_hdf_path: Path, new_terrain_path: Path) -> None:
    """
    Update the terrain filename reference in a HEC-RAS geometry HDF5 file.

    HDF5 path: /Geometry/2D Flow Areas/{area_name}/Terrain Filename

    If the path is not found (e.g. template uses embedded terrain), logs a
    warning and skips rather than raising.

    Args:
        geom_hdf_path:    Path to geometry HDF5 (.g01.hdf or .p01.tmp.hdf)
        new_terrain_path: New terrain GeoTIFF path to write into HDF
    """
    try:
        import h5py
    except ImportError:
        logger.warning("h5py not available; cannot update terrain reference")
        return

    terrain_str = str(new_terrain_path)
    try:
        with h5py.File(geom_hdf_path, "r+") as hf:
            areas_group = hf.get("Geometry/2D Flow Areas")
            if areas_group is None:
                logger.warning(
                    f"HDF path '/Geometry/2D Flow Areas' not found in {geom_hdf_path}; "
                    "skipping terrain reference update"
                )
                return
            updated = 0
            for area_name in areas_group:
                key = f"Geometry/2D Flow Areas/{area_name}/Terrain Filename"
                if key in hf:
                    del hf[key]
                    hf[key] = terrain_str
                    updated += 1
                    logger.debug(f"Updated terrain reference for area '{area_name}'")
            if updated == 0:
                logger.warning(
                    f"No 'Terrain Filename' datasets found under /Geometry/2D Flow Areas "
                    f"in {geom_hdf_path}; template may use embedded terrain"
                )
            else:
                logger.info(
                    f"Updated terrain reference in {geom_hdf_path} → {terrain_str}"
                )
    except OSError as exc:
        logger.warning(f"Could not open geometry HDF {geom_hdf_path}: {exc}")


def _remove_geometry_hdfs(project_dir: Path) -> int:
    """Delete stale geometry HDF files (.g##.hdf) from a cloned project directory.

    Removing these files forces ``ras_preprocess.py`` into Workflow C on the
    next run: it reads Cartesian cell centers from the ASCII ``.g##`` file and
    regenerates the mesh via Voronoi tessellation — our new watershed mesh, not
    the template's.

    ``.p##.hdf`` plan HDF files are intentionally left untouched.

    Args:
        project_dir: Root directory of the cloned HEC-RAS project.

    Returns:
        Number of geometry HDF files successfully deleted.
    """
    import re

    deleted = 0
    for hdf_path in list(project_dir.glob("*.g*.hdf")):
        # Match only .g##.hdf (one or two digits), not .p##.hdf
        if not re.search(r"\.g\d{2}\.hdf$", hdf_path.name):
            continue
        try:
            hdf_path.unlink()
            logger.info(
                "[template-clone] Deleted stale geometry HDF %s — "
                "ras_preprocess.py will regenerate via Workflow C",
                hdf_path.name,
            )
            deleted += 1
        except OSError as exc:
            logger.warning(
                "[template-clone] Could not delete geometry HDF %s: %s",
                hdf_path.name,
                exc,
            )
    return deleted


# ── RAS Commander Utilities ───────────────────────────────────────────────────

# RAS Commander — optional dependency, imported lazily
# Install: pip install ras-commander


def check_ras_commander() -> dict:
    """
    Check RAS Commander installation status.
    Returns dict with: installed (bool), version (str or None), capabilities (list[str])
    """
    result = {"installed": False, "version": None, "capabilities": []}
    try:
        import ras_commander
        result["installed"] = True
        result["version"] = getattr(ras_commander, "__version__", "unknown")
        caps = []
        # HDF results extraction (replaces raw h5py in results.py)
        try:
            from ras_commander.hdf import HdfResultsMesh, HdfMesh
            if hasattr(HdfResultsMesh, "get_mesh_max_ws"):
                caps.append("hdf_results")
            if hasattr(HdfMesh, "get_mesh_area_names"):
                caps.append("mesh_areas")
        except ImportError:
            pass
        # Utility methods
        try:
            from ras_commander import RasUtils
            if hasattr(RasUtils, "ignore_windows_reserved"):
                caps.append("windows_reserved_filter")
        except ImportError:
            pass
        # Manning's n via HDF (P2 will add GeomLandCover.set_2d_mannings_n_hdf)
        try:
            from ras_commander.geom import GeomLandCover
            if hasattr(GeomLandCover, "set_2d_mannings_n_hdf"):
                caps.append("mannings_n_hdf")
        except ImportError:
            pass
        result["capabilities"] = caps
    except ImportError:
        pass
    return result


def _clone_project(template_dir: Path, output_dir: Path, project_name: str) -> Path:
    """
    Clone a HEC-RAS template project to a new directory.
    Uses shutil.copytree with Windows reserved name filtering when available.
    Returns path to cloned project directory.
    """
    dest = output_dir / project_name
    # Use RasUtils.ignore_windows_reserved if ras-commander is installed
    # to safely skip Windows virtual device names (CON, NUL, etc.)
    ignore_func = None
    try:
        from ras_commander import RasUtils
        ignore_func = RasUtils.ignore_windows_reserved
    except ImportError:
        pass
    shutil.copytree(template_dir, dest, ignore=ignore_func)
    logger.info(f"Cloned template project: {dest}")
    return dest


def _update_mannings_n_hdf5(project_dir: Path, mannings_n: float) -> bool:
    """
    Fallback: update Manning's n directly in geometry HDF5.
    Looks for .g01.hdf or .g02.hdf in project_dir.
    HDF path: /Geometry/2D Flow Areas/{area_name}/Mann
    Returns True if updated.
    """
    import h5py
    import numpy as np
    geom_hdfs = list(project_dir.glob("*.g??.hdf"))
    if not geom_hdfs:
        logger.warning(f"No geometry HDF found in {project_dir} — Manning's n not updated")
        return False
    for geom_hdf in geom_hdfs:
        with h5py.File(geom_hdf, "a") as f:
            fa_group = f.get("Geometry/2D Flow Areas")
            if fa_group is None:
                continue
            for area_name in fa_group.keys():
                mann_path = f"Geometry/2D Flow Areas/{area_name}/Mann"
                if mann_path in f:
                    # Mann dataset: shape (N, 3) — [region_id, n_value, calibration]
                    # Update column 1 (n_value) for all rows
                    mann = f[mann_path][:]
                    mann[:, 1] = mannings_n
                    f[mann_path][:] = mann
                    logger.info(
                        f"Updated Manning's n to {mannings_n} in {geom_hdf.name}/{area_name}"
                    )
    return True


def _update_mannings_n(project_dir: Path, mannings_n: float) -> bool:
    """
    Update Manning's n value for all 2D flow areas in the project.
    Uses RC GeomLandCover.set_2d_mannings_n_hdf() if available (P2),
    otherwise falls back to direct HDF5 write.
    Returns True if updated, False if no geometry HDF found.
    """
    # Try RC method first (added in P2 of ras-agent integration)
    try:
        from ras_commander.geom import GeomLandCover
        if hasattr(GeomLandCover, "set_2d_mannings_n_hdf"):
            geom_hdfs = list(project_dir.glob("*.g??.hdf"))
            if not geom_hdfs:
                logger.warning(f"No geometry HDF found in {project_dir}")
                return False
            for geom_hdf in geom_hdfs:
                GeomLandCover.set_2d_mannings_n_hdf(geom_hdf, mannings_n)
                logger.info(f"Updated Manning's n to {mannings_n} via RC: {geom_hdf.name}")
            return True
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"RC Manning's n update failed ({e}) — using HDF5 fallback")
    # Fallback: direct HDF5 write
    return _update_mannings_n_hdf5(project_dir, mannings_n)


# ── Geometry File (ASCII .g##) Utilities ──────────────────────────────────────

def _get_2d_area_name_from_geometry_file(geometry_file: Path) -> Optional[str]:
    """
    Parse a HEC-RAS .g## file and return the first 2D flow area name found.
    Returns None if no 2D flow area is defined.
    """
    pattern = re.compile(r"^2D Flow Area=\s*(.+?)\s*,", re.MULTILINE | re.IGNORECASE)
    text = geometry_file.read_text(errors="replace")
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _write_perimeter_to_geometry_file(
    geometry_file: Path,
    area_name: str,
    perimeter_coords: list,
    cell_size_m: float = 100.0,
) -> bool:
    """
    Update the 2D flow area perimeter in a HEC-RAS plain text geometry file (.g##).

    HEC-RAS regenerates the geometry HDF on the next save/open, so writing
    to the ASCII file is the correct approach (confirmed by Bill Katzenmeyer,
    CLB Engineering / RAS Commander, 2026-03-13).

    Args:
        geometry_file:     Path to .g01 / .g02 / etc. ASCII geometry file
        area_name:         Name of the 2D flow area to update (e.g. "Perimeter 1")
        perimeter_coords:  List of (x, y) tuples in project CRS (EPSG:5070, meters)
                           Will be automatically closed (first point appended if not already)
        cell_size_m:       Target mesh cell size in meters (written to Cell Size field)

    Returns:
        True if perimeter was found and updated, False if area_name not found in file.

    Notes:
        - Creates a .bak backup of the original file before modifying
        - Closes the polygon automatically if first != last point
        - Preserves all other content in the geometry file unchanged
    """
    text = geometry_file.read_text(errors="replace")

    # Check that the named 2D flow area exists in the file
    area_pattern = re.compile(
        r"(2D Flow Area=\s*" + re.escape(area_name) + r"\s*,\s*\d+\s*\n)",
        re.IGNORECASE,
    )
    if not area_pattern.search(text):
        logger.warning(
            f"2D flow area '{area_name}' not found in {geometry_file.name} — perimeter not updated"
        )
        return False

    # Create .bak backup BEFORE any modification
    bak_path = geometry_file.with_suffix(geometry_file.suffix + ".bak")
    bak_path.write_text(text, encoding="utf-8")

    # Ensure polygon is closed
    coords = list(perimeter_coords)
    if coords[0] != coords[-1]:
        coords.append(coords[0])

    coord_lines = "\n".join(f"     {x:.3f},{y:.3f}" for x, y in coords)
    new_perimeter_block = f"2D Flow Area Perimeter= {len(coords)}\n{coord_lines}\n"

    # Replace existing perimeter block (header + coordinate lines)
    perimeter_block_pattern = re.compile(
        r"(2D Flow Area Perimeter=\s*\d+\s*\n)"       # header line
        r"((?:[ \t]*-?[\d.]+\s*,\s*-?[\d.]+[ \t]*\n)*)",  # coordinate lines
        re.MULTILINE,
    )
    updated_text, n_subs = perimeter_block_pattern.subn(new_perimeter_block, text, count=1)

    if n_subs == 0:
        # No existing perimeter block found — insert after the 2D Flow Area header line
        updated_text = area_pattern.sub(
            r"\g<1>" + new_perimeter_block,
            updated_text,
            count=1,
        )

    # Update Cell Size line if present
    cell_size_pattern = re.compile(r"2D Flow Area Cell Size=\s*[\d.]+")
    updated_text = cell_size_pattern.sub(
        f"2D Flow Area Cell Size= {cell_size_m:.1f}", updated_text
    )

    geometry_file.write_text(updated_text, encoding="utf-8")
    logger.info(
        f"Updated perimeter in {geometry_file.name}: "
        f"{len(coords)} points, cell size {cell_size_m:.1f}m"
    )
    return True


# ── Cartesian Mesh Generation ─────────────────────────────────────────────────
# Cartesian mesh approach from CLB Engineering / Bill Katzenmeyer
# (Breaking the RAS 6.6 Mesh Lock, April 2026)
#
# Key insight: HEC-RAS 6.6 reads cell center coordinates from the "Storage Area
# 2D Points" section of the .g## text file, runs Voronoi tessellation, and writes
# full mesh topology to .g##.hdf.  Whoever controls the cell centers controls the
# mesh — no RASMapper, no GUI, no DLL needed.


def _fmt_coord(x: float) -> str:
    """
    16-character fixed-width HEC-RAS coordinate encoder.

    CRITICAL: Do not change this function. HEC-RAS 6.6 parses the Storage
    Area 2D Points section using 16-character fixed-width fields. A naive
    ``f"{x:.6f}"`` produces 14 characters — the parser reads the next
    coordinate starting at the wrong column and the cell centers are garbage.

    Works correctly for non-negative coordinates. EPSG:5070 (NAD83 Albers)
    values are always large positive numbers for the continental US.

    Args:
        x: Coordinate value (non-negative; EPSG:5070 meters)

    Returns:
        Exactly 16 characters: integer digits + decimal point + decimal digits.
    """
    n_int = len(str(int(abs(x))))
    n_dec = 16 - n_int - 1
    return f"{x:.{n_dec}f}"


def _generate_cartesian_cell_centers(
    watershed_polygon,
    cell_size_m: float,
    min_face_length_ratio: float = 0.05,
    max_shift_tries: int = 200,
) -> tuple:
    """
    Generate Cartesian cell centers for a HEC-RAS 2D mesh.

    Scans grid origin offsets (dx_shift, dy_shift) ∈ [0, cell_size_m) × [0,
    cell_size_m) to find a position where no Voronoi boundary (VB) falls
    within ``tol = min_face_length_ratio × cell_size_m`` of any polygon
    vertex coordinate.  This prevents topological face errors in the HEC-RAS
    6.6 geometry preprocessor.

    VB positions for a grid starting at (xmin + dx_shift, ymin + dy_shift)::

        x-VBs at xmin + dx_shift + cell_size_m/2 + k × cell_size_m
        y-VBs at ymin + dy_shift + cell_size_m/2 + m × cell_size_m

    If no clean shift is found after max_shift_tries the combination with the
    fewest conflicts is used and a warning is logged.

    Args:
        watershed_polygon:    Shapely Polygon in project CRS (EPSG:5070, meters)
        cell_size_m:          Mesh cell size in meters
        min_face_length_ratio: HEC-RAS MinFaceLength / CellSize ratio (default 0.05)
        max_shift_tries:      Maximum (dx, dy) combinations to search

    Returns:
        Tuple of (cell_centers, dx_shift, dy_shift):
          cell_centers — (N, 2) float64 array of (x, y) in project CRS
          dx_shift     — grid x-origin offset used (meters)
          dy_shift     — grid y-origin offset used (meters)

    Raises:
        ImportError: If numpy or shapely is not installed
    """
    import numpy as np
    from shapely import contains_xy

    tol = min_face_length_ratio * cell_size_m

    # Extract polygon exterior vertices
    if hasattr(watershed_polygon, "exterior"):
        verts = np.array(watershed_polygon.exterior.coords)
    else:
        verts = np.array(list(watershed_polygon.coords))

    vx = verts[:, 0]
    vy = verts[:, 1]

    xmin, ymin, xmax, ymax = watershed_polygon.bounds

    # Search grid: n_side × n_side combinations; break on first clean shift
    n_side = max(2, int(np.sqrt(max_shift_tries)))
    shift_step = cell_size_m / n_side

    best_shift = (0.0, 0.0)
    best_conflicts = int(len(vx)) * 2 + 1   # worse than any real result
    found_clean = False

    for i in range(n_side):
        dx = i * shift_step
        # x-VBs at xmin + dx + cell_size_m/2 + k*cell_size_m
        # distance of vertex vx_k to nearest x-VB:
        x_relv = (vx - xmin - dx - cell_size_m / 2) % cell_size_m
        x_dist = np.minimum(x_relv, cell_size_m - x_relv)
        n_x = int(np.sum(x_dist < tol))

        for j in range(n_side):
            dy = j * shift_step
            y_relv = (vy - ymin - dy - cell_size_m / 2) % cell_size_m
            y_dist = np.minimum(y_relv, cell_size_m - y_relv)
            n_y = int(np.sum(y_dist < tol))

            total = n_x + n_y
            if total < best_conflicts:
                best_conflicts = total
                best_shift = (dx, dy)

            if total == 0:
                found_clean = True
                break

        if found_clean:
            break

    dx_shift, dy_shift = best_shift

    if not found_clean:
        logger.warning(
            "[CART] No clean grid shift found in %d tries "
            "(best: %d conflict(s) at dx=%.1f m, dy=%.1f m) — using best available",
            n_side * n_side,
            best_conflicts,
            dx_shift,
            dy_shift,
        )
    else:
        logger.info(
            "[CART] Clean grid shift: dx=%.1f m, dy=%.1f m (0 VB-vertex conflicts)",
            dx_shift,
            dy_shift,
        )

    # Generate Cartesian grid; cell centers at xmin+dx_shift + k*cell_size_m
    xs = np.arange(xmin + dx_shift, xmax + cell_size_m, cell_size_m)
    ys = np.arange(ymin + dy_shift, ymax + cell_size_m, cell_size_m)
    xx, yy = np.meshgrid(xs, ys)
    candidates = np.column_stack([xx.ravel(), yy.ravel()])

    # Keep only centers inside the watershed polygon
    mask = contains_xy(watershed_polygon, candidates[:, 0], candidates[:, 1])
    cell_centers = candidates[mask]

    logger.info(
        "[CART] %d Cartesian cell centers (cell_size=%.1f m, dx=%.1f m, dy=%.1f m)",
        len(cell_centers),
        cell_size_m,
        dx_shift,
        dy_shift,
    )
    return cell_centers, dx_shift, dy_shift


def _write_cell_centers_to_geometry_file(
    geometry_file: Path,
    area_name: str,
    cell_centers,
) -> bool:
    """
    Write Cartesian cell centers to the ``Storage Area 2D Points`` section of a
    HEC-RAS plain text geometry file (.g##).

    The HEC-RAS 6.6 geometry preprocessor reads these cell centers and runs
    Voronoi tessellation to produce full mesh topology in .g##.hdf.  Each data
    line contains two 16-character fixed-width coordinates concatenated::

        Storage Area 2D Points= {N}
        {x1_16char}{y1_16char}
        {x2_16char}{y2_16char}
        ...

    See ``_fmt_coord()`` for the critical encoding requirement.

    Creates a .bak backup of the original file before modifying (same pattern
    as ``_write_perimeter_to_geometry_file``).

    Args:
        geometry_file: Path to .g## ASCII geometry file
        area_name:     Name of the 2D flow area (used for logging)
        cell_centers:  (N, 2) array-like of (x, y) cell center coordinates

    Returns:
        True if written successfully; False if cell_centers is empty.
    """
    if len(cell_centers) == 0:
        logger.warning(
            "[CART] No cell centers provided for area '%s' — Storage Area 2D Points not written",
            area_name,
        )
        return False

    text = geometry_file.read_text(errors="replace")

    # Backup before any modification
    bak_path = geometry_file.with_suffix(geometry_file.suffix + ".bak")
    bak_path.write_text(text, encoding="utf-8")

    # Build replacement block
    n = len(cell_centers)
    new_header = f"Storage Area 2D Points= {n}\n"
    data_lines = "".join(
        _fmt_coord(float(x)) + _fmt_coord(float(y)) + "\n"
        for x, y in cell_centers
    )
    new_block = new_header + data_lines

    # Replace existing "Storage Area 2D Points" section (data lines start with digit or -)
    points_pattern = re.compile(
        r"Storage Area 2D Points=[ \t]*\d+[ \t]*\n"
        r"(?:[-0-9][^\n]*\n)*",
        re.MULTILINE,
    )
    updated_text, n_subs = points_pattern.subn(new_block, text, count=1)

    if n_subs == 0:
        # No existing section — insert after "2D Flow Area Cell Size=" line
        cell_size_pat = re.compile(
            r"(2D Flow Area Cell Size=[ \t]*[\d.]+[ \t]*\n)", re.MULTILINE
        )
        m = cell_size_pat.search(text)
        if m:
            pos = m.end()
            updated_text = text[:pos] + new_block + text[pos:]
        else:
            # Fallback: append at end of file
            updated_text = text.rstrip("\n") + "\n" + new_block

    geometry_file.write_text(updated_text, encoding="utf-8")
    logger.info(
        "[CART] Wrote %d cell centers to %s (area: %s)",
        n,
        geometry_file.name,
        area_name,
    )
    return True


# ── Template Clone Implementation ─────────────────────────────────────────────

def _build_from_template(
    watershed,
    hydro_set,
    output_dir: Path,
    return_periods: list,
    nlcd_raster_path: Optional[Path] = None,
) -> HecRasProject:
    """
    Build a HEC-RAS project by cloning the closest area-matched template.

    Steps:
    1. Select template from registry by drainage area
    2. Clone template directory to output_dir/project_name
    3. Determine Manning's n from NLCD raster or default
    4. TODO (Bill Katzenmeyer): Update 2D flow area perimeter to match watershed
    5. Update terrain reference in geometry HDF
    6. Write unsteady flow files for each return period
    7. Write plan files for each return period
    8. Return HecRasProject

    Args:
        watershed:         WatershedResult from watershed.py
        hydro_set:         HydrographSet from hydrograph.py
        output_dir:        Root directory for the new project
        return_periods:    List of return period years to configure
        nlcd_raster_path:  Optional path to NLCD 2019 GeoTIFF
    """
    # ── 1. Select template
    template = select_template(watershed.characteristics.drainage_area_mi2)
    if template is None:
        raise RuntimeError(
            "No templates registered in TEMPLATE_REGISTRY. "
            "Build 2D HEC-RAS template projects on Windows and register them with "
            "register_template() before calling build_model(). "
            "See docs/KNOWLEDGE.md § 'Three Mesh Strategy Paths' for details."
        )

    # ── 2. Clone template
    project_name = f"ras_agent_{watershed.characteristics.drainage_area_mi2:.0f}mi2"
    project_dir = Path(output_dir) / project_name
    if project_dir.exists():
        logger.warning(f"Project directory already exists, removing: {project_dir}")
        shutil.rmtree(project_dir)
    _clone_project(template.template_dir, Path(output_dir), project_name)
    logger.info(f"Cloned template '{template.name}' → {project_dir}")

    # ── Discover key files in cloned project
    prj_files = list(project_dir.glob("*.prj"))
    if not prj_files:
        raise RuntimeError(f"No .prj file in cloned project: {project_dir}")
    prj_file = prj_files[0]
    project_base = prj_file.stem

    geom_files = list(project_dir.glob("*.g01"))
    geom_file = geom_files[0] if geom_files else project_dir / f"{project_base}.g01"
    geom_ext = "g01"

    flow_file = project_dir / f"{project_base}.u01"
    plan_file = project_dir / f"{project_base}.p01"
    plan_hdf = project_dir / f"{project_base}.p01.hdf"

    # ── 3. Manning's n
    if nlcd_raster_path is not None:
        mannings_n = dominant_mannings_n_from_raster(
            nlcd_raster_path, watershed.basin.geometry.iloc[0]
        )
    else:
        mannings_n = DEFAULT_MANNINGS_N
        logger.info(f"No NLCD raster provided; using default Manning's n = {mannings_n}")
    _update_mannings_n(project_dir, mannings_n)

    # Adaptive cell size: 30–300 m based on drainage area
    # (computed here so it's available for both perimeter write and cell centers)
    area_km2 = watershed.characteristics.drainage_area_km2
    cell_size_m = min(max(area_km2 * 0.5, 30.0), 300.0)

    # Variables populated in steps 4 / 4b
    watershed_polygon_5070 = None
    area_name = None
    cell_count = None
    grid_shift = None

    # ── 4. Update 2D flow area perimeter to match watershed boundary
    # (Bill Katzenmeyer / CLB Engineering confirmed 2026-03-13: write to ASCII .g## file;
    #  HEC-RAS regenerates geometry HDF on next save/open)
    geom_files = list(project_dir.glob("*.g??"))
    if geom_files and watershed.basin is not None:
        geom_file = geom_files[0]
        try:
            import geopandas as gpd
            basin_geom = getattr(watershed.basin, "geometry", None)
            if basin_geom is not None and hasattr(basin_geom, "iloc"):
                basin_shape = basin_geom.iloc[0]
            else:
                basin_shape = watershed.basin
            basin_gdf = gpd.GeoDataFrame(geometry=[basin_shape], crs="EPSG:4326")
            basin_5070 = basin_gdf.to_crs("EPSG:5070")
            watershed_polygon_5070 = basin_5070.geometry.iloc[0]
            boundary = watershed_polygon_5070.exterior
            perimeter_coords = list(boundary.coords)
            area_name = _get_2d_area_name_from_geometry_file(geom_file)
            if area_name:
                updated = _write_perimeter_to_geometry_file(
                    geom_file, area_name, perimeter_coords, cell_size_m
                )
                if updated:
                    logger.info(
                        f"Updated 2D flow area perimeter: {len(perimeter_coords)} points, "
                        f"cell size {cell_size_m:.0f}m"
                    )
                else:
                    logger.warning(
                        f"Could not update perimeter — area '{area_name}' not found in {geom_file.name}"
                    )
            else:
                logger.warning(
                    f"No 2D flow area found in {geom_file.name} — perimeter not updated"
                )
        except Exception as exc:
            logger.warning(f"Perimeter update failed ({exc}) — template mesh perimeter in use")

        # ── 4b. Write Cartesian cell centers
        # The HEC-RAS 6.6 geometry preprocessor runs Voronoi tessellation from
        # these centers and writes the full mesh to .g##.hdf — no GUI needed.
        # (Bill Katzenmeyer / CLB Engineering, April 2026)
        if watershed_polygon_5070 is not None and area_name is not None:
            try:
                cc, dx_s, dy_s = _generate_cartesian_cell_centers(
                    watershed_polygon_5070, cell_size_m
                )
                if len(cc) > 0:
                    _write_cell_centers_to_geometry_file(geom_file, area_name, cc)
                    cell_count = len(cc)
                    grid_shift = (dx_s, dy_s)
                    logger.info(
                        "[CART] %d Cartesian cell centers written "
                        "(cell_size=%.0f m, shift=(%.1f, %.1f) m)",
                        cell_count,
                        cell_size_m,
                        dx_s,
                        dy_s,
                    )
                else:
                    logger.warning(
                        "[CART] No cell centers inside polygon — skipping Storage Area 2D Points"
                    )
            except Exception as exc:
                logger.warning(
                    "[CART] Cell center generation failed (%s) — "
                    "preprocessor will use perimeter-only approach",
                    exc,
                )
    else:
        logger.warning(
            "No geometry file or watershed basin geometry — perimeter/cell centers not updated (using template mesh)"
        )

    # ── 5. Update terrain reference in geometry HDF
    geom_hdf_candidates = list(project_dir.glob("*.g01.hdf"))
    if geom_hdf_candidates:
        # --- Mesh preflight check ---
        try:
            from mesh_inspector import preflight_template, format_report
            logger.info("Running mesh pre-flight check on template geometry HDF...")
            pf = preflight_template(geom_hdf_candidates[0], max_cells=500)
            if not pf.passed:
                logger.warning(
                    "Template mesh has %d violation(s) — model may fail in HEC-RAS.\n%s",
                    pf.total_violations,
                    format_report(pf),
                )
            else:
                logger.info(
                    "Mesh pre-flight passed (%d cells sampled, 0 violations).",
                    sum(a.n_cells for a in pf.areas),
                )
        except Exception as exc:
            logger.warning("Mesh pre-flight check skipped: %s", exc)
        _update_terrain_reference(geom_hdf_candidates[0], watershed.dem_clipped)
    else:
        logger.warning(
            "No .g01.hdf geometry HDF found in cloned project; "
            "terrain reference not updated. May need first GUI run on Windows."
        )

    # ── 5b. Remove geometry HDF(s) so ras_preprocess.py uses Workflow C
    # (Workflow C reads Cartesian cell centers from ASCII .g## and regenerates
    # the HDF via Voronoi tessellation — our new watershed mesh, not the template)
    _remove_geometry_hdfs(project_dir)

    # ── 6. Write unsteady flow files + plan files
    bc_slope = watershed.characteristics.main_channel_slope_m_per_m

    for rp in return_periods:
        hydro = hydro_set.get(rp)
        if hydro is None:
            logger.warning(f"No hydrograph for T={rp}yr; skipping")
            continue

        # Flow file: .u01 for first RP (runner.py expects .u01)
        # For multi-RP support, suffix by return period index
        rp_idx = return_periods.index(rp) + 1
        u_suffix = f"u{rp_idx:02d}"
        p_suffix = f"p{rp_idx:02d}"

        rp_flow_file = project_dir / f"{project_base}.{u_suffix}"
        _write_unsteady_flow_file(
            rp_flow_file, hydro_set, rp, bc_slope
        )

        rp_plan_file = project_dir / f"{project_base}.{p_suffix}"
        _write_plan_file(
            rp_plan_file,
            geom_file=geom_ext,
            flow_file=u_suffix,
            simulation_duration_hr=hydro.duration_hr,
            plan_title=f"T={rp}yr — RAS Agent",
            short_id=f"T{rp}YR",
        )

    # Primary plan/flow = first return period
    primary_rp_idx = 1
    primary_u = f"u{primary_rp_idx:02d}"
    primary_p = f"p{primary_rp_idx:02d}"
    flow_file = project_dir / f"{project_base}.{primary_u}"
    plan_file = project_dir / f"{project_base}.{primary_p}"
    plan_hdf = project_dir / f"{project_base}.{primary_p}.hdf"

    metadata = {
        "template_name": template.name,
        "template_dir": str(template.template_dir),
        "template_area_mi2": template.target_area_mi2,
        "watershed_area_mi2": watershed.characteristics.drainage_area_mi2,
        "main_channel_slope": bc_slope,
        "mannings_n": mannings_n,
        "dem_clipped": str(watershed.dem_clipped),
        "cell_count": cell_count,
        "cell_size_m": cell_size_m,
        "grid_shift": grid_shift,
    }

    logger.info(
        f"Template clone complete: {project_dir} "
        f"({len(return_periods)} return periods, "
        f"{cell_count or 'no'} Cartesian cell centers)"
    )

    return HecRasProject(
        project_dir=project_dir,
        project_name=project_name,
        prj_file=prj_file,
        geometry_file=geom_file,
        flow_file=flow_file,
        plan_file=plan_file,
        plan_hdf=plan_hdf,
        geom_ext=geom_ext,
        mesh_strategy="template_clone",
        return_periods=return_periods,
        metadata=metadata,
        cell_count=cell_count,
        cell_size_m=cell_size_m,
        grid_shift=grid_shift,
    )


# ── Stub Implementations ──────────────────────────────────────────────────────

def _build_hdf5_direct(
    watershed,
    hydro_set,
    output_dir: Path,
    return_periods: list,
    nlcd_raster_path: Optional[Path] = None,
    **kwargs,
) -> HecRasProject:
    """
    TODO: Build HEC-RAS geometry HDF5 from scratch using h5py.
    Planned for Phase 2B after template+clone is proven.

    True greenfield approach: define 2D flow area perimeter from watershed
    polygon, generate mesh cells programmatically, write all HDF5 datasets
    matching HEC-RAS 6.6 geometry file spec.
    """
    raise NotImplementedError(
        "Direct HDF5 mesh construction not yet implemented. "
        "Use mesh_strategy='template_clone' for now. "
        "See docs/KNOWLEDGE.md § 'Three Mesh Strategy Paths' (Path B) for plan."
    )


def _build_ras2025(
    watershed,
    hydro_set,
    output_dir: Path,
    return_periods: list,
    nlcd_raster_path: Optional[Path] = None,
    **kwargs,
) -> HecRasProject:
    """
    TODO: Use RAS2025 API for mesh generation once API is stable.

    RAS2025 is currently in alpha (March 2026). No Linux build available yet.
    API may change. Estimated availability: 6-12 months out.

    See docs/KNOWLEDGE.md § 'Three Mesh Strategy Paths' (Path C) for details.
    """
    raise NotImplementedError(
        "RAS2025 API integration not yet implemented. "
        "Use mesh_strategy='template_clone' for now. "
        "RAS2025 is alpha as of March 2026; no Linux build available. "
        "See docs/KNOWLEDGE.md § 'Three Mesh Strategy Paths' (Path C)."
    )


# ── Main Interface ────────────────────────────────────────────────────────────

def _build_mock_project(
    watershed,
    hydro_set,
    output_dir: Path,
    return_periods: list,
) -> HecRasProject:
    """
    Create a dummy HEC-RAS project directory with placeholder files for mock mode.
    No templates required — allows full orchestrator pipeline in mock/Docker testing.
    """
    import h5py

    project_name = "mock_project"
    project_dir = output_dir / project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    # Placeholder ASCII files
    prj_file = project_dir / f"{project_name}.prj"
    geom_file = project_dir / f"{project_name}.g01"
    flow_file = project_dir / f"{project_name}.u01"
    plan_file = project_dir / f"{project_name}.p01"
    plan_hdf = project_dir / f"{project_name}.p01.tmp.hdf"

    prj_file.write_text(f"Proj Title={project_name}\nProgram Version=6.60\n")
    geom_file.write_text(
        f"Geom Title=Mock Geometry\nProgram Version=6.60\n"
        f"2D Flow Area= MockArea  ,0\n"
        f"2D Flow Area Perimeter=  5\n"
        f"     300000.000,4400000.000\n     300500.000,4400000.000\n"
        f"     300500.000,4400500.000\n     300000.000,4400500.000\n"
        f"     300000.000,4400000.000\n"
        f"2D Flow Area Cell Size=  100.0\n"
    )
    flow_file.write_text(f"Flow Title=Mock Flows\nProgram Version=6.60\n")
    plan_file.write_text(
        f"Plan Title=Mock Plan\nProgram Version=6.60\n"
        f"Geom File=g01\nFlow File=u01\n"
    )

    # Minimal HDF required by runner.py pre-run prep
    with h5py.File(plan_hdf, "w") as f:
        f.attrs["File Type"] = "HEC-RAS Results"
        f.create_group("Plan Data")

    logger.info(f"[mock] Created dummy HEC-RAS project at {project_dir}")

    return HecRasProject(
        project_dir=project_dir,
        project_name=project_name,
        prj_file=prj_file,
        geometry_file=geom_file,
        flow_file=flow_file,
        plan_file=plan_file,
        plan_hdf=plan_hdf,
        geom_ext="g01",
        mesh_strategy="mock",
        return_periods=return_periods,
        metadata={"mock": True},
    )


def build_model(
    watershed,
    hydro_set,
    output_dir: Path,
    return_periods: Optional[list] = None,
    mesh_strategy: str = "template_clone",
    nlcd_raster_path: Optional[Path] = None,
    mock: bool = False,
    **kwargs,
) -> HecRasProject:
    """
    Build a HEC-RAS 6.6 project from watershed and hydrology data.

    Path-agnostic interface: dispatches to the appropriate mesh strategy
    implementation.

    Args:
        watershed:          Delineated watershed (WatershedResult from watershed.py)
        hydro_set:          Design hydrographs (HydrographSet from hydrograph.py)
        output_dir:         Directory where the project folder will be created
        return_periods:     Return period years to configure; default = all in hydro_set
        mesh_strategy:      "template_clone" | "hdf5_direct" | "ras2025"
        nlcd_raster_path:   Optional NLCD 2019 GeoTIFF for Manning's n lookup

    Returns:
        HecRasProject ready for runner.py

    Raises:
        ValueError:          Unknown mesh_strategy
        RuntimeError:        No templates registered (template_clone only)
        NotImplementedError: hdf5_direct or ras2025 strategies not yet implemented
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if return_periods is None:
        return_periods = sorted(hydro_set.hydrographs.keys())
    if not return_periods:
        raise ValueError("return_periods is empty and hydro_set has no hydrographs")

    logger.info(
        f"build_model: strategy={mesh_strategy}, "
        f"area={watershed.characteristics.drainage_area_mi2:.1f} mi², "
        f"return_periods={return_periods}"
    )

    if mock:
        return _build_mock_project(watershed, hydro_set, output_dir, return_periods)

    if mesh_strategy == "template_clone":
        return _build_from_template(
            watershed, hydro_set, output_dir, return_periods, nlcd_raster_path
        )
    elif mesh_strategy == "hdf5_direct":
        return _build_hdf5_direct(
            watershed, hydro_set, output_dir, return_periods, nlcd_raster_path, **kwargs
        )
    elif mesh_strategy == "ras2025":
        return _build_ras2025(
            watershed, hydro_set, output_dir, return_periods, nlcd_raster_path, **kwargs
        )
    else:
        raise ValueError(
            f"Unknown mesh_strategy: '{mesh_strategy}'. "
            "Valid options: 'template_clone', 'hdf5_direct', 'ras2025'"
        )
