"""
aorc.py - NOAA AORC precipitation retrieval and HEC-RAS handoff package.

The module reads the public AORC v1.1 yearly Zarr stores from NOAA/NODD S3,
clips hourly accumulated precipitation to a WGS84 bounding box, writes
incremental GeoTIFF grids, and emits a DSS-ready catalog/manifest for later
HEC-RAS rain-on-grid setup.

It intentionally does not write binary HEC-DSS or inject HEC-RAS HDF content.
Those writer surfaces require HEC native libraries or ras-commander project
authoring and should remain outside this retrieval module.
"""

from __future__ import annotations

import csv
import itertools
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

import httpx
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin

logger = logging.getLogger(__name__)


AORC_BUCKET = "noaa-nws-aorc-v1-1-1km"
AORC_ZARR_BASE_URL = f"https://{AORC_BUCKET}.s3.amazonaws.com"
AORC_PRECIP_VARIABLE = "APCP_surface"
AORC_SCHEMA_VERSION = "ras-agent-aorc-precip/v1"
AORC_HECRAS_MANIFEST_VERSION = "ras-agent-aorc-hecras-manifest/v1"

PRECIP_UNITS = "mm"
PRECIP_NODATA = -9999.0
DEFAULT_DSS_A_PART = "AORC1K"
DEFAULT_DSS_B_PART = "RAS-AGENT"
DEFAULT_DSS_F_PART = "AORC"


class AORCError(RuntimeError):
    """Raised when AORC retrieval, decoding, or export fails."""


@dataclass(frozen=True)
class AORCTimeInterval:
    """One incremental precipitation grid interval."""

    start_time: datetime
    end_time: datetime
    source_valid_time: datetime
    temporal_method: str


@dataclass
class AORCPrecipitationResult:
    """Paths and metadata for a retrieved AORC precipitation package."""

    bbox_wgs84: tuple[float, float, float, float]
    start_time: datetime
    end_time: datetime
    time_step_minutes: int
    raster_paths: list[Path]
    event_total_path: Path
    catalog_csv_path: Path
    metadata_json_path: Path
    hecras_manifest_path: Path
    cache_dir: Path
    grid_shape: tuple[int, int]
    interval_count: int
    temporal_method: str
    source: str = "NOAA AORC v1.1"
    variable: str = AORC_PRECIP_VARIABLE


def _utc_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise AORCError(f"Invalid datetime value for AORC request: {value!r}") from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_timestamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_timestamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%MZ")


def _dss_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%d%b%Y:%H%M").upper()


def _validate_bbox(bbox_wgs84: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    if len(bbox_wgs84) != 4:
        raise AORCError("bbox_wgs84 must be a 4-tuple: (west, south, east, north)")
    west, south, east, north = (float(value) for value in bbox_wgs84)
    if not all(math.isfinite(value) for value in (west, south, east, north)):
        raise AORCError("bbox_wgs84 contains a non-finite coordinate")
    if west >= east or south >= north:
        raise AORCError("bbox_wgs84 must be ordered as west < east and south < north")
    if west < -180 or east > 180 or south < -90 or north > 90:
        raise AORCError("bbox_wgs84 must be in WGS84 longitude/latitude bounds")
    return west, south, east, north


def _years_for_window(start_time: datetime, end_time: datetime) -> range:
    return range(start_time.year, end_time.year + 1)


def _jsonable_path(path: Path) -> str:
    return str(Path(path))


class _CachedByteStore:
    """Read object keys from HTTP(S) or a local Zarr root with a file cache."""

    def __init__(self, base_url: str | Path, cache_dir: Path):
        self.base_url = str(base_url).rstrip("/\\")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        parsed = urlparse(self.base_url)
        self._scheme = parsed.scheme.lower()
        if self._scheme == "file":
            self._local_root = Path(unquote(parsed.path))
        elif self._scheme in ("http", "https"):
            self._local_root = None
        else:
            self._local_root = Path(self.base_url)

    def _cache_path(self, key: str) -> Path:
        parts = [part for part in key.replace("\\", "/").split("/") if part]
        return self.cache_dir.joinpath(*parts)

    def read(self, key: str) -> bytes:
        cache_path = self._cache_path(key)
        if cache_path.exists():
            return cache_path.read_bytes()

        if self._scheme in ("http", "https"):
            url = f"{self.base_url}/{key.lstrip('/')}"
            try:
                response = httpx.get(url, timeout=120, follow_redirects=True)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise FileNotFoundError(key) from exc
                raise AORCError(f"AORC object request failed: {url}") from exc
            except httpx.HTTPError as exc:
                raise AORCError(f"AORC object request failed: {url}") from exc
            data = response.content
        else:
            path = self._local_root / key
            if not path.exists():
                raise FileNotFoundError(str(path))
            data = path.read_bytes()

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)
        return data


