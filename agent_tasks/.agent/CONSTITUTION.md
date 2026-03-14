# RAS Agent — Project Constitution

## Mission

Automate end-to-end 2D HEC-RAS hydraulic modeling: from terrain ingestion through watershed delineation, peak flow estimation, hydrograph generation, model build, HEC-RAS execution, and GIS results export. Built at CHAMP (Illinois State Water Survey).

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| **EPSG:5070** (NAD83 Albers Equal Area) | Meters-based, suitable for continental US analysis |
| **NRCS DUH** for hydrographs | Industry standard for ungauged watersheds |
| **Template clone strategy** (Path A) | RAS Commander cannot create greenfield 2D projects |
| **Mock mode throughout** | Enables full CI without HEC-RAS installation |
| **Bare imports** in pipeline | Simpler module resolution, avoids package structure overhead |
| **Apache 2.0 license** | Open-source, permissive, compatible with government use |
| **Illinois-first** | StreamStats regression fallback tuned for IL |

## Quality Standards

- **112 test baseline** — never reduce
- **All tests pass without HEC-RAS or network access**
- **Graceful degradation** — optional deps always have fallback paths
- **Mock mode** — every pipeline stage works with `mock=True`
- **CI green** — pytest + npm build must pass on every PR

## Architecture Principles

- **Pipeline stages are independent modules** — each can be tested/replaced individually
- **Orchestrator chains stages** — single entry point, clear data flow
- **Web dashboard is a thin client** — all logic lives in the Python pipeline
- **Docker-first deployment** — but works locally without Docker too
