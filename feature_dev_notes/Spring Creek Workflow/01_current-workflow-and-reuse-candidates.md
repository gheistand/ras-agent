# Current Workflow And Reuse Candidates

## Spring Creek Workflow Target

The first real-world demonstration basin is the upstream drainage area to `USGS-05577500` `SPRING CREEK AT SPRINGFIELD, IL`.

Local research and staging already exist in:

- `workspace/Spring Creek Springfield IL/01_gauge`
- `workspace/Spring Creek Springfield IL/02_basin_outline`
- `workspace/Spring Creek Springfield IL/03_nhdplus`
- `workspace/Spring Creek Springfield IL/04_terrain`
- `workspace/Spring Creek Springfield IL/05_landcover_nlcd`
- `workspace/Spring Creek Springfield IL/06_soils`

The workflow intent is:

1. Start from an observed USGS gauge and official basin context.
2. Collect vector, terrain, land cover, and soils inputs.
3. Confirm or refine watershed boundaries through TauDEM preprocessing.
4. Convert those watershed outputs into HEC-RAS-ready instructions.
5. Use `ras-commander` to assemble, compile, run, and validate the model.

## Terrain Source Rule For Spring Creek

Use the terrain source already specified in `ras-agent` as the primary path.

Current `ras-agent` terrain behavior in `pipeline/terrain.py`:

- primary Illinois terrain source: ILHMP clearinghouse tile index
- module attribution: `Copyright 2026 Glenn Heistand / CHAMP — Illinois State Water Survey`
- primary source URL:
  - `https://clearinghouse.isgs.illinois.edu/arcgis/rest/services/Elevation/IL_Height_Modernization_DEM/MapServer/0/query`
- fallback source: USGS 3DEP via The National Map
- default working CRS: `EPSG:5070`
- default DEM resolution: `3.0` meters

Operational rule:

- when we execute terrain acquisition for this workflow, delegate that pull to a subagent
- use the CHAMP-attributed Illinois source defined in `ras-agent` first
- only use USGS 3DEP if the Illinois source is unavailable or incomplete for the requested extent

## What `ras-commander` Already Covers Well

The `ras_commander.usgs` package is already substantial and should be reused rather than reimplemented in `ras-agent`.

Confirmed reusable pieces:

- `ras_commander/usgs/core.py`
  - `RasUsgsCore.retrieve_flow_data`
  - `RasUsgsCore.retrieve_stage_data`
  - `RasUsgsCore.get_gauge_metadata`
  - `RasUsgsCore.check_data_availability`
- `ras_commander/usgs/catalog.py`
  - `UsgsGaugeCatalog.generate_gauge_catalog`
  - standardized `"USGS Gauge Data"` folder structure
  - metadata, historical CSVs, availability summaries, GeoJSON output
- `ras_commander/usgs/time_series.py`
  - resampling, alignment, and gap checks for model-ready time series
- `ras_commander/usgs/boundary_generation.py`
  - flow and stage hydrograph table generation for HEC-RAS boundary conditions
- `ras_commander/usgs/initial_conditions.py`
  - initial-condition extraction and formatting from observed data
- `ras_commander/usgs/metrics.py` and `visualization.py`
  - model validation metrics and graphics
- `ras_commander/usgs/gauge_matching.py`
  - cross section and 2D area matching after a HEC-RAS geometry already exists

Important limitation:

- the current `ras-commander` USGS workflow is project-centric and geometry-centric
- discovery and catalog generation currently expect a RAS project context, especially geometry HDF bounds
- that is strong for model validation and boundary setup, but weaker for the earlier basin-research stage we are in now

## What `hms-commander` Already Covers Well

Confirmed reusable pieces from this pass are narrower but still important.

- `hms_commander/HmsHuc.py`
  - `HmsHuc.get_huc12_for_bounds`
  - `HmsHuc.get_huc8_for_bounds`
  - `HmsHuc.get_huc_by_ids`
  - useful for standardized WBD/HUC lookup and download

What was not found in this pass:

- a project-grade USGS gauge data package comparable to `ras_commander.usgs`
- a basin-first gauge catalog / NWIS / NLDI orchestration layer in `hms-commander`

That means `hms-commander` currently looks more suitable for generalized hydrology-side preprocessing support than for full gauge-data packaging.

## Recommended Reuse Strategy For Spring Creek

Use `ras-commander` later in the workflow for:

- gauge metadata and observed flow/stage retrieval
- gauge data packaging for reports and model folders
- observed hydrograph preparation for boundary conditions
- initial conditions
- model-result validation against observed data

Use `hms-commander` earlier in the workflow for reusable hydro-preprocessing support such as:

- HUC lookup and standard watershed context
- future generalized NHDPlus / NLDI / TauDEM workflow helpers that should not live in `ras-agent`

Keep `ras-agent` focused on:

- Illinois-specific orchestration
- Spring Creek workflow assembly
- wiring shared-library outputs together
- documenting which upstream features are missing

## Practical Implication For This Basin

For Spring Creek, it will likely be better to delay deep custom USGS handling inside `ras-agent` and instead:

1. use current local research downloads for the basin workspace
2. reuse `ras-commander` USGS retrieval and packaging once the model folder and workflow contract are clearer
3. upstream missing basin-first helpers to `hms-commander` or `ras-commander` based on whether the missing capability is hydrology-side or HEC-RAS-side
4. keep terrain acquisition aligned with `ras-agent`'s Illinois-first source contract rather than inventing a separate terrain download path for this basin
