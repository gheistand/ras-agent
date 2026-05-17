"""
model_builder.py — HEC-RAS 6.6 project builder

Builds a HEC-RAS 6.6 project directory from watershed delineation and
design hydrograph results. `geometry_first` is the target strategy:

  geometry_first   — create project from project scaffold, write .g## via
                     ras-commander GeomStorage, let HEC-RAS regenerate HDF

Legacy compatibility strategies still exist for older tests and workflows, but
they should not guide new implementation:

  template_clone   — legacy clone workflow pending retirement/quarantine
  hdf5_direct      — legacy experimental seed-project placeholder
  ras2025          — placeholder for a future public API, not current runtime

`geometry_first` is the production strategy: it writes watershed-derived
2D flow area perimeters into plain-text `.g##` files via `ras-commander`
`GeomStorage` and lets HEC-RAS regenerate derived HDF/preprocessor artifacts.

Mesh generation should stay RASMapper-aligned through `ras-commander`, not
through a separate Cartesian runtime path.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

import logging
import math
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from water_source import (
        WaterSourceContractError,
        ensure_project_water_source_ready,
        normalize_water_source_mode,
    )
except ImportError:  # pragma: no cover - package-style import fallback
    from pipeline.water_source import (
        WaterSourceContractError,
        ensure_project_water_source_ready,
        normalize_water_source_mode,
    )


BOUNDARY_CONDITION_MODES = ("headwater", "downstream")
DOWNSTREAM_BOUNDARY_CONDITION_TODO = (
    "Define the durable input contract for upstream inflow hydrographs and their provenance.",
    "Decide how downstream basins reference upstream model outputs versus external hydrograph inputs.",
    "Add builder/orchestrator/batch regression fixtures for chained basins, including AD8 fallback weighting checks.",
)


def normalize_boundary_condition_mode(boundary_condition_mode: Optional[str]) -> str:
    """Normalize supported boundary-condition mode labels."""
    if boundary_condition_mode is None:
        return "headwater"

    normalized = str(boundary_condition_mode).strip().lower().replace("-", "_")
    aliases = {
        "headwater": "headwater",
        "downstream": "downstream",
        "non_headwater": "downstream",
        "nonheadwater": "downstream",
        "chained": "downstream",
        "chained_model": "downstream",
    }
    if normalized not in aliases:
        raise ValueError(
            f"Unknown boundary_condition_mode: {boundary_condition_mode!r}. "
            f"Valid options: {', '.join(BOUNDARY_CONDITION_MODES)}"
        )
    return aliases[normalized]


def downstream_boundary_condition_scaffold_message() -> str:
    """Return the current fail-fast note for downstream/chained basin support."""
    todo_text = "; ".join(DOWNSTREAM_BOUNDARY_CONDITION_TODO)
    return (
        "boundary_condition_mode='downstream' is scaffolded through the builder "
        "and orchestration APIs, but non-headwater basin support is not implemented "
        f"yet. Remaining planning/work items: {todo_text}"
    )


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
    mesh_strategy: str       # "geometry_first", "template_clone", "hdf5_direct", "ras2025"
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
DEFAULT_MANNINGS_N = 0.060


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
        f"Run HTab=-1\n"
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

def _detect_geom_format(geometry_file: Path) -> str:
    """
    Detect whether a HEC-RAS geometry file uses modern "2D Flow Area" format
    or older "Storage Area 2D" format.

    The Storage Area 2D format is identified by the presence of
    ``Storage Area Is2D=-1`` anywhere in the file (e.g. Mud Creek project).
    In this format the perimeter section is headed by ``Storage Area Surface
    Line=`` and coordinates are encoded in 16-char fixed-width pairs (the
    same encoding as ``Storage Area 2D Points``).

    Args:
        geometry_file: Path to .g## ASCII geometry file.

    Returns:
        ``"storage_area"`` if Storage Area 2D format is detected,
        ``"2d_flow_area"`` otherwise (default).
    """
    text = geometry_file.read_text(errors="replace")
    if "Storage Area Is2D=-1" in text:
        return "storage_area"
    if "2D Flow Area=" in text:
        return "2d_flow_area"
    return "2d_flow_area"


def _get_2d_area_name_from_geometry_file(geometry_file: Path) -> Optional[str]:
    """
    Parse a HEC-RAS .g## file and return the first 2D flow area name found.

    Supports both ``2D Flow Area`` format (modern) and ``Storage Area 2D``
    format (older; identified by ``Storage Area Is2D=-1`` within 100 lines of
    the ``Storage Area=`` header).

    Returns:
        Area name string, or None if no 2D flow area is defined.
    """
    text = geometry_file.read_text(errors="replace")

    # Format A: modern "2D Flow Area" format
    pattern_a = re.compile(r"^2D Flow Area=\s*(.+?)\s*,", re.MULTILINE | re.IGNORECASE)
    match = pattern_a.search(text)
    if match:
        return match.group(1).strip()

    # Format B: older "Storage Area 2D" format
    # Find a Storage Area block that has Storage Area Is2D=-1 within 100 lines
    lines = text.splitlines()
    sa_pattern = re.compile(r"^Storage Area=\s*(.+?)\s*,", re.IGNORECASE)
    for i, line in enumerate(lines):
        m = sa_pattern.match(line)
        if m:
            window = lines[i : i + 100]
            if any("Storage Area Is2D=-1" in wl for wl in window):
                return m.group(1).strip()

    return None


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

    Supports both ``2D Flow Area`` format (modern) and ``Storage Area 2D``
    format (older; detected via :func:`_detect_geom_format`).  In Storage
    Area format the perimeter section header is ``Storage Area Surface Line=``
    and coordinates are written as 16-char fixed-width pairs (same encoding as
    ``Storage Area 2D Points``).  The ``2D Flow Area Cell Size=`` field is
    **not** written in Storage Area format.

    Args:
        geometry_file:     Path to .g01 / .g02 / etc. ASCII geometry file
        area_name:         Name of the 2D flow area to update (e.g. "Perimeter 1")
        perimeter_coords:  List of (x, y) tuples in project CRS (EPSG:5070, meters)
                           Will be automatically closed (first point appended if not already)
        cell_size_m:       Target mesh cell size in meters (written to Cell Size field;
                           ignored for Storage Area format)

    Returns:
        True if perimeter was found and updated, False if area_name not found in file.

    Notes:
        - Creates a .bak backup of the original file before modifying
        - Closes the polygon automatically if first != last point
        - Preserves all other content in the geometry file unchanged
    """
    geom_format = _detect_geom_format(geometry_file)
    text = geometry_file.read_text(errors="replace")

    # ── Storage Area 2D format ─────────────────────────────────────────────────
    if geom_format == "storage_area":
        # Check that the named storage area exists in the file
        sa_check = re.compile(
            r"^Storage Area=\s*" + re.escape(area_name) + r"\s*,",
            re.MULTILINE | re.IGNORECASE,
        )
        if not sa_check.search(text):
            logger.warning(
                f"Storage area '{area_name}' not found in {geometry_file.name} — perimeter not updated"
            )
            return False

        # Backup before any modification
        bak_path = geometry_file.with_suffix(geometry_file.suffix + ".bak")
        bak_path.write_text(text, encoding="utf-8")

        # Ensure polygon is closed
        coords = list(perimeter_coords)
        if coords[0] != coords[-1]:
            coords.append(coords[0])

        # 16-char fixed-width pairs (same encoding as Storage Area 2D Points)
        coord_lines = "".join(
            _fmt_coord(float(x)) + _fmt_coord(float(y)) + "\n" for x, y in coords
        )
        new_surface_block = f"Storage Area Surface Line= {len(coords)}\n{coord_lines}"

        # Replace existing Surface Line block (header + coordinate lines)
        surface_pattern = re.compile(
            r"Storage Area Surface Line=[ \t]*\d+[ \t]*\n"
            r"(?:[-0-9][^\n]*\n)*",
            re.MULTILINE,
        )
        updated_text, n_subs = surface_pattern.subn(new_surface_block, text, count=1)

        if n_subs == 0:
            # No existing Surface Line block — insert after the Storage Area= header line
            sa_header_pattern = re.compile(
                r"(Storage Area=\s*" + re.escape(area_name) + r"[^\n]*\n)",
                re.MULTILINE | re.IGNORECASE,
            )
            updated_text = sa_header_pattern.sub(
                r"\g<1>" + new_surface_block,
                text,
                count=1,
            )

        geometry_file.write_text(updated_text, encoding="utf-8")
        logger.info(
            f"Updated Storage Area surface line in {geometry_file.name}: {len(coords)} points"
        )
        return True

    # ── 2D Flow Area format (default) ─────────────────────────────────────────
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