class _AORCYearZarr:
    """Minimal Zarr v2 reader for small AORC bbox/time subsets."""

    def __init__(self, year: int, base_url: str | Path, cache_dir: Path):
        self.year = int(year)
        self.prefix = f"{self.year}.zarr"
        self.store = _CachedByteStore(base_url, cache_dir)
        self._metadata: dict[str, dict[str, Any]] = {}
        self._attrs: dict[str, dict[str, Any]] = {}

    def _read_json(self, key: str) -> dict[str, Any]:
        try:
            return json.loads(self.store.read(key).decode("utf-8"))
        except FileNotFoundError as exc:
            raise AORCError(f"AORC Zarr key not found: {key}") from exc

    def array_metadata(self, array_name: str) -> dict[str, Any]:
        if array_name not in self._metadata:
            self._metadata[array_name] = self._read_json(f"{self.prefix}/{array_name}/.zarray")
        return self._metadata[array_name]

    def array_attrs(self, array_name: str) -> dict[str, Any]:
        if array_name not in self._attrs:
            try:
                self._attrs[array_name] = self._read_json(f"{self.prefix}/{array_name}/.zattrs")
            except AORCError:
                self._attrs[array_name] = {}
        return self._attrs[array_name]

    def read_array(self, array_name: str, slices: tuple[slice, ...]) -> np.ndarray:
        metadata = self.array_metadata(array_name)
        shape = tuple(int(value) for value in metadata["shape"])
        chunks = tuple(int(value) for value in metadata["chunks"])
        dtype = np.dtype(metadata["dtype"])
        normalized = _normalize_slices(slices, shape)
        out_shape = tuple(item.stop - item.start for item in normalized)
        fill_value = metadata.get("fill_value", 0)
        output = np.empty(out_shape, dtype=dtype)
        output[...] = fill_value

        chunk_ranges = []
        for selected, chunk_size in zip(normalized, chunks):
            first = selected.start // chunk_size
            last = (selected.stop - 1) // chunk_size
            chunk_ranges.append(range(first, last + 1))

        for chunk_coords in itertools.product(*chunk_ranges):
            chunk = self._read_chunk(array_name, metadata, chunk_coords)
            chunk_slices = []
            out_slices = []
            for dim, chunk_index in enumerate(chunk_coords):
                chunk_start = chunk_index * chunks[dim]
                chunk_stop = chunk_start + chunk.shape[dim]
                selected = normalized[dim]
                overlap_start = max(selected.start, chunk_start)
                overlap_stop = min(selected.stop, chunk_stop)
                if overlap_start >= overlap_stop:
                    break
                chunk_slices.append(slice(overlap_start - chunk_start, overlap_stop - chunk_start))
                out_slices.append(slice(overlap_start - selected.start, overlap_stop - selected.start))
            else:
                output[tuple(out_slices)] = chunk[tuple(chunk_slices)]

        return output

    def read_coordinate(self, array_name: str) -> np.ndarray:
        metadata = self.array_metadata(array_name)
        shape = tuple(int(value) for value in metadata["shape"])
        return self.read_array(array_name, tuple(slice(0, dim) for dim in shape))

    def _read_chunk(
        self,
        array_name: str,
        metadata: dict[str, Any],
        chunk_coords: tuple[int, ...],
    ) -> np.ndarray:
        shape = tuple(int(value) for value in metadata["shape"])
        chunks = tuple(int(value) for value in metadata["chunks"])
        dtype = np.dtype(metadata["dtype"])
        fill_value = metadata.get("fill_value", 0)
        chunk_shape = tuple(
            min(chunks[dim], shape[dim] - chunk_coords[dim] * chunks[dim])
            for dim in range(len(shape))
        )
        key = f"{self.prefix}/{array_name}/" + ".".join(str(value) for value in chunk_coords)

        try:
            encoded = self.store.read(key)
        except FileNotFoundError:
            chunk = np.empty(chunk_shape, dtype=dtype)
            chunk[...] = fill_value
            return chunk

        decoded = _decode_chunk(encoded, metadata)
        expected_size = int(np.prod(chunk_shape))
        if decoded.size < expected_size:
            padded = np.empty(expected_size, dtype=dtype)
            padded[...] = fill_value
            padded[: decoded.size] = decoded
            decoded = padded
        elif decoded.size > expected_size:
            decoded = decoded[:expected_size]

        order = metadata.get("order", "C")
        return decoded.reshape(chunk_shape, order=order)


