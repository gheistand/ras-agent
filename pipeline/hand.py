"""
hand.py — Height Above Nearest Drainage (HAND) computation

Computes the HAND raster from TauDEM artifacts produced during watershed
delineation. HAND = vertical elevation difference between each DEM cell and
the nearest stream cell along the D-infinity flow path.

Uses TauDEM DinfFlowDir + DinfDistDown (stat=min, dist=v) — the same method
used by NOAA OWP for National Water Model flood inundation mapping.

Reference: Nobre et al. (2011), "Height Above the Nearest Drainage — a
hydrologically relevant new terrain model", J. Hydrology, 404, 13-29.

Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey
Apache License 2.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.mask import mask as rio_mask

from taudem import TauDem, TauDemError

logger = logging.getLogger(__name__)

TARGET_CRS = CRS.from_epsg(5070)

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


@dataclass
class HandResult:
    """Result of HAND computation.

    Attributes:
        hand_path: Path to the HAND COG GeoTIFF (meters, EPSG:5070).
        hand_clipped_path: Path to watershed-clipped HAND raster, or None.
        min_hand_m: Minimum HAND value in the domain (meters).
        max_hand_m: Maximum HAND value in the domain (meters).
        mean_hand_m: Mean HAND value (meters).
        stream_cell_count: Number of stream cells (HAND ~ 0).
        artifacts: Intermediate TauDEM files produced during computation.
    """
    hand_path: Path
    hand_clipped_path: Optional[Path]
    min_hand_m: float
    max_hand_m: float
    mean_hand_m: float
    stream_cell_count: int
    artifacts: dict[str, Path]


def compute_hand(
    fel_path: Union[str, Path],
    src_path: Union[str, Path],
    output_dir: Union[str, Path],
    watershed_shape=None,
    taudem_executable_dir: Optional[Path] = None,
    taudem_processes: int = 1,
) -> HandResult:
    """Compute HAND raster from pit-filled DEM and stream source grid.

    Args:
        fel_path:     Pit-filled DEM (from TauDEM PitRemove, typically
                      watershed artifact 'fel').
        src_path:     Stream source grid (from TauDEM Threshold, typically
                      watershed artifact 'src').
        output_dir:   Directory to write HAND outputs.
        watershed_shape: Optional shapely geometry to clip HAND to the
                      delineated watershed boundary.
        taudem_executable_dir: Optional TauDEM executable directory override.
        taudem_processes: Number of TauDEM MPI processes.

    Returns:
        HandResult with paths to HAND rasters and summary statistics.
    """
    fel_path = Path(fel_path)
    src_path = Path(src_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not fel_path.exists():
        raise FileNotFoundError(f"Pit-filled DEM not found: {fel_path}")
    if not src_path.exists():
        raise FileNotFoundError(f"Stream source grid not found: {src_path}")

    TauDem.validate_environment(
        required=["DinfFlowDir", "DinfDistDown"],
        executable_dir=taudem_executable_dir,
    )

    ang_path = output_dir / "hand_ang.tif"
    slp_path = output_dir / "hand_slp.tif"
    hand_raw_path = output_dir / "hand_raw.tif"

    logger.info(
        "[HAND] Computing D-infinity flow directions from %s", fel_path.name
    )
    TauDem.dinf_flow_dir(
        fel_path, ang_path, slp_path,
        executable_dir=taudem_executable_dir,
        processes=taudem_processes,
    )

    logger.info("[HAND] Computing DinfDistDown (stat=min, dist=v) — HAND")
    TauDem.dinf_dist_down(
        angfile=ang_path,
        felfile=fel_path,
        srcfile=src_path,
        ddfile=hand_raw_path,
        stat_method="min",
        dist_method="v",
        executable_dir=taudem_executable_dir,
        processes=taudem_processes,
    )

    hand_cog_path = output_dir / "hand.tif"
    _to_cog(hand_raw_path, hand_cog_path)

    hand_clipped_path = None
    if watershed_shape is not None:
        hand_clipped_path = output_dir / "hand_clipped.tif"
        _clip_to_watershed(hand_cog_path, watershed_shape, hand_clipped_path)

    stats_path = hand_clipped_path or hand_cog_path
    stats = _compute_stats(stats_path)

    logger.info(
        "[CALC] HAND: min=%.2f m, max=%.2f m, mean=%.2f m, "
        "stream_cells=%d [%s]",
        stats["min"], stats["max"], stats["mean"], stats["stream_cells"],
        "VALID" if stats["max"] < 200 else "WARN: max HAND > 200 m"
    )

    return HandResult(
        hand_path=hand_cog_path,
        hand_clipped_path=hand_clipped_path,
        min_hand_m=stats["min"],
        max_hand_m=stats["max"],
        mean_hand_m=stats["mean"],
        stream_cell_count=stats["stream_cells"],
        artifacts={
            "ang": ang_path,
            "slp": slp_path,
            "hand_raw": hand_raw_path,
        },
    )


def mock_hand(output_dir: Union[str, Path]) -> HandResult:
    """Generate a synthetic HAND raster for mock/test mode."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    hand_path = output_dir / "hand.tif"

    if not hand_path.exists():
        rows, cols = 20, 20
        hand_data = np.zeros((rows, cols), dtype=np.float32)
        for r in range(rows):
            for c in range(cols):
                dist_to_center = abs(c - cols // 2)
                hand_data[r, c] = dist_to_center * 0.5
        from rasterio.transform import from_bounds
        transform = from_bounds(0, 0, 10000, 10000, cols, rows)
        with rasterio.open(
            hand_path, "w", driver="GTiff", height=rows, width=cols,
            count=1, dtype="float32", crs=TARGET_CRS,
            transform=transform, nodata=NODATA,
        ) as dst:
            dst.write(hand_data, 1)

    return HandResult(
        hand_path=hand_path,
        hand_clipped_path=None,
        min_hand_m=0.0,
        max_hand_m=4.75,
        mean_hand_m=2.5,
        stream_cell_count=20,
        artifacts={},
    )


def _to_cog(src_path: Path, dst_path: Path) -> None:
    """Re-encode a raster as a Cloud-Optimized GeoTIFF with overviews."""
    with rasterio.open(src_path) as src:
        profile = src.profile.copy()
        profile.update(**_COG_OPTIONS, nodata=NODATA)
        data = src.read(1)

    nodata_mask = ~np.isfinite(data) | (data < -1e6)
    data[nodata_mask] = NODATA

    with rasterio.open(dst_path, "w", **profile) as dst:
        dst.write(data, 1)
        safe_levels = [
            lv for lv in _OVERVIEW_LEVELS
            if dst.height // lv >= 1 and dst.width // lv >= 1
        ]
        if safe_levels:
            dst.build_overviews(safe_levels, rasterio.enums.Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")


def _clip_to_watershed(
    hand_path: Path, watershed_shape, output_path: Path
) -> None:
    """Clip HAND raster to watershed boundary polygon."""
    from shapely.geometry import mapping
    with rasterio.open(hand_path) as src:
        out_image, out_transform = rio_mask(
            src, [mapping(watershed_shape)], crop=True, nodata=NODATA,
        )
        out_meta = src.meta.copy()
        out_meta.update(
            height=out_image.shape[1],
            width=out_image.shape[2],
            transform=out_transform,
            nodata=NODATA,
            **_COG_OPTIONS,
        )
    with rasterio.open(output_path, "w", **out_meta) as dst:
        dst.write(out_image)
        safe_levels = [
            lv for lv in _OVERVIEW_LEVELS
            if dst.height // lv >= 1 and dst.width // lv >= 1
        ]
        if safe_levels:
            dst.build_overviews(safe_levels, rasterio.enums.Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")


def _compute_stats(hand_path: Path) -> dict:
    """Read HAND raster and compute summary statistics."""
    with rasterio.open(hand_path) as src:
        data = src.read(1)
    valid = data[(data != NODATA) & np.isfinite(data) & (data >= 0)]
    if valid.size == 0:
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "stream_cells": 0}
    stream_cells = int(np.sum(valid < 0.01))
    return {
        "min": float(valid.min()),
        "max": float(valid.max()),
        "mean": float(valid.mean()),
        "stream_cells": stream_cells,
    }


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="Compute HAND raster from TauDEM artifacts"
    )
    parser.add_argument("--fel", required=True, help="Pit-filled DEM")
    parser.add_argument("--src", required=True, help="Stream source grid")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--taudem-dir", default=None)
    parser.add_argument("--processes", type=int, default=1)
    args = parser.parse_args()

    result = compute_hand(
        fel_path=Path(args.fel),
        src_path=Path(args.src),
        output_dir=Path(args.output),
        taudem_executable_dir=Path(args.taudem_dir) if args.taudem_dir else None,
        taudem_processes=args.processes,
    )
    print(f"\nHAND computed:")
    print(f"  Output: {result.hand_path}")
    print(f"  Min:    {result.min_hand_m:.2f} m")
    print(f"  Max:    {result.max_hand_m:.2f} m")
    print(f"  Mean:   {result.mean_hand_m:.2f} m")
    print(f"  Stream cells: {result.stream_cell_count}")