def _watershed_polygon_5070(watershed):
    """Return a single watershed polygon in EPSG:5070."""
    from pyproj import Transformer
    from shapely.geometry import box
    import geopandas as gpd

    basin = getattr(watershed, "basin", None)
    basin_geom = getattr(basin, "geometry", None)
    if basin_geom is not None and hasattr(basin_geom, "iloc"):
        geom = basin_geom.iloc[0]
        basin_crs = getattr(basin, "crs", None) or "EPSG:5070"
        basin_gdf = gpd.GeoDataFrame(geometry=[geom], crs=basin_crs).to_crs("EPSG:5070")
        return basin_gdf.geometry.iloc[0]

    if basin is not None and hasattr(basin, "geom_type"):
        basin_gdf = gpd.GeoDataFrame(geometry=[basin], crs="EPSG:5070").to_crs("EPSG:5070")
        return basin_gdf.geometry.iloc[0]

    chars = watershed.characteristics
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    center_x, center_y = transformer.transform(chars.centroid_lon, chars.centroid_lat)
    area_m2 = max(chars.drainage_area_km2, 0.01) * 1_000_000.0
    side = math.sqrt(area_m2)
    return box(
        center_x - side / 2.0,
        center_y - side / 2.0,
        center_x + side / 2.0,
        center_y + side / 2.0,
    )


def _linework_5070(gdf_like) -> Optional["gpd.GeoDataFrame"]:
    """Coerce a linework GeoDataFrame-like object to EPSG:5070."""
    if gdf_like is None or not hasattr(gdf_like, "geometry"):
        return None
    import geopandas as gpd

    crs = getattr(gdf_like, "crs", None) or "EPSG:5070"
    gdf = gpd.GeoDataFrame(gdf_like.copy(), geometry=gdf_like.geometry, crs=crs)
    return gdf.to_crs("EPSG:5070")


def _build_seed_breaklines(
    watershed,
    basin_poly,
    centerlines_gdf,
):
    """Return breaklines in EPSG:5070 from watershed outputs or fallbacks."""
    import geopandas as gpd

    existing = _linework_5070(getattr(watershed, "breaklines", None))
    if existing is not None and len(existing) > 0:
        return existing

    geoms = []
    types = []
    if centerlines_gdf is not None and len(centerlines_gdf) > 0:
        for geom in centerlines_gdf.geometry:
            geoms.append(geom)
            types.append("stream")

    boundary = basin_poly.boundary
    if hasattr(boundary, "geoms"):
        for geom in boundary.geoms:
            geoms.append(geom)
            types.append("boundary")
    else:
        geoms.append(boundary)
        types.append("boundary")

    return gpd.GeoDataFrame({"breakline_type": types}, geometry=geoms, crs="EPSG:5070")


def _cell_size_from_area_km2(area_km2: float) -> float:
    """Adaptive default cell size for a seed project."""
    return min(max(area_km2 * 0.5, 30.0), 300.0)


