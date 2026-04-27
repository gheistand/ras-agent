# GSSURGO Workflow For HEC-RAS

Last revised: 2026-04-21

## Why this needs to change

The current Spring Creek soils artifact is an informational SSURGO polygon clip. That is useful for reporting, but it is not the right handoff for HEC-RAS soils-layer import.

HEC-RAS 7.0 supports two soils-entry paths:

- Shapefile input, where the user is responsible for joining the right tabular attributes before import.
- GSSURGO file geodatabase input, which RAS Mapper recognizes directly and exposes classifications such as Hydrologic Group, Texture Group, Map Unit Key, and Map Unit Symbol.

That means the current `ssurgo_mapunitpoly_*.geojson` workflow should remain a reporting artifact, not the model-import artifact.

## Current external constraints

- The HEC-RAS 7.0 soils-layer page still points users to the NRCS Geospatial Data Gateway for GSSURGO, but USDA retired that service on March 31, 2026.
- USDA now publishes gSSURGO downloads through the NRCS Box-hosted distribution page and continues to publish SSURGO through Web Soil Survey / SSURGO Portal.
- gSSURGO is distributed as a statewide or CONUS ESRI file geodatabase and includes the source SSURGO tables plus the `Valu1` ready-to-map table.

## Proposed ras-agent workflow

1. Keep a shared buffered analysis extent as the authoritative informational extent.
2. Continue generating lightweight reporting artifacts from that extent:
   - `nlcd_2021_analysis_extent.tif`
   - `ssurgo_mapunitpoly_analysis_extent.geojson`
   - `USGS_05577500_upstream_flowlines_analysis_extent.geojson`
3. Add a separate GSSURGO acquisition path for model import:
   - Prefer a cached Illinois statewide gSSURGO file geodatabase.
   - Allow an override path from config or environment so the repo does not hard-code one workstation layout.
4. Subset the Illinois gSSURGO geodatabase to the shared analysis extent while preserving the geodatabase structure HEC-RAS expects.
5. Write the subset to a workspace-local handoff folder such as:
   - `06_soils/gssurgo/hec_ras_import.gdb`
   - `06_soils/gssurgo/summary.json`
   - `06_soils/gssurgo/README.md`
6. Preserve both the direct-import geodatabase and a flattened QA view:
   - direct import: geodatabase for RAS Mapper
   - QA view: GeoJSON / CSV summaries for report figures and engineering review
7. Pass the geodatabase handoff into `ras-commander` once headless soils-layer creation is available there.

## Required preprocessing behavior

The preprocessing step should be conservative. The point is to preserve the GSSURGO structure that HEC-RAS can ingest, not to flatten the data prematurely.

Required outputs:

- Workspace-local geodatabase subset clipped to the buffered analysis extent.
- Manifest entries that record:
  - statewide source dataset
  - download date
  - subset extent
  - retained feature classes / tables
  - workspace output paths
- QA summary including:
  - mukey count
  - musym count
  - hydrologic-group coverage
  - any records that resolve to `none` or null classification

## Implementation notes

- Do not keep using a soils WFS polygon clip as the model-import endpoint.
- Do not convert GSSURGO to a shapefile if the target is HEC-RAS import. That throws away the structure RAS Mapper already knows how to interpret.
- Keep the reporting layer and the model-import layer separate. They solve different problems.
- The reusable headless "create soils layer in HEC-RAS" capability still belongs in `ras-commander`, consistent with Issue #47.

## Suggested next coding steps

1. Add config for a cached Illinois gSSURGO source path or download URL.
2. Build a geodatabase subsetting helper that clips by the shared analysis extent and writes a workspace-local `.gdb`.
3. Add manifest + summary generation for the subset.
4. Leave the current SSURGO GeoJSON path in place for reports until the GSSURGO handoff is fully wired into `ras-commander`.

## References

- [HEC-RAS 2D User's Manual: Creating a Soils Data Layer](https://www.hec.usace.army.mil/confluence/rasdocs/r2dum/7.0/developing-a-terrain-model-and-geospatial-layers/creating-a-soils-data-layer)
- [USDA NRCS: Gridded Soil Survey Geographic (gSSURGO) Database](https://www.nrcs.usda.gov/resources/data-and-reports/gridded-soil-survey-geographic-gssurgo-database)
- [USDA NRCS: SSURGO Portal](https://www.nrcs.usda.gov/resources/data-and-reports/ssurgo-portal)
- [USDA NRCS: Geospatial Data Gateway retirement notice](https://gdg.sc.egov.usda.gov/index.html)
