# End-To-End Checklist Seed

## Purpose

This is the first seed checklist for real-world Spring Creek workflow testing. It is not yet the final QA plan, but it is detailed enough to drive upcoming subagent work and issue discovery.

## 1. Gauge And Official Basin Context

- Confirm site metadata for `USGS-05577500`
- Confirm parameter availability for flow and stage
- Save periods of record and current metadata snapshot
- Confirm the official upstream basin geometry used as the baseline reference
- Confirm which HUC12 contains the gauge point
- Confirm which HUC12 polygons intersect the official upstream basin

## 2. Vector Context And Hydrography

- Save upstream flowlines for the basin from official services
- Save HUC12 and any larger HUC context used for reporting
- Record which official datasets are being treated as authoritative baseline context
- Record CRS for each downloaded vector dataset

## 3. Terrain, Land Cover, And Soils

- Delegate terrain download to a subagent when this step is executed
- Use the primary Illinois terrain source defined in `ras-agent/pipeline/terrain.py`
- Treat the ILHMP clearinghouse source attributed to CHAMP / Illinois State Water Survey as the first terrain source for this basin
- Use USGS 3DEP only as fallback if the Illinois source is unavailable or incomplete
- Download terrain for the full basin plus a small buffer
- Confirm terrain resolution, source, acquisition date, and projection
- Confirm terrain is processed to `EPSG:5070`
- Download NLCD land cover covering the same analysis extent
- Download soils data covering the same analysis extent
- Save clipped basin versions in the workflow CRS
- Record provenance for each raster/vector input

## 4. TauDEM Delineation Baseline

- Prepare TauDEM workspace inputs from the staged DEM and pour point
- Run the direct TauDEM workflow with preserved intermediate artifacts
- Save conditioned DEM, flow direction, accumulation, thresholded streams, snapped outlet, subbasins, and network outputs
- Record all TauDEM commands, thresholds, and assumptions
- Compare the TauDEM basin outline to the official basin outline
- Compare outlet snap location to the official gauge point

## 5. Boundary And Drainage-Area QA

- Compute official basin area
- Compute TauDEM basin area
- Capture official gauge drainage area from USGS metadata
- Compare official gauge drainage area to basin polygon area and TauDEM basin area
- Record acceptable tolerance for drainage-area mismatch

## 6. HEC-RAS Model Input Preparation

- Confirm the seed template project structure to use
- Define the watershed-derived 2D flow area perimeter
- Define terrain attachment and regeneration steps
- Define how land cover roughness will be mapped into the project
- Define how soils and infiltration instructions will be compiled into the project
- Confirm the required project, geometry, flow, and plan files for the first runnable model

## 7. Observed Data Integration

- Retrieve observed flow data for the modeling and validation windows
- Retrieve observed stage data if available and useful
- Resample observed data to model interval as needed
- Check and document data gaps
- Define how observed data will be used:
  - upstream inflow
  - downstream stage
  - initial conditions
  - validation only

## 8. Model Execution And Validation

- Run the first complete model with reproducible inputs
- Confirm geometry recompilation and any Windows-side regeneration steps
- Extract modeled time series at the gauge comparison location
- Align observed and modeled time series
- Compute validation metrics
- Generate review graphics for engineering inspection
- Record any model-data timing, peak, stage, or volume issues

## 9. Documentation And Upstreaming

- Record which steps were manual versus automated
- Record which steps belong in `ras-agent`
- Record which reusable gaps belong in `hms-commander`
- Record which reusable gaps belong in `ras-commander`
- Open GitHub issues in sibling repos for confirmed reusable gaps
- Link those issues back into `agent_tasks` once opened