def _seed_cell_centers(basin_poly, cell_size_m: float, max_cells: int = 4000):
    """Generate regular seed cell centers inside the basin polygon."""
    import numpy as np
    from shapely.geometry import Point
    from shapely.prepared import prep

    minx, miny, maxx, maxy = basin_poly.bounds
    xs = np.arange(minx + cell_size_m / 2.0, maxx, cell_size_m)
    ys = np.arange(miny + cell_size_m / 2.0, maxy, cell_size_m)
    prepared = prep(basin_poly)
    centers = []
    for y in ys:
        for x in xs:
            if prepared.contains(Point(float(x), float(y))):
                centers.append((float(x), float(y)))

    if not centers:
        centers = [(float(basin_poly.centroid.x), float(basin_poly.centroid.y))]
    if len(centers) > max_cells:
        stride = math.ceil(len(centers) / max_cells)
        centers = centers[::stride]
    return np.asarray(centers, dtype=float)


def _write_project_file(prj_file: Path, project_name: str) -> None:
    """Write a minimal HEC-RAS project file."""
    prj_file.write_text(
        f"Proj Title={project_name}\n"
        f"Current Plan=p01\n"
        f"Program Version=6.60\n"
    )


def _write_geometry_seed_file(
    geom_file: Path,
    area_name: str,
    perimeter_coords: list[tuple[float, float]],
    cell_size_m: float,
    mannings_n: float,
) -> None:
    """
    Write a minimal ASCII geometry file for the current experimental seed path.

    Long-term, watershed-derived 2D flow area geometry should be emitted via
    `ras-commander` geometry writers rather than maintained here as the primary
    project-assembly contract.
    """
    coords = list(perimeter_coords)
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    coord_lines = "\n".join(f"     {x:.3f},{y:.3f}" for x, y in coords)
    geom_file.write_text(
        f"Geom Title=RAS Agent Seed Geometry\n"
        f"Program Version=6.60\n\n"
        f"2D Flow Area= {area_name}  ,0\n"
        f"2D Flow Area Perimeter= {len(coords)}\n"
        f"{coord_lines}\n"
        f"2D Flow Area Cell Size= {cell_size_m:.1f}\n"
        f"Mann= {mannings_n:.3f} ,0 ,0\n"
    )


def _seed_hdf_geometry(
    hdf_path: Path,
    area_name: str,
    cell_centers,
    terrain_path: Optional[Path] = None,
    mannings_n: float = DEFAULT_MANNINGS_N,
) -> None:
    """Write minimal geometry metadata into a plan or geometry HDF."""
    import h5py
    import numpy as np

    with h5py.File(hdf_path, "w") as hf:
        hf.attrs["File Type"] = "HEC-RAS Seed Geometry"
        hf.create_group("Plan Data")
        area_group = hf.require_group(f"Geometry/2D Flow Areas/{area_name}")
        area_group.create_dataset(
            "Cells Center Coordinate",
            data=np.asarray(cell_centers, dtype=np.float64),
        )
        mann = np.zeros((1, 3), dtype=float)
        mann[:, 1] = mannings_n
        area_group.create_dataset("Mann", data=mann)
        if terrain_path is not None:
            area_group.create_dataset("Terrain Filename", data=str(terrain_path))


def _centerline_count(watershed) -> int:
    centerlines = getattr(watershed, "centerlines", None)
    if centerlines is not None and hasattr(centerlines, "__len__"):
        return len(centerlines)
    streams = getattr(watershed, "streams", None)
    if streams is not None and hasattr(streams, "__len__"):
        return len(streams)
    return 0


def _text_mesh_point_count(geom_file: Path, area_name: str) -> int:
    """Check .g01 text for existing mesh seed points in the 2D flow area."""
    try:
        text = geom_file.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("Storage Area 2D Points="):
                count_str = line.split("=", 1)[1].strip()
                return int(count_str) if count_str else 0
        return 0
    except Exception:
        return 0


# ── Cartesian Mesh Generation ────────────────────────────────────────────────


