# Feature Gaps And Issue Candidates

## Purpose

This file is a staging area for reusable feature requests exposed by the Spring Creek workflow.

These are not the system of record. If work belongs in `ras-commander` or `hms-commander`, open a GitHub issue there and add the issue link back into these notes and `agent_tasks`.

## Open Issue Links

- `hms-commander` gauge-first watershed study builder: https://github.com/gpt-cmdr/hms-commander/issues/2
- `hms-commander` workspace organizer and manifest builder: https://github.com/gpt-cmdr/hms-commander/issues/3
- `hms-commander` hydrology-side report and data-gap generator: https://github.com/gpt-cmdr/hms-commander/issues/4
- `hms-commander` TauDEM example-workflow/input-pack support: https://github.com/gpt-cmdr/hms-commander/issues/5
- `ras-commander` basin-first USGS study package: https://github.com/gpt-cmdr/ras-commander/issues/35
- `ras-commander` drainage-area comparison utility: https://github.com/gpt-cmdr/ras-commander/issues/36
- `ras-commander` model-side report and data-gap generator: https://github.com/gpt-cmdr/ras-commander/issues/37
- `ras-commander` geometry-first 2D flow area writer: https://github.com/gpt-cmdr/ras-commander/issues/38
- `ras-agent` local issue tracker status: GitHub issues are disabled in `gpt-cmdr/ras-agent` as of 2026-04-16, so repo-local items remain tracked in `agent_tasks` until issues are enabled.

## `ras-commander` Issue Candidates

### 1. Basin-first USGS catalog without requiring an initialized RAS project

Current state:

- `UsgsGaugeCatalog.generate_gauge_catalog()` is built around `RasPrj` and geometry-derived project bounds

Gap:

- early workflow research starts from a gauge, basin polygon, or HUC set before a HEC-RAS project exists

Requested capability:

- allow catalog generation from an explicit polygon, bounding box, gauge list, or output folder without requiring `init_ras_project()`

Why it belongs there:

- gauge cataloging, NWIS retrieval, report packaging, and boundary-condition prep are USGS/HEC-RAS support functions already centered in `ras-commander`

### 2. Gauge package builder for model setup and reporting

Current state:

- `ras-commander` can retrieve data, cache data, generate boundary tables, and compute metrics

Gap:

- there is not yet one obvious basin-to-report workflow that bundles:
  - site metadata
  - flow and stage periods of record
  - drainage area comparison
  - hydrograph plots
  - validation graphics
  - boundary-condition-ready exports

Requested capability:

- a higher-level gauge package builder for downstream engineering review and model folder organization

### 3. Drainage-area comparison utility

Current state:

- gauge metadata includes drainage area when available

Gap:

- the Spring Creek workflow needs a standard comparison between:
  - official gauge drainage area
  - official basin polygon area
  - TauDEM-delineated basin area
  - model 2D flow area footprint

Requested capability:

- one reusable utility in `ras-commander` that compares observed gauge drainage area to model and watershed geometry areas and writes a short QA summary

### 4. Cleaner handoff from observed data to boundary condition files

Current state:

- lower-level pieces exist for flow/stage boundary tables and initial conditions

Gap:

- the workflow still needs a clearer top-level API for:
  - select gauge
  - select simulation window
  - choose flow or stage usage
  - generate boundary/IC outputs
  - update target project files

Requested capability:

- one orchestration helper that turns a gauge and simulation contract into ready-to-review updates for model inputs

## `hms-commander` Issue Candidates

### 1. Gauge-first basin bootstrap helper

Current state:

- `HmsHuc` helps with HUC download, but that is only one piece of the early hydrology workflow

Gap:

- there is no obvious class that starts from a USGS site ID and assembles:
  - site metadata
  - point geometry
  - official basin outline
  - upstream flowlines
  - intersecting HUCs

Requested capability:

- a general gauge-first watershed context helper, likely using USGS NWIS/NLDI/NHDPlus services

Why it belongs there:

- this is hydrology-side watershed preprocessing, not HEC-RAS project editing

### 2. General watershed research workspace organizer

Gap:

- the Spring Creek research workspace was assembled manually

Requested capability:

- a general helper that creates a standard folder structure for hydrology studies, downloads core context datasets, and writes a manifest

Suggested scope:

- gauge
- basin outline
- HUC context
- NHDPlus flowlines
- terrain
- land cover
- soils
- provenance metadata

### 3. TauDEM input-pack preparation helpers

Gap:

- the workflow still needs generalized helpers to prepare TauDEM-ready inputs and document preprocessing assumptions before delineation

Requested capability:

- helpers for:
  - DEM staging and clipping
  - outlet / pour point preparation
  - workspace artifact naming
  - provenance logging
  - boundary comparison against official basin products

### 4. Observed-flow packaging for HMS-side studies

Gap:

- this pass did not find an `hms-commander` USGS data package comparable to `ras_commander.usgs`

Requested capability:

- determine whether `hms-commander` should add a lighter-weight observed-flow package for watershed and hydrology studies, separate from HEC-RAS project concerns

Important decision needed:

- avoid duplicating the full `ras-commander` USGS stack if a shared utility or minimal adapter would be cleaner

## Split By Generalizability

Work that should land in `hms-commander`:

- HUC/NHDPlus/NLDI/TauDEM preprocessing helpers
- basin-first watershed context assembly
- hydro-preprocessing workspace organization
- general provenance and artifact packaging for watershed delineation studies

Work that should land in `ras-commander`:

- gauge data cataloging for model folders
- boundary-condition and initial-condition generation from observed data
- gauge-to-model matching
- model-result validation against observed records
- report-oriented packaging tied to HEC-RAS projects

Work that should stay in `ras-agent`:

- Illinois profile decisions
- Spring Creek-specific orchestration
- integration glue between the two shared libraries
- tracking which upstream issues block the local workflow
