"""
model_builder.py — HEC-RAS 6.6 project builder

Builds a HEC-RAS 6.6 project directory from watershed delineation and
design hydrograph results. Supports three mesh strategies:

  template_clone  — clone an existing template project, swap terrain/BCs
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
        from ras_commander import RasPrj
        caps = []
        if hasattr(RasPrj, "clone_project"):
            caps.append("clone_project")
        if hasattr(RasPrj, "set_mannings_n") or hasattr(RasPrj, "update_mannings"):
            caps.append("mannings_n")
        result["capabilities"] = caps
    except ImportError:
        pass
    return result


def _clone_project(template_dir: Path, output_dir: Path, project_name: str) -> Path:
    """
    Clone a HEC-RAS template project to a new directory.
    Uses RAS Commander if available, falls back to shutil.copytree.
    Returns path to cloned project directory.
    """
    dest = output_dir / project_name
    try:
        from ras_commander import RasPrj
        prj = RasPrj(str(template_dir))
        prj.clone_project(str(dest))
        logger.info(f"Cloned project via RAS Commander: {dest}")
    except ImportError:
        logger.warning("ras-commander not installed — using shutil.copytree fallback")
        shutil.copytree(template_dir, dest)
    except Exception as e:
        logger.warning(f"RAS Commander clone failed ({e}) — using shutil.copytree fallback")
        shutil.copytree(template_dir, dest)
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
    Uses RAS Commander if available.
    Returns True if updated, False if skipped (no RC or not supported).
    """
    try:
        from ras_commander import RasPrj
        prj = RasPrj(str(project_dir))
        if hasattr(prj, "set_mannings_n"):
            prj.set_mannings_n(mannings_n)
            logger.info(f"Updated Manning's n to {mannings_n} via RAS Commander")
            return True
        elif hasattr(prj, "update_mannings"):
            prj.update_mannings(mannings_n)
            logger.info(f"Updated Manning's n to {mannings_n} via RAS Commander")
            return True
        else:
            logger.warning(
                "RAS Commander installed but Manning's n update method not found — "
                "using HDF5 fallback"
            )
            return _update_mannings_n_hdf5(project_dir, mannings_n)
    except ImportError:
        logger.warning("ras-commander not installed — using HDF5 fallback for Manning's n")
        return _update_mannings_n_hdf5(project_dir, mannings_n)
    except Exception as e:
        logger.warning(f"RAS Commander Manning's n update failed ({e}) — using HDF5 fallback")
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
            boundary = basin_5070.geometry.iloc[0].exterior
            perimeter_coords = list(boundary.coords)
            # Adaptive cell size: 30-300m based on drainage area
            area_km2 = watershed.characteristics.drainage_area_km2
            cell_size_m = min(max(area_km2 * 0.5, 30.0), 300.0)
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
    else:
        logger.warning(
            "No geometry file or watershed basin geometry — perimeter not updated (using template mesh)"
        )

    # ── 5. Update terrain reference in geometry HDF
    geom_hdf_candidates = list(project_dir.glob("*.g01.hdf"))
    if geom_hdf_candidates:
        _update_terrain_reference(geom_hdf_candidates[0], watershed.dem_clipped)
    else:
        logger.warning(
            "No .g01.hdf geometry HDF found in cloned project; "
            "terrain reference not updated. May need first GUI run on Windows."
        )

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
    }

    logger.info(
        f"Template clone complete: {project_dir} "
        f"({len(return_periods)} return periods)"
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

def build_model(
    watershed,
    hydro_set,
    output_dir: Path,
    return_periods: Optional[list] = None,
    mesh_strategy: str = "template_clone",
    nlcd_raster_path: Optional[Path] = None,
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
