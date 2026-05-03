# AORC Precipitation Retrieval

`pipeline/aorc.py` retrieves NOAA Analysis of Record for Calibration (AORC)
hourly gridded precipitation for rain-on-grid forcing review packages.

## Data Source

Primary access is the NOAA Open Data Dissemination public S3 bucket:

- Registry: <https://registry.opendata.aws/noaa-nws-aorc/>
- Bucket: `s3://noaa-nws-aorc-v1-1-1km`
- HTTPS root: <https://noaa-nws-aorc-v1-1-1km.s3.amazonaws.com/>
- Variable used by ras-agent: `APCP_surface`

The bucket is organized as yearly Zarr v2 stores such as `2024.zarr/`.
The precipitation chunks are zstd-compressed integer grids with a scale factor
of `0.1`, so ras-agent decodes them with `zstandard` and writes output depths
in millimeters.

NOAA also publishes legacy direct-download AORC V1.1 directories under
<https://hydrology.nws.noaa.gov/pub/AORC/V1.1/>. Those directories include
RFC-oriented 4 km precipitation/temperature products and selected 1 km regional
archives. They are useful as a reference/legacy path, but the public yearly Zarr
bucket is the preferred source for bbox/time-window retrieval.

No official 1 km CONUS THREDDS endpoint was identified during this task. Use
the NOAA/NODD S3 Zarr data unless a project specifically requires a legacy RFC
archive.

## Resolution, Coverage, and Timing

NOAA describes AORC v1.1 as a near-surface meteorological record covering the
continental United States and Alaska plus hydrologically contributing adjacent
areas. It is on a WGS84 latitude/longitude grid at roughly 30 arc seconds
(about 800 m to 1 km depending on latitude) and hourly time resolution.

For CONUS, the record begins in 1979 and extends to near-present. NOAA notes
that AORC updates run with about a 10-day lag so late corrections to input
Stage IV and NLDAS data can be included.

`APCP_surface` is one-hour accumulated surface precipitation. The timestamp is
the valid time at the end of the accumulation interval. For an event window
`start_time` to `end_time`, ras-agent selects records where:

```text
start_time < AORC valid_time <= end_time
```

## ras-agent Output

`retrieve_aorc_precipitation()` writes:

- `aorc_grids/aorc_apcp_<YYYYMMDDTHHMMZ>.tif`: incremental precipitation grids
  in millimeters.
- `aorc_event_total_mm.tif`: event-total precipitation depth.
- `aorc_hecras_dss_catalog.csv`: one row per grid with a DSS-style pathname.
- `aorc_metadata.json`: source, request, grid, cache, and artifact metadata.
- `aorc_hecras_manifest.json`: HEC-RAS rain-on-grid handoff manifest.

The module writes a DSS-ready catalog and GeoTIFF stack, not a binary `.dss`
file. HEC-DSS grid writing requires HEC native tooling, HEC-MetVue/GridUtil, or
project-authoring support in `ras-commander`. Direct HEC-RAS HDF injection is
intentionally avoided here because repo guidance treats HDF direct writes as
experimental scaffolding rather than the target architecture.

## Temporal Resampling

Native AORC precipitation is hourly. When a sub-hourly interval is requested,
ras-agent distributes each hourly depth uniformly across the substeps. This
preserves the hourly and event totals, but it does not recover observed
sub-hourly storm variability. Treat sub-hourly AORC grids as a numerical
timestep convenience, not as a higher-resolution rainfall observation.

When a multi-hour interval is requested, ras-agent sums whole hourly grids. The
hourly record count must divide evenly into the requested interval.

## Caching

The downloader caches Zarr metadata and chunks under `output_dir/cache` by
default, or under a caller-supplied `cache_dir`. Repeated bbox/time requests can
reuse cached chunks and avoid re-downloading NOAA objects.

## Known Limitations

- AORC is retrospective/calibration forcing, not a true real-time product.
- Recent data have an expected lag.
- NOAA posted an April 2, 2026 notice that a small fraction of Zarr rows had
  masking issues and that corrected Zarr files were being regenerated.
- The retrieval module clips by grid cells intersecting the bbox; model-domain
  buffering should happen before calling it.
- Binary DSS creation and RAS project wiring remain downstream responsibilities.