def _fmt_coord(x: float) -> str:
    """
    16-character fixed-width HEC-RAS coordinate encoder.

    CRITICAL: Do not change this function. HEC-RAS 6.6 parses the Storage
    Area 2D Points section using 16-character fixed-width fields. A naive
    ``f"{x:.6f}"`` produces 14 characters — the parser reads the next
    coordinate starting at the wrong column and the cell centers are garbage.

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
    vertex coordinate.

    Args:
        watershed_polygon:    Shapely Polygon in project CRS (EPSG:5070, meters)
        cell_size_m:          Mesh cell size in meters
        min_face_length_ratio: HEC-RAS MinFaceLength / CellSize ratio (default 0.05)
        max_shift_tries:      Maximum (dx, dy) combinations to search

    Returns:
        Tuple of (cell_centers, dx_shift, dy_shift).
    """
    import numpy as np
    from shapely import contains_xy

    tol = min_face_length_ratio * cell_size_m

    if hasattr(watershed_polygon, "exterior"):
        verts = np.array(watershed_polygon.exterior.coords)
    else:
        verts = np.array(list(watershed_polygon.coords))

    vx = verts[:, 0]
    vy = verts[:, 1]

    xmin, ymin, xmax, ymax = watershed_polygon.bounds

    n_side = max(2, int(np.sqrt(max_shift_tries)))
    shift_step = cell_size_m / n_side

    best_shift = (0.0, 0.0)
    best_conflicts = int(len(vx)) * 2 + 1
    found_clean = False

    for i in range(n_side):
        dx = i * shift_step
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
            n_side * n_side, best_conflicts, dx_shift, dy_shift,
        )
    else:
        logger.info(
            "[CART] Clean grid shift: dx=%.1f m, dy=%.1f m (0 VB-vertex conflicts)",
            dx_shift, dy_shift,
        )

    xs = np.arange(xmin + dx_shift, xmax + cell_size_m, cell_size_m)
    ys = np.arange(ymin + dy_shift, ymax + cell_size_m, cell_size_m)
    xx, yy = np.meshgrid(xs, ys)
    candidates = np.column_stack([xx.ravel(), yy.ravel()])

    mask = contains_xy(watershed_polygon, candidates[:, 0], candidates[:, 1])
    cell_centers = candidates[mask]

    logger.info(
        "[CART] %d Cartesian cell centers (cell_size=%.1f m, dx=%.1f m, dy=%.1f m)",
        len(cell_centers), cell_size_m, dx_shift, dy_shift,
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

    Creates a .bak backup of the original file before modifying.

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

    bak_path = geometry_file.with_suffix(geometry_file.suffix + ".bak")
    bak_path.write_text(text, encoding="utf-8")

    n = len(cell_centers)
    new_header = f"Storage Area 2D Points= {n}\n"
    data_lines = "".join(
        _fmt_coord(float(x)) + _fmt_coord(float(y)) + "\n"
        for x, y in cell_centers
    )
    new_block = new_header + data_lines

    points_pattern = re.compile(
        r"Storage Area 2D Points=[ \t]*\d+[ \t]*\n"
        r"(?:[-0-9][^\n]*\n)*",
        re.MULTILINE,
    )
    updated_text, n_subs = points_pattern.subn(new_block, text, count=1)

    if n_subs == 0:
        cell_size_pat = re.compile(
            r"(2D Flow Area Cell Size=[ \t]*[\d.]+[ \t]*\n)", re.MULTILINE
        )
        m = cell_size_pat.search(text)
        if m:
            pos = m.end()
            updated_text = text[:pos] + new_block + text[pos:]
        else:
            updated_text = text.rstrip("\n") + "\n" + new_block

    geometry_file.write_text(updated_text, encoding="utf-8")
    logger.info(
        "[CART] Wrote %d cell centers to %s (area: %s)",
        n, geometry_file.name, area_name,
    )
    return True


# ── Template Clone Implementation ─────────────────────────────────────────────

def _build_from_template(
    watershed,
    hydro_set,
    output_dir: Path,
    return_periods: list,
    nlcd_raster_path: Optional[Path] = None,
    boundary_condition_mode: str = "headwater",
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
            basin_shape = _watershed_polygon_5070(watershed)
            boundary = basin_shape.exterior
            perimeter_coords = list(boundary.coords)
            # Adaptive cell size: 30-300m based on drainage area
            area_km2 = watershed.characteristics.drainage_area_km2
            cell_size_m = min(max(area_km2 * 0.5, 30.0), 300.0)
            area_name = _get_2d_area_name_from_geometry_file(geom_file)
            geom_format = _detect_geom_format(geom_file)
            logger.info(
                f"Geometry format detected: {geom_format!r} (area: {area_name!r}, file: {geom_file.name})"
            )
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
        "boundary_condition_mode": normalize_boundary_condition_mode(
            boundary_condition_mode
        ),
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
    boundary_condition_mode: str = "headwater",
    **kwargs,
) -> HecRasProject:
    """
    Build the current experimental seed HEC-RAS project from watershed geometry.

    This path does not require TEMPLATE_REGISTRY. It writes ASCII project,
    geometry, plan, and flow files plus minimal seed HDF metadata so downstream
    steps can carry project provenance and HEC-RAS can regenerate richer
    geometry on the next Windows-side open/save cycle.

    This is scaffolding, not the intended long-term build architecture. The
    production direction is to hand watershed geometry, roughness, and
    infiltration instructions to `ras-commander`, keep `.g##` authoritative for
    geometry-backed content, and let HEC-RAS rebuild compiled HDF artifacts.
    """
    project_name = f"ras_agent_{watershed.characteristics.drainage_area_mi2:.0f}mi2"
    project_dir = Path(output_dir) / project_name
    if project_dir.exists():
        shutil.rmtree(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)

    project_base = project_name
    prj_file = project_dir / f"{project_base}.prj"
    geom_file = project_dir / f"{project_base}.g01"
    geom_hdf = project_dir / f"{project_base}.g01.hdf"
    geom_ext = "g01"
    area_name = "MainArea"

    basin_poly = _watershed_polygon_5070(watershed)
    centerline_source = getattr(watershed, "centerlines", None)
    if centerline_source is None:
        centerline_source = getattr(watershed, "streams", None)
    centerlines_gdf = _linework_5070(centerline_source)
    breaklines_gdf = _build_seed_breaklines(watershed, basin_poly, centerlines_gdf)
    cell_size_m = _cell_size_from_area_km2(watershed.characteristics.drainage_area_km2)
    perimeter_coords = list(basin_poly.exterior.coords)

    if nlcd_raster_path is not None:
        mannings_n = dominant_mannings_n_from_raster(nlcd_raster_path, basin_poly)
    else:
        mannings_n = DEFAULT_MANNINGS_N

    _write_project_file(prj_file, project_name)
    _write_geometry_seed_file(
        geom_file,
        area_name=area_name,
        perimeter_coords=perimeter_coords,
        cell_size_m=cell_size_m,
        mannings_n=mannings_n,
    )

    cell_centers = _seed_cell_centers(basin_poly, cell_size_m)
    _seed_hdf_geometry(
        geom_hdf,
        area_name=area_name,
        cell_centers=cell_centers,
        terrain_path=getattr(watershed, "dem_clipped", None),
        mannings_n=mannings_n,
    )

    bc_slope = max(watershed.characteristics.main_channel_slope_m_per_m, 1e-6)

    for idx, rp in enumerate(return_periods, start=1):
        hydro = hydro_set.get(rp)
        if hydro is None:
            logger.warning(f"No hydrograph for T={rp}yr; skipping")
            continue

        u_suffix = f"u{idx:02d}"
        p_suffix = f"p{idx:02d}"
        rp_flow_file = project_dir / f"{project_base}.{u_suffix}"
        rp_plan_file = project_dir / f"{project_base}.{p_suffix}"
        rp_plan_hdf = project_dir / f"{project_base}.{p_suffix}.hdf"

        _write_unsteady_flow_file(rp_flow_file, hydro_set, rp, bc_slope)
        _write_plan_file(
            rp_plan_file,
            geom_file=geom_ext,
            flow_file=u_suffix,
            simulation_duration_hr=hydro.duration_hr,
            plan_title=f"T={rp}yr — RAS Agent Seed",
            short_id=f"T{rp}YR",
        )
        _seed_hdf_geometry(
            rp_plan_hdf,
            area_name=area_name,
            cell_centers=cell_centers,
            terrain_path=getattr(watershed, "dem_clipped", None),
            mannings_n=mannings_n,
        )

    flow_file = project_dir / f"{project_base}.u01"
    plan_file = project_dir / f"{project_base}.p01"
    plan_hdf = project_dir / f"{project_base}.p01.hdf"
    if not plan_hdf.exists():
        _seed_hdf_geometry(
            plan_hdf,
            area_name=area_name,
            cell_centers=cell_centers,
            terrain_path=getattr(watershed, "dem_clipped", None),
            mannings_n=mannings_n,
        )

    metadata = {
        "boundary_condition_mode": normalize_boundary_condition_mode(
            boundary_condition_mode
        ),
        "watershed_area_mi2": watershed.characteristics.drainage_area_mi2,
        "watershed_area_km2": watershed.characteristics.drainage_area_km2,
        "main_channel_slope": bc_slope,
        "mannings_n": mannings_n,
        "dem_clipped": str(getattr(watershed, "dem_clipped", "")),
        "centerline_count": _centerline_count(watershed),
        "breakline_count": len(breaklines_gdf),
        "artifact_keys": sorted(list(getattr(watershed, "artifacts", {}).keys())),
        "seed_project_only": True,
        "windows_regeneration_required": True,
        "geometry_hdf": str(geom_hdf),
    }

    logger.info(
        "Template-free seed project complete: %s (%d return periods, %.0f cell centers)",
        project_dir,
        len(return_periods),
        len(cell_centers),
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
        mesh_strategy="hdf5_direct",
        return_periods=return_periods,
        metadata=metadata,
    )


# ── Geometry-First Implementation ────────────────────────────────────────────

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "data" / "RAS_6.6_Template"


def _scaffold_project_from_template(
    output_dir: Path,
    project_name: str,
) -> tuple[Path, Path, Path]:
    """
    Copy the RAS_6.6_Template project scaffold and rename files for the new project.

    Returns (project_dir, prj_file, rasmap_file).
    """
    project_dir = output_dir / project_name
    if project_dir.exists():
        shutil.rmtree(project_dir)
    shutil.copytree(TEMPLATE_DIR, project_dir)

    src_prj = project_dir / "TEMPLATE.prj"
    dst_prj = project_dir / f"{project_name}.prj"
    if src_prj.exists():
        text = src_prj.read_text(encoding="utf-8")
        src_prj.unlink()
    else:
        # TEMPLATE.prj is often absent from git checkouts because *.prj files
        # are ignored with generated HEC-RAS project artifacts.
        text = "Proj Title=TEMPLATE\nProgram Version=6.60\n"
    dst_prj.write_text(
        text.replace("Proj Title=TEMPLATE", f"Proj Title={project_name}"),
        encoding="utf-8",
    )

    src_rasmap = project_dir / "TEMPLATE.rasmap"
    dst_rasmap = project_dir / f"{project_name}.rasmap"
    if src_rasmap.exists():
        dst_rasmap.write_text(
            src_rasmap.read_text(encoding="utf-8").replace("TEMPLATE", project_name),
            encoding="utf-8",
        )
        src_rasmap.unlink()
    else:
        dst_rasmap.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<RASMapper><Version>2.0.0</Version><Terrains /></RASMapper>\n",
            encoding="utf-8",
        )

    bak = project_dir / "TEMPLATE.rasmap.backup"
    if bak.exists():
        bak.unlink()

    readme = project_dir / "README.md"
    if readme.exists():
        readme.unlink()

    return project_dir, dst_prj, dst_rasmap