def _normalize_slices(slices: tuple[slice, ...], shape: tuple[int, ...]) -> tuple[slice, ...]:
    if len(slices) != len(shape):
        raise AORCError(f"Expected {len(shape)} slices, got {len(slices)}")
    normalized = []
    for item, dim in zip(slices, shape):
        start = 0 if item.start is None else int(item.start)
        stop = dim if item.stop is None else int(item.stop)
        step = 1 if item.step is None else int(item.step)
        if step != 1:
            raise AORCError("AORC Zarr reader only supports contiguous slices")
        if start < 0 or stop > dim or start >= stop:
            raise AORCError(f"Invalid Zarr slice {item} for dimension length {dim}")
        normalized.append(slice(start, stop))
    return tuple(normalized)


def _decode_chunk(encoded: bytes, metadata: dict[str, Any]) -> np.ndarray:
    compressor = metadata.get("compressor")
    dtype = np.dtype(metadata["dtype"])
    if compressor is None:
        raw = encoded
    elif compressor.get("id") == "zstd":
        try:
            import zstandard as zstd
        except ImportError as exc:
            raise AORCError(
                "AORC Zarr chunks use zstd compression. Install zstandard or "
                "use pipeline/requirements.txt before reading NOAA AORC S3 data."
            ) from exc
        raw = zstd.ZstdDecompressor().decompress(encoded)
    else:
        raise AORCError(f"Unsupported Zarr compressor: {compressor}")
    return np.frombuffer(raw, dtype=dtype)


