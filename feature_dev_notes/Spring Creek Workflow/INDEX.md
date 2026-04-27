# Spring Creek Workflow

Last updated: 2026-04-15

## Purpose

This folder records early feature-development notes for the Spring Creek at Springfield, Illinois workflow centered on USGS gauge `05577500`.

The immediate goal is to separate:

- what `ras-agent` can already reuse from `ras-commander` and `hms-commander`
- what should become upstream GitHub issues in those repos
- what end-to-end real-world checks still need to be formalized before production workflow development

These notes are local planning material. If a reusable feature gap belongs in a sibling repo, the canonical request should be a GitHub issue in that target repo.

## Current Local Context

- Gauge: `USGS-05577500`
- Station name: `SPRING CREEK AT SPRINGFIELD, IL`
- Current research workspace: `workspace/Spring Creek Springfield IL/`
- Current gauge HUC12: `071300080203` `Archer Creek-Spring Creek`
- Official NLDI upstream basin currently intersects `11` HUC12 polygons in local workspace outputs

## Notes In This Folder

- [01_current-workflow-and-reuse-candidates.md](01_current-workflow-and-reuse-candidates.md)
- [02_feature-gaps-and-issue-candidates.md](02_feature-gaps-and-issue-candidates.md)
- [03_end-to-end-checklist-seed.md](03_end-to-end-checklist-seed.md)

## Working Rule

Land work where it is most reusable:

- general TauDEM, watershed preprocessing, HUC/NHDPlus/NLDI, and hydrology-support tooling belongs in `hms-commander`
- general HEC-RAS project, geometry, boundary condition, compilation, execution, and validation tooling belongs in `ras-commander`
- Illinois-specific orchestration, profile choices, and product integration belong in `ras-agent`