def _register_files_in_prj(
    prj_file: Path,
    geom_ext: str = "g01",
    flow_ext: str = "u01",
    plan_ext: str = "p01",
) -> None:
    """Append geometry, flow, and plan file references to the .prj file."""
    text = prj_file.read_text(encoding="utf-8")
    lines_to_add = (
        f"Current Plan={plan_ext}\n"
        f"Geom File={geom_ext}\n"
        f"Unsteady File={flow_ext}\n"
        f"Plan File={plan_ext}\n"
    )
    prj_file.write_text(text + lines_to_add, encoding="utf-8")


def _register_terrain_in_rasmap(rasmap_file: Path, terrain_path: Path) -> None:
    """Replace the empty terrain node in a RASMap file with the project terrain."""
    tree = ET.parse(rasmap_file)
    root = tree.getroot()
    terrains = root.find("Terrains")
    if terrains is None:
        raise ValueError(f"Terrains element not found in {rasmap_file}")

    registered_terrains = ET.Element(
        "Terrains",
        {"Checked": "True", "Expanded": "True"},
    )
    ET.SubElement(
        registered_terrains,
        "Layer",
        {
            "Name": "Terrain",
            "Type": "TerrainLayer",
            "Checked": "True",
            "Filename": r".\terrain.tif",
        },
    )

    for index, child in enumerate(list(root)):
        if child is terrains:
            root.remove(terrains)
            root.insert(index, registered_terrains)
            break

    ET.indent(tree, space="  ")
    tree.write(rasmap_file, encoding="utf-8", short_empty_elements=True)
    logger.info("Registered terrain in rasmap: %s -> %s", rasmap_file.name, terrain_path.name)


_UNSUPPORTED_GEOM_PREFIXES = (
    "Storage Area 2D PointsPerimeterTime",
    "2D Cell Minimum Area Fraction",
    "2D Face Area Laminar Depth",
)