def _coordinate_slice(coords: np.ndarray, lower: float, upper: float) -> slice:
    finite = np.asarray(coords, dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size < 2:
        raise AORCError("AORC coordinate array is empty or invalid")
    diffs = np.diff(np.sort(np.unique(finite)))
    diffs = np.abs(diffs[diffs != 0])
    half_step = float(np.median(diffs) / 2.0) if diffs.size else 0.0
    mask = (coords >= lower - half_step) & (coords <= upper + half_step)
    indexes = np.where(mask)[0]
    if indexes.size == 0:
        raise AORCError(
            f"Requested bbox coordinate range {lower:.6f}..{upper:.6f} "
            "does not intersect the AORC grid"
        )
    return slice(int(indexes.min()), int(indexes.max()) + 1)


def _datetime_from_epoch_seconds(value: float) -> datetime:
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _selected_time_indexes(times: np.ndarray, start_time: datetime, end_time: datetime) -> list[int]:
    selected = []
    for idx, value in enumerate(times):
        valid_time = _datetime_from_epoch_seconds(float(value))
        if start_time < valid_time <= end_time:
            selected.append(idx)
    return selected


def _scale_precipitation(raw: np.ndarray, metadata: dict[str, Any], attrs: dict[str, Any]) -> np.ndarray:
    fill_value = metadata.get("fill_value", attrs.get("missing_value", -32767))
    scale_factor = float(attrs.get("scale_factor", 1.0))
    data = raw.astype("float32")
    data[raw == fill_value] = np.nan
    data *= scale_factor
    data[data < 0] = np.nan
    return data


def _orient_grid(
    data: np.ndarray,
    latitudes: np.ndarray,
    longitudes: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    latitudes = np.asarray(latitudes, dtype=float)
    longitudes = np.asarray(longitudes, dtype=float)

    if longitudes[0] > longitudes[-1]:
        longitudes = longitudes[::-1]
        data = data[:, :, ::-1]
    if latitudes[0] < latitudes[-1]:
        latitudes = latitudes[::-1]
        data = data[:, ::-1, :]
    return data, latitudes, longitudes


def _grid_transform(latitudes: np.ndarray, longitudes: np.ndarray):
    if latitudes.size < 1 or longitudes.size < 1:
        raise AORCError("Cannot build raster transform for an empty grid")
    lon_res = _median_spacing(longitudes)
    lat_res = _median_spacing(latitudes)
    west_edge = float(np.min(longitudes) - lon_res / 2.0)
    north_edge = float(np.max(latitudes) + lat_res / 2.0)
    return from_origin(west_edge, north_edge, lon_res, lat_res)


def _median_spacing(values: np.ndarray) -> float:
    if values.size == 1:
        return 1.0 / 120.0
    diffs = np.abs(np.diff(np.asarray(values, dtype=float)))
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        return 1.0 / 120.0
    return float(np.median(diffs))


def resample_hourly_precipitation(
    data_mm: np.ndarray,
    hour_end_times: list[datetime],
    time_step_minutes: int = 60,
) -> tuple[np.ndarray, list[AORCTimeInterval], str]:
    """
    Convert hourly accumulated AORC grids to the requested regular interval.

    For sub-hourly intervals, each hourly accumulation is distributed uniformly
    across the substeps. This preserves the hourly/event total but does not
    recover true sub-hourly storm structure.
    """
    if time_step_minutes <= 0:
        raise AORCError("time_step_minutes must be positive")

    hour_end_times = [time.astimezone(timezone.utc) for time in hour_end_times]
    if len(hour_end_times) != data_mm.shape[0]:
        raise AORCError("hour_end_times length must match the time dimension")

    if time_step_minutes == 60:
        intervals = [
            AORCTimeInterval(
                start_time=end_time - timedelta(hours=1),
                end_time=end_time,
                source_valid_time=end_time,
                temporal_method="native_hourly",
            )
            for end_time in hour_end_times
        ]
        return data_mm.astype("float32"), intervals, "native_hourly"

    if time_step_minutes < 60:
        if 60 % time_step_minutes != 0:
            raise AORCError("Sub-hourly time_step_minutes must divide evenly into 60")
        substeps = 60 // time_step_minutes
        rows = []
        intervals = []
        dt = timedelta(minutes=time_step_minutes)
        for grid, end_time in zip(data_mm, hour_end_times):
            hour_start = end_time - timedelta(hours=1)
            for substep in range(substeps):
                start = hour_start + substep * dt
                end = start + dt
                rows.append((grid / substeps).astype("float32"))
                intervals.append(
                    AORCTimeInterval(
                        start_time=start,
                        end_time=end,
                        source_valid_time=end_time,
                        temporal_method="uniform_subhourly_disaggregation",
                    )
                )
        return np.stack(rows, axis=0), intervals, "uniform_subhourly_disaggregation"

    if time_step_minutes % 60 != 0:
        raise AORCError("Aggregated time_step_minutes must be a multiple of 60")
    hours_per_step = time_step_minutes // 60
    if data_mm.shape[0] % hours_per_step != 0:
        raise AORCError(
            "Hourly record count must be evenly divisible by the aggregation interval"
        )
    rows = []
    intervals = []
    for start_idx in range(0, data_mm.shape[0], hours_per_step):
        end_idx = start_idx + hours_per_step
        end_time = hour_end_times[end_idx - 1]
        start_time = hour_end_times[start_idx] - timedelta(hours=1)
        rows.append(np.nansum(data_mm[start_idx:end_idx], axis=0).astype("float32"))
        intervals.append(
            AORCTimeInterval(
                start_time=start_time,
                end_time=end_time,
                source_valid_time=end_time,
                temporal_method="multi_hour_sum",
            )
        )
    return np.stack(rows, axis=0), intervals, "multi_hour_sum"


def _dss_pathname(
    interval: AORCTimeInterval,
    *,
    a_part: str = DEFAULT_DSS_A_PART,
    b_part: str = DEFAULT_DSS_B_PART,
    f_part: str = DEFAULT_DSS_F_PART,
) -> str:
    return (
        f"/{a_part}/{b_part}/PRECIP/"
        f"{_dss_time(interval.start_time)}/{_dss_time(interval.end_time)}/{f_part}/"
    ).upper()


def _write_grid(
    path: Path,
    grid: np.ndarray,
    transform,
    crs: CRS,
    interval: Optional[AORCTimeInterval] = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = np.where(np.isnan(grid), PRECIP_NODATA, grid).astype("float32")
    profile = {
        "driver": "GTiff",
        "height": output.shape[0],
        "width": output.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": crs,
        "transform": transform,
        "nodata": PRECIP_NODATA,
        "compress": "lzw",
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(output, 1)
        tags = {"units": PRECIP_UNITS, "source": "NOAA AORC v1.1"}
        if interval is not None:
            tags.update(
                {
                    "interval_start_utc": _format_timestamp(interval.start_time),
                    "interval_end_utc": _format_timestamp(interval.end_time),
                    "source_valid_time_utc": _format_timestamp(interval.source_valid_time),
                    "temporal_method": interval.temporal_method,
                }
            )
        dst.update_tags(**tags)
    return path


def _write_catalog(
    path: Path,
    intervals: list[AORCTimeInterval],
    raster_paths: list[Path],
    *,
    dss_a_part: str,
    dss_b_part: str,
    dss_f_part: str,
) -> list[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for interval, raster_path in zip(intervals, raster_paths):
        rows.append(
            {
                "start_time_utc": _format_timestamp(interval.start_time),
                "end_time_utc": _format_timestamp(interval.end_time),
                "source_valid_time_utc": _format_timestamp(interval.source_valid_time),
                "depth_mm_tif": str(raster_path),
                "dss_pathname": _dss_pathname(
                    interval,
                    a_part=dss_a_part,
                    b_part=dss_b_part,
                    f_part=dss_f_part,
                ),
                "temporal_method": interval.temporal_method,
                "units": PRECIP_UNITS,
            }
        )
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)
    return rows


def _write_metadata(
    path: Path,
    *,
    bbox_wgs84: tuple[float, float, float, float],
    start_time: datetime,
    end_time: datetime,
    time_step_minutes: int,
    temporal_method: str,
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    raster_paths: list[Path],
    event_total_path: Path,
    catalog_path: Path,
    cache_dir: Path,
) -> dict[str, Any]:
    metadata = {
        "schema_version": AORC_SCHEMA_VERSION,
        "source": {
            "provider": "NOAA Office of Water Prediction",
            "dataset": "Analysis of Record for Calibration v1.1",
            "bucket": AORC_BUCKET,
            "base_url": AORC_ZARR_BASE_URL,
            "variable": AORC_PRECIP_VARIABLE,
            "variable_long_name": "Total Precipitation",
            "native_units": "kg/m^2",
            "output_units": PRECIP_UNITS,
            "temporal_resolution": "1 hour",
            "crs": "EPSG:4326",
        },
        "request": {
            "bbox_wgs84": list(bbox_wgs84),
            "start_time_utc": _format_timestamp(start_time),
            "end_time_utc": _format_timestamp(end_time),
            "time_step_minutes": time_step_minutes,
            "time_selection": "AORC hourly accumulation records with start_time < valid_time <= end_time",
        },
        "processing": {
            "temporal_method": temporal_method,
            "subhourly_note": (
                "Sub-hourly outputs distribute each hourly accumulation uniformly; "
                "they preserve total depth but do not add observed sub-hourly variability."
            ),
            "cache_dir": str(cache_dir),
        },
        "grid": {
            "height": int(latitudes.size),
            "width": int(longitudes.size),
            "north": float(np.max(latitudes)),
            "south": float(np.min(latitudes)),
            "west": float(np.min(longitudes)),
            "east": float(np.max(longitudes)),
        },
        "artifacts": {
            "incremental_depth_mm_tifs": [_jsonable_path(path) for path in raster_paths],
            "event_total_mm_tif": _jsonable_path(event_total_path),
            "catalog_csv": _jsonable_path(catalog_path),
            "metadata_json": _jsonable_path(path),
        },
    }
    path.write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding="utf-8")
    return metadata


def _write_hecras_manifest(
    path: Path,
    *,
    metadata: dict[str, Any],
    catalog_rows: list[dict[str, Any]],
    dss_a_part: str,
    dss_b_part: str,
    dss_f_part: str,
) -> dict[str, Any]:
    manifest = {
        "schema_version": AORC_HECRAS_MANIFEST_VERSION,
        "source_metadata": metadata,
        "hec_ras_handoff": {
            "target": "rain_on_grid",
            "binary_dss_written": False,
            "dss_pathname_pattern": f"/{dss_a_part}/{dss_b_part}/PRECIP/<START>/<END>/{dss_f_part}/".upper(),
            "reason": (
                "ras-agent writes a DSS-ready grid catalog and GeoTIFF stack. "
                "Binary HEC-DSS grid creation requires HEC native tooling, "
                "HEC-MetVue/GridUtil, or ras-commander project authoring."
            ),
            "preferred_next_step": (
                "Import the cataloged grids into HEC-DSS with HEC tooling or "
                "consume this manifest from ras-commander rain-on-grid setup."
            ),
        },
        "records": catalog_rows,
    }
    path.write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
    return manifest


def retrieve_aorc_precipitation(
    bbox_wgs84: tuple[float, float, float, float],
    start_time: datetime | str,
    end_time: datetime | str,
    output_dir: Path,
    *,
    time_step_minutes: int = 60,
    cache_dir: Optional[Path] = None,
    base_url: str | Path = AORC_ZARR_BASE_URL,
    dss_a_part: str = DEFAULT_DSS_A_PART,
    dss_b_part: str = DEFAULT_DSS_B_PART,
    dss_f_part: str = DEFAULT_DSS_F_PART,
) -> AORCPrecipitationResult:
    """
    Retrieve AORC precipitation grids for a bbox and event time window.

    Args:
        bbox_wgs84: (west, south, east, north) in WGS84 decimal degrees.
        start_time: Event start. AORC hourly records are selected when
            start_time < valid_time <= end_time because APCP is an accumulation
            ending at the valid time.
        end_time: Event end.
        output_dir: Directory for grids, catalog, metadata, and default cache.
        time_step_minutes: 60 for native hourly output; sub-hourly divisors of
            60 distribute hourly depth uniformly; larger multiples aggregate
            hourly grids by summation.
        cache_dir: Optional persistent cache for Zarr metadata and chunks.
        base_url: NOAA S3 HTTPS URL or a local Zarr root, used by tests/offline
            mirrors.
        dss_a_part: A part used in DSS pathname catalog records.
        dss_b_part: B part used in DSS pathname catalog records.
        dss_f_part: F part used in DSS pathname catalog records.

    Returns:
        AORCPrecipitationResult with written artifact paths.
    """
    bbox = _validate_bbox(tuple(bbox_wgs84))
    start_dt = _utc_datetime(start_time)
    end_dt = _utc_datetime(end_time)
    if end_dt <= start_dt:
        raise AORCError("end_time must be after start_time")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache = Path(cache_dir) if cache_dir is not None else output_dir / "cache"
    west, south, east, north = bbox

    hourly_data: list[np.ndarray] = []
    hourly_times: list[datetime] = []
    lat_subset: Optional[np.ndarray] = None
    lon_subset: Optional[np.ndarray] = None

    for year in _years_for_window(start_dt, end_dt):
        year_store = _AORCYearZarr(year, base_url, cache)
        try:
            time_values = year_store.read_coordinate("time")
        except AORCError:
            logger.debug("AORC year store not available for %s", year)
            continue

        selected = _selected_time_indexes(time_values, start_dt, end_dt)
        if not selected:
            continue

        latitudes = year_store.read_coordinate("latitude")
        longitudes = year_store.read_coordinate("longitude")
        lat_slice = _coordinate_slice(latitudes, south, north)
        lon_slice = _coordinate_slice(longitudes, west, east)
        time_slice = slice(min(selected), max(selected) + 1)

        raw = year_store.read_array(
            AORC_PRECIP_VARIABLE,
            (time_slice, lat_slice, lon_slice),
        )
        relative_indexes = [idx - time_slice.start for idx in selected]
        raw = raw[relative_indexes, :, :]
        metadata = year_store.array_metadata(AORC_PRECIP_VARIABLE)
        attrs = year_store.array_attrs(AORC_PRECIP_VARIABLE)
        data_mm = _scale_precipitation(raw, metadata, attrs)

        selected_times = [
            _datetime_from_epoch_seconds(float(time_values[idx]))
            for idx in selected
        ]
        hourly_data.append(data_mm)
        hourly_times.extend(selected_times)

        if lat_subset is None:
            lat_subset = np.asarray(latitudes[lat_slice], dtype=float)
            lon_subset = np.asarray(longitudes[lon_slice], dtype=float)

    if not hourly_data or lat_subset is None or lon_subset is None:
        raise AORCError(
            "No AORC hourly precipitation records intersected the requested "
            "bbox/time window"
        )

    data = np.concatenate(hourly_data, axis=0)
    order = np.argsort(np.asarray([time.timestamp() for time in hourly_times]))
    data = data[order, :, :]
    hourly_times = [hourly_times[int(idx)] for idx in order]
    data, lat_subset, lon_subset = _orient_grid(data, lat_subset, lon_subset)

    resampled, intervals, temporal_method = resample_hourly_precipitation(
        data,
        hourly_times,
        time_step_minutes=time_step_minutes,
    )

    raster_dir = output_dir / "aorc_grids"
    crs = CRS.from_epsg(4326)
    transform = _grid_transform(lat_subset, lon_subset)
    raster_paths = []
    for grid, interval in zip(resampled, intervals):
        path = raster_dir / f"aorc_apcp_{_file_timestamp(interval.end_time)}.tif"
        raster_paths.append(_write_grid(path, grid, transform, crs, interval))

    all_missing = np.all(np.isnan(resampled), axis=0)
    event_total = np.nansum(resampled, axis=0).astype("float32")
    event_total[all_missing] = np.nan
    event_total_path = output_dir / "aorc_event_total_mm.tif"
    _write_grid(event_total_path, event_total, transform, crs, None)

    catalog_path = output_dir / "aorc_hecras_dss_catalog.csv"
    rows = _write_catalog(
        catalog_path,
        intervals,
        raster_paths,
        dss_a_part=dss_a_part,
        dss_b_part=dss_b_part,
        dss_f_part=dss_f_part,
    )
    metadata_path = output_dir / "aorc_metadata.json"
    metadata = _write_metadata(
        metadata_path,
        bbox_wgs84=bbox,
        start_time=start_dt,
        end_time=end_dt,
        time_step_minutes=time_step_minutes,
        temporal_method=temporal_method,
        latitudes=lat_subset,
        longitudes=lon_subset,
        raster_paths=raster_paths,
        event_total_path=event_total_path,
        catalog_path=catalog_path,
        cache_dir=cache,
    )
    manifest_path = output_dir / "aorc_hecras_manifest.json"
    _write_hecras_manifest(
        manifest_path,
        metadata=metadata,
        catalog_rows=rows,
        dss_a_part=dss_a_part,
        dss_b_part=dss_b_part,
        dss_f_part=dss_f_part,
    )

    logger.info(
        "AORC precipitation package written to %s (%s grids, %s)",
        output_dir,
        len(raster_paths),
        temporal_method,
    )
    return AORCPrecipitationResult(
        bbox_wgs84=bbox,
        start_time=start_dt,
        end_time=end_dt,
        time_step_minutes=time_step_minutes,
        raster_paths=raster_paths,
        event_total_path=event_total_path,
        catalog_csv_path=catalog_path,
        metadata_json_path=metadata_path,
        hecras_manifest_path=manifest_path,
        cache_dir=cache,
        grid_shape=(int(lat_subset.size), int(lon_subset.size)),
        interval_count=len(intervals),
        temporal_method=temporal_method,
    )


def load_aorc_metadata(path: Path) -> dict[str, Any]:
    """Load a written AORC metadata JSON artifact."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    parser = argparse.ArgumentParser(description="Retrieve NOAA AORC precipitation grids")
    parser.add_argument("--west", type=float, required=True)
    parser.add_argument("--south", type=float, required=True)
    parser.add_argument("--east", type=float, required=True)
    parser.add_argument("--north", type=float, required=True)
    parser.add_argument("--start", required=True, help="Event start time, e.g. 2024-07-14T00:00Z")
    parser.add_argument("--end", required=True, help="Event end time, e.g. 2024-07-15T00:00Z")
    parser.add_argument("--output", required=True)
    parser.add_argument("--time-step-minutes", type=int, default=60)
    args = parser.parse_args()

    result = retrieve_aorc_precipitation(
        (args.west, args.south, args.east, args.north),
        args.start,
        args.end,
        Path(args.output),
        time_step_minutes=args.time_step_minutes,
    )
    print(f"AORC package ready: {result.metadata_json_path}")
