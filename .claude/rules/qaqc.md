---
description: Reflexive autonomous QAQC — built into every agent's cognitive loop, not post-hoc
globs:
---

# Autonomous QAQC Protocol

QAQC is not optional, not post-hoc, and not a separate phase. It is built into every agent's
cognitive loop. Agents validate their own work and surface findings before proceeding.

## The QAQC Loop

For every substantive change or pipeline stage completion:

```
Make the change / run the stage
        ↓
Validate outputs against scientific-validation.md bounds
        ↓
Log all results with [CALC] prefix (see transparency.md)
        ↓
PASS → proceed      WARN → log + proceed (async) or pause (blocking)
        ↓                   ↓
                     HITL → route via expert_liaison.ask()
```

---

## Per-Agent QAQC Responsibilities

### pipeline-dev
After every pipeline module change:
1. Run `python -m pytest tests/test_{module}.py -v`
2. Verify mock mode still works end-to-end
3. Verify any scientific calculation changes are within bounds (scientific-validation.md)
4. If scientific logic changed → hydro-reviewer should review before commit

### test-engineer
After writing or modifying tests:
1. Run full suite: `python -m pytest tests/ -v`
2. Verify count ≥ 117 (current baseline — never reduce)
3. Verify no test requires real network access
4. Verify no test requires HEC-RAS installation

### devops
After infrastructure changes:
1. Verify `docker-compose build api` succeeds
2. Verify CI workflow parses without errors
3. Check that new hook scripts are registered in `settings.json`

### hydro-reviewer
Proactively triggered by changes to: `hydrograph.py`, `streamstats.py`,
`model_builder.py` (Manning's n), `watershed.py` (Tc), `scientific-validation.md`.
1. Check all equations against references
2. Verify parameter ranges against scientific-validation.md
3. If out-of-range → write CRITICAL finding AND trigger HITL

### qaqc-validator
Post-run validation:
1. Check all stage outputs against scientific-validation.md
2. Produce pass/warn/fail summary per stage
3. Block run from being marked "complete" if CRITICAL failures exist
4. Queue HITL questions for all anomalies

---

## Pipeline-Level QAQC Checkpoints

### After Stage 1 (Terrain)
- ✓ CRS is EPSG:5070 after reprojection?
- ✓ DEM NoData < 5% of watershed area?
- ✓ Relief in [50, 1500] ft?
- ✓ GeoTIFF is Cloud-Optimized?

### After Stage 2 (Watershed)
- ✓ Drainage area in [10, 500] mi²?
- ✓ Pour point snap distance < 500 m?
- ✓ Watershed polygon closed and valid?
- ✓ Main channel length > 2 km?

### After Stage 3 (Peak Flows)
- ✓ Q2 < Q10 < Q100 < Q500 (monotonic)?
- ✓ Q100 unit peak flow in [30, 800] csm?
- ✓ If both API and regression: discrepancy < ±20%?
- ✓ Confidence intervals documented?

### After Stage 4 (Hydrograph)
- ✓ Tc in [0.25, 15] hr?
- ✓ Hydrograph peak ≈ input Q within ±5%?
- ✓ No negative flow values?
- ✓ Duration ≥ 1.5 × Tp?

### After Stage 5 (Model Build)
- ✓ Template area within ±25% of target area?
- ✓ Manning's n assigned to > 95% of cells?
- ✓ Downstream BC slope in [0.0005, 0.005] ft/ft?
- ✓ Simulation window > 2 × hydrograph duration?

### After Stage 6 (Runner)
- ✓ HEC-RAS completed without errors?
- ✓ HDF5 output contains expected groups/datasets?
- ✓ No stability warnings in RAS log?

### After Stage 7 (Results)
- ✓ COG GeoTIFFs valid with overviews?
- ✓ Max depth in [0.1, 30] ft?
- ✓ Flood extent < 40% of watershed area?
- ✓ All return periods exported?

---

## QAQC Report Format

Every run produces a QAQC report at `.claude/outputs/qaqc-validator/{date}-{run_id}-qaqc.md`:

```markdown
# QAQC Report — {run_id}
Date: {date} | Watershed: {name} | Area: {X} mi²
Overall Status: PASS | PASS with WARNINGS | FAIL

## Stage Results
| Stage | Status | Key Value | Notes |
|-------|--------|-----------|-------|
| Terrain | ✅ | relief=420m, nodata=0.8% | |
| Watershed | ✅ | area=95.3 mi² | |
| Peak flows | ⚠️ | Q100=9384 CFS, 18% vs regression | HITL queued |
| Hydrograph | ✅ | Tc=4.2 hr, Qp=9384 CFS | |
| Model build | ✅ | n coverage=100% | |
| Runner | ✅ | 2400 sec, clean | |
| Results | ✅ | max depth=2.1 ft | |

## Findings
[WARN] Stage 3: API/regression peak flow discrepancy 18% — logged, expert notified
```
