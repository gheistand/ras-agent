"""
Tests for NOAA AORC precipitation retrieval.

The tests use a tiny local Zarr v2 store so they do not make network requests
or require zstd support.
"""

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

import aorc


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _write_zarr_array(
    root: Path,
    name: str,
    array: np.ndarray,
    *,
    chunks: tuple[int, ...],
    attrs: dict | None = None,
    fill_value=None,
) -> None:
    array_dir = root / name
    array_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "chunks": list(chunks),
        "compressor": None,
        "dtype": np.dtype(array.dtype).str,
        "fill_value": fill_value,
        "filters": None,
        "order": "C",
        "shape": list(array.shape),
        "zarr_format": 2,
    }
    (array_dir / ".zarray").write_text(json.dumps(metadata), encoding="utf-8")
    (array_dir / ".zattrs").write_text(json.dumps(attrs or {}), encoding="utf-8")

    chunk_ranges = [
        range((array.shape[dim] + chunks[dim] - 1) // chunks[dim])
        for dim in range(array.ndim)
    ]
    for coords in np.ndindex(*(len(rng) for rng in chunk_ranges)):
        slices = []
        for dim, chunk_index in enumerate(coords):
            start = chunk_index * chunks[dim]
            stop = min(start + chunks[dim], array.shape[dim])
            slices.append(slice(start, stop))
        chunk = np.ascontiguousarray(array[tuple(slices)])
        key = ".".join(str(value) for value in coords)
        (array_dir / key).write_bytes(chunk.tobytes(order="C"))


def _write_fake_aorc_store(root: Path) -> Path:
    zarr_root = root / "2020.zarr"
    zarr_root.mkdir(parents=True, exist_ok=True)
    (zarr_root / ".zgroup").write_text(json.dumps({"zarr_format": 2}), encoding="utf-8")
    (zarr_root / ".zattrs").write_text("{}", encoding="utf-8")

    times = np.array(
        [
            _epoch("2020-01-01T01:00:00Z"),
            _epoch("2020-01-01T02:00:00Z"),
            _epoch("2020-01-01T03:00:00Z"),
        ],
        dtype="<f8",
    )
    latitudes = np.array([39.99, 40.00], dtype="<f8")
    longitudes = np.array([-90.00, -89.99, -89.98], dtype="<f8")
    precip = np.array(
        [
            [[10, 20, 30], [40, 50, 60]],
            [[15, 25, 35], [45, 55, 65]],
            [[20, 30, 40], [50, 60, 70]],
        ],
        dtype="<i2",
    )

    _write_zarr_array(
        zarr_root,
        "time",
        times,
        chunks=(3,),
        attrs={"units": "seconds since 1970-01-01"},
        fill_value=-32767.0,
    )
    _write_zarr_array(
        zarr_root,
        "latitude",
        latitudes,
        chunks=(2,),
        attrs={"units": "degrees_north"},
        fill_value=-32767.0,
    )
    _write_zarr_array(
        zarr_root,
        "longitude",
        longitudes,
        chunks=(3,),
        attrs={"units": "degrees_east"},
        fill_value=-32767.0,
    )
    _write_zarr_array(
        zarr_root,
        "APCP_surface",
        precip,
        chunks=(2, 2, 2),
        attrs={
            "long_name": "Total Precipitation",
            "missing_value": -32767,
            "scale_factor": 0.1,
            "units": "kg/m^2",
        },
        fill_value=-32767,
    )
    return root


def test_retrieve_aorc_precipitation_writes_geotiffs_catalog_and_manifest(tmp_path: Path):
    source = _write_fake_aorc_store(tmp_path / "source")

    result = aorc.retrieve_aorc_precipitation(
        (-90.005, 39.985, -89.985, 40.005),
        "2020-01-01T00:00:00Z",
        "2020-01-01T03:00:00Z",
        tmp_path / "out",
        base_url=source,
        dss_b_part="SPRING-CREEK",
    )

    assert result.interval_count == 3
    assert result.grid_shape == (2, 3)
    assert result.temporal_method == "native_hourly"
    assert result.catalog_csv_path.exists()
    assert result.hecras_manifest_path.exists()
    assert result.metadata_json_path.exists()

    with rasterio.open(result.raster_paths[0]) as src:
        data = src.read(1)
        assert src.crs.to_epsg() == 4326
        assert data.shape == (2, 3)
        assert np.isclose(data[0, 0], 4.0)
        assert src.tags()["interval_end_utc"] == "2020-01-01T01:00:00Z"

    catalog = result.catalog_csv_path.read_text(encoding="utf-8")
    assert "/AORC1K/SPRING-CREEK/PRECIP/01JAN2020:0000/01JAN2020:0100/AORC/" in catalog

    manifest = json.loads(result.hecras_manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "ras-agent-aorc-hecras-manifest/v1"
    assert manifest["hec_ras_handoff"]["binary_dss_written"] is False
    assert len(manifest["records"]) == 3


def test_retrieve_aorc_precipitation_uses_cached_chunks(tmp_path: Path):
    source = _write_fake_aorc_store(tmp_path / "source")
    cache_dir = tmp_path / "cache"

    first = aorc.retrieve_aorc_precipitation(
        (-90.005, 39.985, -89.985, 40.005),
        datetime(2020, 1, 1, 0, tzinfo=timezone.utc),
        datetime(2020, 1, 1, 2, tzinfo=timezone.utc),
        tmp_path / "out1",
        base_url=source,
        cache_dir=cache_dir,
    )
    assert first.interval_count == 2

    shutil.rmtree(source)
    second = aorc.retrieve_aorc_precipitation(
        (-90.005, 39.985, -89.985, 40.005),
        "2020-01-01T00:00:00Z",
        "2020-01-01T02:00:00Z",
        tmp_path / "out2",
        base_url=source,
        cache_dir=cache_dir,
    )

    assert second.interval_count == 2
    with rasterio.open(second.event_total_path) as src:
        total = src.read(1)
    assert np.isclose(total[0, 0], 8.5)


def test_subhourly_disaggregation_preserves_hourly_depth(tmp_path: Path):
    source = _write_fake_aorc_store(tmp_path / "source")

    result = aorc.retrieve_aorc_precipitation(
        (-90.005, 39.985, -89.985, 40.005),
        "2020-01-01T00:00:00Z",
        "2020-01-01T01:00:00Z",
        tmp_path / "out",
        base_url=source,
        time_step_minutes=15,
    )

    assert result.interval_count == 4
    assert result.temporal_method == "uniform_subhourly_disaggregation"

    pieces = []
    for path in result.raster_paths:
        with rasterio.open(path) as src:
            pieces.append(src.read(1))
            assert src.tags()["temporal_method"] == "uniform_subhourly_disaggregation"
    assert np.isclose(np.sum(pieces, axis=0)[0, 0], 4.0)


def test_aorc_rejects_time_window_without_records(tmp_path: Path):
    source = _write_fake_aorc_store(tmp_path / "source")

    try:
        aorc.retrieve_aorc_precipitation(
            (-90.005, 39.985, -89.985, 40.005),
            "2020-01-02T00:00:00Z",
            "2020-01-02T03:00:00Z",
            tmp_path / "out",
            base_url=source,
        )
    except aorc.AORCError as exc:
        assert "No AORC hourly precipitation records" in str(exc)
    else:
        raise AssertionError("Expected AORCError")
