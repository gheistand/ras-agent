---
description: Accepted engineering bounds for H&H outputs — agents validate against these, never silently accept out-of-range values
globs: pipeline/**
---

# Scientific Validation Bounds

All pipeline outputs must pass these bounds checks before being accepted.
Out-of-range values must be flagged — WARN for edge cases, HITL for critical violations.

Reference: USGS SIR 2008-5176 (Soong et al. 2008), NRCS NEH Part 630, Chow (1959),
USACE HEC-RAS 2D Modeling Guidelines.

---

## Manning's Roughness Coefficient (n)

Used by `model_builder.py` for NLCD-based roughness assignment.

| NLCD Code | Description | Typical n | Safety Bounds | Notes |
|-----------|-------------|-----------|---------------|-------|
| 11 | Open water | 0.035 | 0.020–0.050 | Channel: 0.025–0.035; ponds/lakes: 0.030–0.040 |
| 21 | Dev. low intensity | 0.080 | 0.050–0.120 | Sparse structures, golf courses |
| 22 | Dev. medium intensity | 0.100 | 0.060–0.150 | Mixed structures, streets |
| 23 | Dev. high intensity | 0.120 | 0.070–0.180 | Dense urban, parking lots |
| 24 | Dev. open space | 0.065 | 0.050–0.080 | Parks, utility corridors |
| 31 | Barren land | 0.030 | 0.020–0.050 | Rock, sand, bare soil |
| 41 | Deciduous forest | 0.120 | 0.080–0.160 | Oak, maple, ash — dominant IL type |
| 42 | Evergreen forest | 0.140 | 0.090–0.180 | Pine, fir, spruce |
| 43 | Mixed forest | 0.130 | 0.085–0.170 | Mixed deciduous/evergreen |
| 52 | Shrub/scrub | 0.060 | 0.030–0.100 | Low brush, young forest |
| 71 | Grassland/herbaceous | 0.040 | 0.035–0.050 | Overland floodplain flow (higher than channel) |
| 81 | Pasture/hay | 0.035 | 0.025–0.060 | Mowed/grazed grassland |
| 82 | Cultivated crops | 0.037 | 0.025–0.070 | Row crops; bare soil post-harvest: 0.020–0.030 |
| 90 | Woody wetlands | 0.080 | 0.050–0.150 | Swamp, bottomland forest |
| 95 | Herbaceous wetlands | 0.080 | 0.050–0.150 | Marsh, sedge meadow |

**Validation logic:** Flag if assigned n falls outside Safety Bounds. Trigger HITL if NLCD class
is not in the table (no default n should be silently applied).

---

## Time of Concentration (Tc)

Method: Kirpich formula — standard for IL ungauged agricultural watersheds (10–500 mi²).

```
Tc = 0.0078 * L^0.77 * S^(-0.385)   [result in minutes]
Tc_hr = Tc_min / 60

where:
  L = longest flow path / main channel length (feet)
      NOTE: L is the longest flow path, not watershed perimeter
      or straight-line distance — a common implementation error
  S = main channel slope (ft/ft), computed by 10–85% method
```

| Drainage Area | Typical Tc Range | Notes |
|---------------|-----------------|-------|
| 10 mi² | 0.5–2.0 hr | Small IL ag watershed |
| 50 mi² | 1.5–5.0 hr | |
| 100 mi² | 2.5–7.0 hr | |
| 200 mi² | 4.0–10.0 hr | |
| 500 mi² | 6.0–12.0 hr | |

**Validity bounds:**
- Tc < 0.25 hr → HITL: watershed may be too small, steep, or urban
- Tc > 15 hr → HITL: watershed may be too large or very flat (S < 0.0005 ft/ft)
- S < 0.0001 ft/ft → Kirpich unreliable; consider alternative Tc method or HITL

---

## Peak Flow Estimation

Regulatory standard: FEMA **100-year return period (Q100)** in CFS.

### Unit Peak Flow (csm = CFS per square mile)

| Return Period | Typical csm (IL) | Safety Bounds |
|---------------|-----------------|---------------|
| Q2 | 15–50 csm | 5–200 csm |
| Q10 | 30–80 csm | 10–400 csm |
| Q100 | 50–150 csm | 30–800 csm |
| Q500 | 80–250 csm | 50–1200 csm |

**Validation checks:**
- Q2 < Q10 < Q100 < Q500 (monotonic) — if not, HITL
- Q100/Q2 ratio: typical 2.0–6.0; flag if < 1.5 or > 8.0
- API vs. regression discrepancy > ±20% → HITL (which source is authoritative?)
- StreamStats confidence intervals must be documented, not discarded

### StreamStats vs. IL Regression Fallback

| Source | Validity | Typical uncertainty |
|--------|----------|-------------------|
| USGS StreamStats API | 10–500 mi², rural IL | ±25–40% (95% CI) |
| IL regression (SIR 2008-5176) | 10–500 mi², rural IL | ±30–50% |

Both sources share the same underlying regression equations. Discrepancies > ±20% indicate
watershed characteristics at the edge of equation validity — flag for HITL.

---

## Mesh Cell Size

| Watershed Area | Recommended | Min (LiDAR floor) | Max (stability) |
|----------------|------------|------------------|-----------------|
| < 50 mi² | 15–30 m | 10 m | 50 m |
| 50–150 mi² | 25–60 m | 10 m | 100 m |
| 150–300 mi² | 50–100 m | 15 m | 150 m |
| > 300 mi² | 75–200 m | 20 m | 300 m |

- < 10 m → HITL: below LiDAR resolution, no additional terrain fidelity
- > 300 m → HITL: excessive numerical diffusion for flood routing
- Selection rationale must be logged (see `rules/transparency.md`)

---

## Watershed Characteristics (Illinois, 10–500 mi²)

| Parameter | Typical Range | Flag Condition |
|-----------|---------------|----------------|
| Relief (max – min elev.) | 200–600 ft | < 50 ft or > 1500 ft → HITL |
| Mean slope | 0.5–2.5% | < 0.1% or > 5.0% → WARN |
| Main channel length / area | 0.5–1.2 mi/mi² | > 2.5 → WARN (unusual channel geometry) |
| Drainage density | 1–3 mi/mi² | > 5 → WARN |
| Cultivated crop fraction (NLCD 82) | 40–70% | < 10% or > 90% → note land use context |
| Pour point snap distance | < 300 m | > 500 m → HITL |
| NoData in DEM | < 2% | > 5% → HITL |

---

## Results / Output

| Parameter | Flag Condition | Severity | Action |
|-----------|---------------|----------|--------|
| Max depth anywhere | > 30 ft | CRITICAL | HITL — model instability likely |
| Max depth anywhere | < 0.1 ft | WARN | Check BC and initial conditions |
| Max WSE above terrain | > 30 ft | CRITICAL | HITL — likely error |
| Flood extent | > 40% of watershed area | CRITICAL | HITL — check BCs, mesh |
| Flood extent | < 1% of watershed area | WARN | Verify BC discharge is correct |
| Depth vs. FIRM benchmark | RMSE > 0.5 ft | CRITICAL | HITL — calibration needed (FEMA standard) |
| Depth vs. FIRM benchmark | RMSE 0.25–0.5 ft | WARN | Flag; may be acceptable for planning |
| Wet cells at t=0 | > 1% of domain | WARN | Initial condition issue |

---

## Validation Implementation Pattern

```python
# Example bounds check in pipeline modules
def _check_tc_bounds(tc_hr: float, drainage_area_mi2: float) -> str:
    """Returns 'PASS', 'WARN', or 'HITL' with message."""
    if tc_hr < 0.25 or tc_hr > 15:
        return f"HITL: Tc={tc_hr:.2f} hr outside valid range [0.25, 15] hr"
    # Warn if outside typical range for this drainage area
    typical_min = 0.003 * drainage_area_mi2 + 0.2   # rough heuristic
    typical_max = 0.025 * drainage_area_mi2 + 1.0
    if not (typical_min <= tc_hr <= typical_max):
        return f"WARN: Tc={tc_hr:.2f} hr outside typical range for {drainage_area_mi2:.0f} mi²"
    return "PASS"
```

Agents must call the equivalent check and act on the result:
- `PASS` → proceed, log the value
- `WARN` → log a warning, proceed in `async` mode, pause in `blocking` mode
- `HITL` → always route through `expert_liaison.ask()` regardless of mode