def _strip_unsupported_geom_lines(geom_file: Path) -> None:
    """Remove GeomStorage lines that cause HEC-RAS 6.6 to hang."""
    text = geom_file.read_text(encoding="utf-8")
    lines = [
        l for l in text.splitlines()
        if not any(l.startswith(p) for p in _UNSUPPORTED_GEOM_PREFIXES)
    ]
    geom_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_geometry_first_geom_file(
    geom_file: Path,
    area_name: str,
    perimeter_coords: list[tuple[float, float]],
    cell_size_m: float,
    mannings_n: float,
) -> None:
    """
    Create a .g## file and populate it via ras-commander GeomStorage.

    Writes the initial header, then delegates perimeter and settings to
    GeomStorage.set_2d_flow_area_perimeter() and set_2d_flow_area_settings().
    """
    from ras_commander.geom import GeomStorage

    geom_file.write_text(
        "Geom Title=RAS Agent Geometry\n"
        "Program Version=6.60\n",
        encoding="utf-8",
    )

    GeomStorage.set_2d_flow_area_perimeter(
        geom_file,
        area_name,
        coordinates=perimeter_coords,
        recompute_centroid=True,
        point_generation_data=[None, None, int(cell_size_m), int(cell_size_m)],
        create_backup=False,
    )

    GeomStorage.set_2d_flow_area_settings(
        geom_file,
        area_name,
        mannings_n=mannings_n,
        spatially_varied_mann_on_faces=True,
        composite_classification=True,
        create_backup=False,
    )

    # Strip GeomStorage lines that HEC-RAS 6.6 can't parse (causes silent hang)
    _strip_unsupported_geom_lines(geom_file)

    logger.info(
        "Wrote geometry-first .g## via GeomStorage: %s (%d vertices, cell=%dm, n=%.3f)",
        geom_file.name, len(perimeter_coords), int(cell_size_m), mannings_n,
    )


def _build_geometry_first(
    watershed,
    hydro_set,
    output_dir: Path,
    return_periods: list,
    nlcd_raster_path: Optional[Path] = None,
    boundary_condition_mode: str = "headwater",
    **kwargs,
) -> HecRasProject:
    """
    Build a HEC-RAS 6.6 project using the geometry-first workflow.

    Copies the RAS_6.6_Template project scaffold, writes watershed-derived 2D flow
    area geometry via ras-commander GeomStorage, and creates plan/flow files.
    HEC-RAS regenerates all HDF artifacts on the next compute_plan() call.
    """
    boundary_condition_mode = normalize_boundary_condition_mode(
        boundary_condition_mode
    )
    if boundary_condition_mode != "headwater":
        raise NotImplementedError(downstream_boundary_condition_scaffold_message())

    project_name = f"ras_agent_{watershed.characteristics.drainage_area_mi2:.0f}mi2"
    project_dir, prj_file, rasmap_file = _scaffold_project_from_template(output_dir, project_name)

    area_name = "MainArea"
    geom_ext = "g01"
    geom_file = project_dir / f"{project_name}.{geom_ext}"

    basin_poly = _watershed_polygon_5070(watershed)
    cell_size_m = _cell_size_from_area_km2(watershed.characteristics.drainage_area_km2)

    # Smooth perimeter to remove narrow concavities that cause Cell Area Error
    smooth_dist = cell_size_m * 0.5
    basin_smoothed = basin_poly.buffer(smooth_dist).buffer(-smooth_dist)
    basin_smoothed = basin_smoothed.simplify(cell_size_m * 0.25, preserve_topology=True)
    perimeter_coords = list(basin_smoothed.exterior.coords)

    if nlcd_raster_path is not None:
        mannings_n = dominant_mannings_n_from_raster(nlcd_raster_path, basin_poly)
    else:
        mannings_n = DEFAULT_MANNINGS_N

    _write_geometry_first_geom_file(
        geom_file, area_name, perimeter_coords, cell_size_m, mannings_n,
    )

    # Insert stream centerlines as breaklines for mesh refinement
    streams_gdf_raw = _linework_5070(getattr(watershed, "streams", None))
    streams_5070 = list(streams_gdf_raw.geometry) if streams_gdf_raw is not None else []
    breakline_simplify_ft = kwargs.get("breakline_simplify_ft", 10.0)
    if streams_5070:
        from ras_commander.geom import GeomStorage as _GeomStorageBL
        simplify_tol_m = breakline_simplify_ft * 0.3048 if breakline_simplify_ft else 0
        breakline_defs = []
        for idx, geom in enumerate(streams_5070, start=1):
            if simplify_tol_m > 0:
                geom = geom.simplify(simplify_tol_m, preserve_topology=True)
            breakline_defs.append({
                "name": f"Stream{idx}",
                "coords": list(geom.coords),
                "cell_size_near": cell_size_m * 0.33,
                "cell_size_far": None,
            })
        _GeomStorageBL.set_breaklines(
            geom_file, area_name, breakline_defs, create_backup=False,
        )
        total_pts = sum(len(bl["coords"]) for bl in breakline_defs)
        logger.info(
            "Inserted %d stream breaklines (%d vertices, simplified at %.0fft) into %s",
            len(breakline_defs), total_pts, breakline_simplify_ft or 0, geom_file.name,
        )

    dem_clipped = getattr(watershed, "dem_clipped", None)
    terrain_file = None
    if dem_clipped is not None and Path(dem_clipped).exists():
        terrain_file = project_dir / "terrain.tif"
        shutil.copy2(dem_clipped, terrain_file)
        _register_terrain_in_rasmap(rasmap_file, terrain_file)
    else:
        logger.warning("No clipped DEM available for terrain registration; skipping")

    bc_slope = max(watershed.characteristics.main_channel_slope_m_per_m, 1e-6)

    # Generate 2D BC Lines from watershed/stream/terrain data
    from pipeline.bc_lines import (
        append_bc_lines_to_geom,
        generate_bc_lines,
        write_unsteady_flow_file_2d,
    )
    from shapely.geometry import Point as ShapelyPoint

    pp = getattr(watershed, "pour_point", None)
    pour_point_geom = ShapelyPoint(pp.x, pp.y) if pp is not None else basin_poly.centroid

    dem_for_bc = terrain_file

    # Contributing area grid for flow splitting (optional)
    ad8_path = None
    artifacts = getattr(watershed, "artifacts", {})
    if "ad8" in artifacts and Path(artifacts["ad8"]).exists():
        ad8_path = Path(artifacts["ad8"])

    # Future non-headwater/chained-basin work belongs here once the upstream
    # hydrograph handoff contract is defined and validated. The plumbing now
    # reaches this builder via `boundary_condition_mode`, but we intentionally
    # fail fast above until downstream-specific inputs and QA are designed.
    bc_set = generate_bc_lines(
        basin=basin_poly,
        streams=streams_5070,
        pour_point=pour_point_geom,
        dem_path=dem_for_bc,
        ad8_path=ad8_path,
        area_name=area_name,
        channel_slope=bc_slope,
        headwater=True,
    )
    append_bc_lines_to_geom(geom_file, bc_set)

    for idx, rp in enumerate(return_periods, start=1):
        hydro = hydro_set.get(rp)
        if hydro is None:
            logger.warning(f"No hydrograph for T={rp}yr; skipping")
            continue

        u_suffix = f"u{idx:02d}"
        p_suffix = f"p{idx:02d}"
        rp_flow_file = project_dir / f"{project_name}.{u_suffix}"
        rp_plan_file = project_dir / f"{project_name}.{p_suffix}"

        write_unsteady_flow_file_2d(rp_flow_file, hydro_set, rp, bc_set, bc_slope)
        _write_plan_file(
            rp_plan_file,
            geom_file=geom_ext,
            flow_file=u_suffix,
            simulation_duration_hr=hydro.duration_hr,
            plan_title=f"T={rp}yr — RAS Agent",
            short_id=f"T{rp}YR",
        )

    _register_files_in_prj(prj_file, geom_ext=geom_ext)

    # Set breakline spacing before mesh generation
    if streams_5070:
        try:
            from ras_commander.geom import GeomMesh as _GeomMesh
            _GeomMesh.set_breakline_spacing(
                str(geom_file),
                near=cell_size_m * 0.33,
                far=cell_size_m,
                all_breaklines=True,
            )
        except ImportError:
            logger.warning("GeomMesh not available; skipping breakline spacing")

    # Headless mesh generation via RasMapperLib (operates on .g01 text)
    mesh_result = None
    existing_points = _text_mesh_point_count(geom_file, area_name)
    if existing_points > 0:
        logger.info(
            "Mesh already exists: %d points (%s) — skipping generation",
            existing_points, area_name,
        )
    else:
        try:
            from ras_commander.geom import GeomMesh
            mesh_result = GeomMesh.generate(
                str(geom_file),
                mesh_name=area_name,
                cell_size=cell_size_m,
                bl_spacing=cell_size_m / 2.0,
                near_repeats=1,
                max_iterations=8,
            )
            if mesh_result.ok:
                logger.info(
                    "Mesh generated: %d cells, %d faces (%s)",
                    mesh_result.cell_count, mesh_result.face_count, area_name,
                )
            else:
                logger.warning(
                    "Mesh generation incomplete: %s (fixes: %s)",
                    mesh_result.error_message, mesh_result.fixes_applied,
                )
        except ImportError:
            logger.info(
                "GeomMesh not available — open project in RAS Mapper to generate mesh",
            )
        except Exception as exc:
            logger.warning("Mesh generation failed: %s", exc)

    mesh_point_count = _text_mesh_point_count(geom_file, area_name)

    mesh_qa_package = None
    try:
        from pipeline.mesh_qa import build_mesh_qa_package

        mesh_qa_package = build_mesh_qa_package(
            geom_file,
            output_dir=project_dir / "qa" / "mesh",
            area_name=area_name,
            regenerated_hdf_path=geom_file.with_suffix(geom_file.suffix + ".hdf"),
            target_cell_size_m=cell_size_m,
            mesh_result=mesh_result,
        )
    except ImportError:
        try:
            from mesh_qa import build_mesh_qa_package

            mesh_qa_package = build_mesh_qa_package(
                geom_file,
                output_dir=project_dir / "qa" / "mesh",
                area_name=area_name,
                regenerated_hdf_path=geom_file.with_suffix(geom_file.suffix + ".hdf"),
                target_cell_size_m=cell_size_m,
                mesh_result=mesh_result,
            )
        except Exception as exc:
            logger.warning("Mesh QA package generation failed: %s", exc)
    except Exception as exc:
        logger.warning("Mesh QA package generation failed: %s", exc)

    flow_file = project_dir / f"{project_name}.u01"
    plan_file = project_dir / f"{project_name}.p01"
    plan_hdf = project_dir / f"{project_name}.p01.hdf"

    metadata = {
        "boundary_condition_mode": boundary_condition_mode,
        "downstream_boundary_condition_todo": list(
            DOWNSTREAM_BOUNDARY_CONDITION_TODO
        ),
        "watershed_area_mi2": watershed.characteristics.drainage_area_mi2,
        "watershed_area_km2": watershed.characteristics.drainage_area_km2,
        "main_channel_slope": bc_slope,
        "mannings_n": mannings_n,
        "cell_size_m": cell_size_m,
        "dem_clipped": str(getattr(watershed, "dem_clipped", "")),
        "centerline_count": _centerline_count(watershed),
        "breakline_count": len(bl) if (bl := _linework_5070(getattr(watershed, "breaklines", None))) is not None else 0,
        "mesh_cells": mesh_result.cell_count if mesh_result and mesh_result.ok else mesh_point_count,
        "mesh_status": mesh_result.status if mesh_result else "deferred",
        "mesh_qa_status": mesh_qa_package.get("status") if mesh_qa_package else "failed",
        "mesh_qa_artifacts": mesh_qa_package.get("artifacts") if mesh_qa_package else {},
        "mesh_qa_flag_count": len(mesh_qa_package.get("flags", [])) if mesh_qa_package else 0,
        "artifact_keys": sorted(list(getattr(watershed, "artifacts", {}).keys())),
    }

    logger.info(
        "Geometry-first project complete: %s (%d return periods)",
        project_dir, len(return_periods),
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
        mesh_strategy="geometry_first",
        return_periods=return_periods,
        metadata=metadata,
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
    """
    raise NotImplementedError(
        "RAS2025 API integration not yet implemented. "
        "Use mesh_strategy='geometry_first' for current work. "
        "RAS2025 is alpha as of March 2026; no Linux build available. "
    )


# ── Main Interface ────────────────────────────────────────────────────────────

def _build_mock_project(
    watershed,
    hydro_set,
    output_dir: Path,
    return_periods: list,
    boundary_condition_mode: str = "headwater",
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
        metadata={
            "mock": True,
            "boundary_condition_mode": normalize_boundary_condition_mode(
                boundary_condition_mode
            ),
        },
    )


def _hydrograph_water_source_provenance(hydro_set, return_periods: list) -> dict:
    """Build a compact provenance payload for generated boundary hydrographs."""
    hydrographs = {}
    for rp in return_periods:
        hydro = hydro_set.get(rp)
        if hydro is None:
            continue
        flows = getattr(hydro, "flows_cfs", [])
        try:
            point_count = len(flows)
        except TypeError:
            point_count = 0
        hydrographs[str(rp)] = {
            "source": getattr(hydro, "source", None),
            "peak_flow_cfs": getattr(hydro, "peak_flow_cfs", None),
            "duration_hr": getattr(hydro, "duration_hr", None),
            "time_step_hr": getattr(hydro, "time_step_hr", None),
            "point_count": point_count,
        }
    return {
        "source": "generated_design_hydrograph",
        "method": "hydrograph.generate_hydrograph_set",
        "return_periods": list(return_periods),
        "hydrographs": hydrographs,
    }


def _default_water_source_provenance(
    hydro_set,
    return_periods: list,
    *,
    mock: bool,
) -> dict:
    if mock:
        return {
            "source": "mock_screening",
            "method": "synthetic test model; no production water source",
        }
    return _hydrograph_water_source_provenance(hydro_set, return_periods)


def build_model(
    watershed,
    hydro_set,
    output_dir: Path,
    return_periods: Optional[list] = None,
    mesh_strategy: str = "geometry_first",
    boundary_condition_mode: str = "headwater",
    nlcd_raster_path: Optional[Path] = None,
    water_source_mode: Optional[str] = "auto",
    water_source_provenance: Optional[dict] = None,
    allow_low_detail_screening: bool = False,
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
        mesh_strategy:      "geometry_first" | "hdf5_direct" | "template_clone" | "ras2025"
                            default is "geometry_first", which uses
                            ras-commander GeomStorage to write .g## and
                            lets HEC-RAS/RASMapper regenerate HDF artifacts.
                            The other strategies are legacy compatibility paths
                            or placeholders, not recommended fallbacks.
        boundary_condition_mode:
                            "headwater" | "downstream". The downstream option is
                            scaffolded through the API but intentionally fails
                            fast until chained-basin inputs and QA are designed.
        nlcd_raster_path:   Optional NLCD 2019 GeoTIFF for Manning's n lookup
        water_source_mode:  "auto" | "rain_on_grid" | "external_hydrograph" |
                            "mock_screening" | "none". Production headwater
                            builds must validate a defensible water source.
        water_source_provenance:
                            Optional source/provenance payload to store in
                            project metadata and validation artifacts.
        allow_low_detail_screening:
                            Allow explicit low-detail screening output that is
                            not production-ready.

    Returns:
        HecRasProject ready for runner.py

    Raises:
        ValueError:          Unknown mesh_strategy
        RuntimeError:        No templates registered (template_clone only)
        NotImplementedError: ras2025 strategy not yet implemented
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if return_periods is None:
        return_periods = sorted(hydro_set.hydrographs.keys())
    if not return_periods:
        raise ValueError("return_periods is empty and hydro_set has no hydrographs")

    boundary_condition_mode = normalize_boundary_condition_mode(
        boundary_condition_mode
    )

    normalized_water_source_mode = normalize_water_source_mode(
        water_source_mode,
        mock=mock,
        allow_low_detail_screening=allow_low_detail_screening,
    )

    logger.info(
        f"build_model: strategy={mesh_strategy}, "
        f"bc_mode={boundary_condition_mode}, "
        f"water_source={normalized_water_source_mode}, "
        f"area={watershed.characteristics.drainage_area_mi2:.1f} mi², "
        f"return_periods={return_periods}"
    )

    if boundary_condition_mode != "headwater":
        raise NotImplementedError(downstream_boundary_condition_scaffold_message())

    if mock:
        project = _build_mock_project(
            watershed,
            hydro_set,
            output_dir,
            return_periods,
            boundary_condition_mode=boundary_condition_mode,
        )
    elif mesh_strategy == "geometry_first":
        project = _build_geometry_first(
            watershed,
            hydro_set,
            output_dir,
            return_periods,
            nlcd_raster_path,
            boundary_condition_mode=boundary_condition_mode,
            **kwargs,
        )
    elif mesh_strategy == "template_clone":
        project = _build_from_template(
            watershed,
            hydro_set,
            output_dir,
            return_periods,
            nlcd_raster_path,
            boundary_condition_mode=boundary_condition_mode,
        )
    elif mesh_strategy == "hdf5_direct":
        project = _build_hdf5_direct(
            watershed,
            hydro_set,
            output_dir,
            return_periods,
            nlcd_raster_path,
            boundary_condition_mode=boundary_condition_mode,
            **kwargs,
        )
    elif mesh_strategy == "ras2025":
        project = _build_ras2025(
            watershed, hydro_set, output_dir, return_periods, nlcd_raster_path, **kwargs
        )
    else:
        raise ValueError(
            f"Unknown mesh_strategy: '{mesh_strategy}'. "
            "Valid options: 'geometry_first', 'template_clone', 'hdf5_direct', 'ras2025'"
        )

    provenance = (
        water_source_provenance
        if water_source_provenance is not None
        else _default_water_source_provenance(
            hydro_set,
            return_periods,
            mock=mock,
        )
    )
    explicit_screening = normalized_water_source_mode == "mock_screening"
    require_production_ready = not (
        mock or explicit_screening or allow_low_detail_screening
    )
    validation = ensure_project_water_source_ready(
        project,
        water_source_mode=normalized_water_source_mode,
        water_source_provenance=provenance,
        mock=mock,
        allow_low_detail_screening=allow_low_detail_screening,
        require_production_ready=require_production_ready,
    )
    logger.info(
        "Water-source validation: mode=%s status=%s production_ready=%s",
        validation["mode"],
        validation["contract_status"],
        validation["production_ready"],
    )
    return project
