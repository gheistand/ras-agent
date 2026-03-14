# Claude Code Infrastructure Recommendations for RAS Agent
## Comprehensive Agent Architecture Review & Build Plan

**Reviewer:** Senior AI Systems Architect  
**Date:** 2026-03-14  
**Status:** Complete — Ready for Implementation  
**Output Path:** `/Users/glennheistand/ras-agent/agent_tasks/.agent/CLAUDE_INFRA_RECOMMENDATIONS.md`

---

## Executive Summary

Glenn's four non-negotiable requirements — **HITL as a first-class rule, domain expert access, reflexive autonomous QAQC, and transparent calculation logging** — are currently **missing from the agent architecture**. The hydro-reviewer exists but is passive (must be explicitly invoked). There are no rules codifying when to pause vs. proceed, no automated QAQC enforcement, and no formal mechanism to surface uncertainty.

This document provides:

1. **Four new first-class rules** establishing HITL decision logic, scientific validation bounds, calculation transparency, and reflexive QAQC
2. **Three new agents** (qaqc-validator, expert-liaison, updated hydro-reviewer) to implement these principles
3. **Three new skills** (/ask-expert, /validate-run, /calibrate) for user/agent interaction
4. **Three new hooks** for calculation transparency, range guarding, and proactive hydro-reviewer triggering
5. **Root CLAUDE.md additions** documenting HITL section and validation bounds
6. **Domain knowledge codification** for pipeline/CLAUDE.md with Manning's n tables, Tc bounds, StreamStats uncertainty, cell size guidance, and IL-specific characteristics

All code ready to write to disk. **No implementations are made** — this is a specification document.

---

## Part 1: New Rules (4 files)

### Rule 1.1: human-in-the-loop.md

**File:** `.claude/rules/human-in-the-loop.md`

```markdown
---
description: First-class rule — when agents pause to ask domain expert (Glenn) vs. proceed autonomously
globs:
---

# Human in the Loop (HITL) — First-Class Rule

HITL is a core architectural principle. Agents must know when to pause and ask Glenn rather than proceeding autonomously on scientific/methodological decisions.

## Glenn's Domain Expertise

Glenn Heistand is:
- PE (Professional Engineer) + CFM (Certified Floodplain Manager)
- Deep expertise in hydrology, hydraulic modeling (H&H), FEMA floodplain management
- HEC-RAS 2D modeling specialist
- NRCS methods expert
- USGS StreamStats user
- Illinois hydrology specialist (CHAMP section lead, ISWS)

**He is the domain expert. Always defer to his judgment on H&H methodology.**

## HITL Decision Tree

### PAUSE (Always Ask Glenn) When:

1. **Choosing scientific methods** — Tc formula, peak flow estimation method, synthetic hydrograph model, Manning's n assignment strategy
   - Example: "Should we use Kirpich vs. SCS CN for this 95 mi² IL agricultural watershed?"
   - Example: "Regression equations are showing Q100=12,000 CFS but StreamStats gives 8,500 CFS — which do we trust?"

2. **Interpreting uncertainty or anomalies** — Results that fall outside expected bounds, conflicting data sources, missing parameters
   - Example: "Manning's n from NLCD is 0.12 for this forest, but typical floodplain values are 0.10. Should we adjust?"
   - Example: "Watershed delineation shows two possible pour points. Which represents the true outlet?"

3. **Setting parameters with high domain sensitivity** — Cell size selection, hydrograph duration, boundary condition type, warm-up time, simulation time step
   - Example: "Mesh cell size 150m vs. 75m for a 250 mi² watershed — trade-offs?"
   - Example: "Should warm-up time be 12h or 24h? Is baseflow needed?"

4. **FEMA compliance questions** — Any choice that affects regulatory acceptability, depth accuracy, floodplain mapping
   - Example: "Is <0.5 ft depth error vs. benchmarks achievable with our template mesh?"
   - Example: "Do we document StreamStats regression uncertainties in the project report?"

5. **Deviating from standard IL practice** — When Glenn's established methodology should apply
   - Example: "First time modeling in a new IL region. Should we test regression equations against local gages?"

### PROCEED (Autonomous) When:

1. **Implementing Glenn's established methodology** — Once he's decided on Tc method, Manning's n lookup table, hydrograph type, etc., implementation is autonomous
   - Example: Agent implements Kirpich Tc formula once Glenn confirms it's appropriate for this watershed
   - Example: Agent applies NLCD-based Manning's n table once Glenn approves the lookup logic

2. **Data ingestion and format conversion** — Downloading DEM, clipping to watershed, reprojecting to EPSG:5070, converting GeoTIFF to COG
   - These are technical, not scientific — proceed autonomously

3. **Mesh/model generation following a template** — Cloning template projects, updating Manning's n, writing boundary conditions
   - Once template approach is validated, this is mechanical — proceed autonomously

4. **QAQC and validation checks** — Running tests, comparing outputs to bounds, flagging anomalies
   - Agents should run these automatically and report findings to Glenn via expert-liaison

5. **Code implementation and testing** — Writing Python, fixing bugs, refactoring
   - Proceed autonomously; flag scientific implications for Glenn via expert-liaison

## Implementation Pattern

### In Agent Code/Prompts:

When uncertain, use this pattern:

```python
# Example: Manning's n assignment decision
if value_is_outside_established_bounds(mannings_n, cell_type):
    logger.warning(f"Manning's n={mannings_n} for {cell_type} — outside typical range 0.02–0.15")
    expert_liaison.ask_expert(
        question="Manning's n assignment",
        context={
            "cell_type": cell_type,
            "assigned_value": mannings_n,
            "typical_range": (0.02, 0.15),
            "nlcd_source": nlcd_data,
        },
        urgency="before_proceeding"  # or "log_for_review"
    )
    # Pause for response
```

### In Orchestrator/Main Agent:

When a subagent flags a HITL question via expert-liaison, the orchestrator:
1. Pauses the pipeline
2. Routes the question to Glenn via Telegram (or preferred channel)
3. Waits for response
4. Resumes with Glenn's guidance or aborts gracefully

## Examples by Module

### terrain.py
- **PAUSE:** "Which DEM source is more suitable — ILHMP LiDAR (3m, 1 mi² data gaps) or USGS 3DEP (10m, complete)?"
- **PROCEED:** Downloading, mosaicing, reprojecting DEMs

### watershed.py
- **PAUSE:** "Pour point 1 (user-supplied) vs. Pour point 2 (D8 delineation) — which outlet should we use?"
- **PAUSE:** "Minimum stream area threshold: should we use 2.0 km² or 5.0 km²?"
- **PROCEED:** Filling pits, computing flow direction, extracting watershed polygon

### streamstats.py
- **PAUSE:** "StreamStats API returned Q100=8,500 CFS. IL regression equations gave 12,000 CFS. How should we reconcile?"
- **PAUSE:** "Should we weight StreamStats uncertainty bands in the output documentation?"
- **PROCEED:** Querying API, parsing responses, implementing fallback equations

### hydrograph.py
- **PAUSE:** "Kirpich Tc is 6.2 hours for this watershed. Does that seem reasonable for 95 mi² central IL ag land?"
- **PAUSE:** "Should we apply a peak rate factor adjustment (484 vs. 300)?"
- **PROCEED:** Computing ordinates from NRCS DUH table, scaling to peak flow

### model_builder.py
- **PAUSE:** "Selected medium template (200 mi²) for 185 mi² watershed. Will mesh perimeter fit?"
- **PAUSE:** "Manning's n for developed areas: NLCD gives 0.10, but should we use 0.08 for newer development?"
- **PROCEED:** Cloning template, updating HDF5 files, writing perimeter to .g## file

### results.py
- **PAUSE:** "Max flood depth is 0.3 ft — is that physically plausible for a 2D model?"
- **PAUSE:** "Flood extent area is 50 mi² for Q100. Compare against published FIRM — is that reasonable?"
- **PROCEED:** Extracting HDF5 data, interpolating to raster, exporting COG GeoTIFF

## When in Doubt

If an agent is uncertain whether a decision is HITL, **err on the side of asking Glenn**. Surfacing uncertainty is the goal. Silent autonomy on a scientifically sensitive choice is the failure mode.

---
```

### Rule 1.2: scientific-validation.md

**File:** `.claude/rules/scientific-validation.md`

```markdown
---
description: Validation bounds for scientific quantities — Manning's n, Tc, peak flows, cell size, depth error
globs: pipeline/**
---

# Scientific Validation & Bounds

Agents must cross-check outputs against physical reasonableness bounds. Flag anomalies. Never silently accept results outside expected engineering ranges.

## Key Validation Bounds

### Manning's Roughness Coefficient (n)

| Land Cover Type | NLCD Class | Typical Range | Safety Bounds* | Source |
|-----------------|-----------|---------------|----------------|--------|
| Open water | 11 | 0.030–0.040 | 0.020–0.050 | HEC-RAS manual |
| Developed, low intensity | 21 | 0.070–0.090 | 0.050–0.120 | USACE practice |
| Developed, medium intensity | 22 | 0.090–0.110 | 0.060–0.150 | USACE practice |
| Developed, high intensity | 23 | 0.100–0.130 | 0.070–0.180 | USACE practice |
| Barren land | 31 | 0.025–0.035 | 0.020–0.050 | Chow (1959) |
| Deciduous forest | 41 | 0.100–0.130 | 0.080–0.160 | USACE practice |
| Evergreen forest | 42 | 0.120–0.150 | 0.090–0.180 | USACE practice |
| Mixed forest | 43 | 0.110–0.140 | 0.085–0.170 | USACE practice |
| Shrub/scrub | 52 | 0.050–0.070 | 0.030–0.100 | USACE practice |
| Grassland/herbaceous | 71 | 0.030–0.040 | 0.025–0.060 | USACE practice |
| Pasture/hay | 81 | 0.030–0.040 | 0.025–0.060 | USACE practice |
| Cultivated crops | 82 | 0.035–0.045 | 0.025–0.070 | USACE practice |
| Woody wetlands | 90 | 0.060–0.100 | 0.050–0.150 | USACE practice |
| Herbaceous wetlands | 95 | 0.060–0.100 | 0.050–0.150 | USACE practice |

*Safety Bounds allow for uncertainty in land cover classification and local conditions.*

**Validation logic:**
```python
if not (lower_bound <= assigned_n <= upper_bound):
    logger.warning(f"Manning's n={assigned_n} outside safety bounds [{lower_bound}, {upper_bound}]")
    # Trigger expert-liaison HITL question
```

### Time of Concentration (Tc)

**Method:** Kirpich formula (for IL agricultural watersheds, 10–500 mi²)
- Formula: `Tc = 0.0078 * L^0.77 * S^(-0.385)` where L=feet, S=ft/ft (slope)
- Returns: Tc in minutes; convert to hours: Tc_hr = Tc_min / 60
- **Valid range for IL:** 0.25–12 hours (typical: 1–8 hours for 10–500 mi² ag watersheds)
- **Very small watersheds (<5 mi²):** Tc may fall below 0.5 hr — physically possible but rare for ungauged streams
- **Very flat terrain (S < 0.001 ft/ft):** Tc may exceed 12 hours — inspect watershed; consider alternative methods if S < 0.0005
- **HITL trigger:** If Tc < 0.25 hr or Tc > 15 hr, ask Glenn for validation

**Typical IL ranges by area:**
| Drainage Area | Typical Tc Range |
|---------------|-----------------|
| 10 mi² | 0.5–2.0 hr |
| 50 mi² | 1.5–5.0 hr |
| 100 mi² | 2.5–7.0 hr |
| 200 mi² | 4.0–10.0 hr |
| 500 mi² | 6.0–12.0 hr |

### Peak Flow Estimation

**Regulatory standard:** FEMA 100-year return period (Q100)

| Metric | Bound | Source | HITL Trigger |
|--------|-------|--------|--------------|
| **StreamStats vs. Regression** | Reconcile if >±20% difference | USGS SIR 2008-5176 | Yes, ask Glenn |
| **Unit peak flow (q_p)** | 300–600 csm* typical | NRCS NEH 630 | Flag if q_p < 200 or > 800 csm |
| **Peak rate factor (Fp)** | 300 (flat) – 484 (standard) | NRCS | Confirm with Glenn for each watershed |
| **Baseflow** | 5–15% of peak** | USGS practice | Flag if > 20% or missing justification |
| **Hydrograph duration** | 1.5–3.0 × Tp*** | NRCS | Flag if < Tp or > 4 × Tp |

*csm = cubic feet per second per square mile  
**Baseflow as % of Q100  
***Tp = time to peak

**Validation logic:**
```python
q_peak_csm = peak_flow_cfs / drainage_area_mi2
if not (300 <= q_peak_csm <= 600):
    logger.warning(f"Peak flow {q_peak_csm} csm outside typical 300–600 range")
    # Consider HITL question
```

### Cell Size (2D Mesh Resolution)

| Watershed Area | Recommended Range | Minimum (LiDAR limit) | Maximum (stability) |
|----------------|-------------------|----------------------|---------------------|
| <50 mi² | 15–30 m | 10 m | 50 m |
| 50–150 mi² | 25–60 m | 10 m | 100 m |
| 150–300 mi² | 50–100 m | 15 m | 150 m |
| >300 mi² | 75–200 m | 20 m | 300 m |

**Justification:**
- **Minimum:** LiDAR resolution (typically 3–10 m); finer meshes capture terrain detail but require more computation
- **Maximum:** Stability + compute time; larger cells miss localized flow features but reduce runtime
- **Trade-off:** Choose cell size based on (1) terrain resolution, (2) target flood feature scale (levees, bridges), (3) available compute time

**HITL trigger:** If cell size < 10 m or > 300 m, or if ratio of cell size to watershed relief is unusual, ask Glenn

### Flood Depth Accuracy

**FEMA regulatory standard:** <0.5 ft absolute error vs. known benchmarks (gages, post-event surveys)

| Scenario | Target Accuracy | Margin |
|----------|-----------------|--------|
| Regulatory (FEMA FIRM) | <0.5 ft | ±0.25 ft |
| Engineering study | <1.0 ft | ±0.5 ft |
| Planning-level | <2.0 ft | ±1.0 ft |

**Validation logic:**
```python
if max_model_depth_error_ft > 0.5:
    logger.warning(f"Max depth error {max_model_depth_error_ft} ft exceeds FEMA tolerance")
    # Flag for post-run review by expert-liaison
```

### Watershed Characteristics

**For Illinois, 10–500 mi² range:**

| Parameter | Typical Range | Bounds |
|-----------|---------------|--------|
| **Relief (elevation range)** | 200–800 ft | 50–1500 ft |
| **Mean slope** | 0.5–2.5% | 0.1–5.0% |
| **Channel length/area ratio** | 0.5–1.5 mi/mi² | 0.3–2.5 mi/mi² |
| **Drainage density** | 1–3 mi/mi² | 0.5–5 mi/mi² |
| **Forest fraction (NLCD)** | 10–40% | 0–100% |

**HITL trigger:** If watershed characteristics fall outside typical range, ask Glenn if special methodology applies

## Implementation Pattern

### In pipeline modules:

1. **Log all validation decisions:**
   ```python
   logger.info(f"Manning's n={assigned_n} (NLCD class {nlcd_code}, typical range {typical_min}–{typical_max})")
   ```

2. **Flag out-of-bounds:**
   ```python
   if assigned_n < safety_bounds[0] or assigned_n > safety_bounds[1]:
       logger.warning(f"Out-of-bounds Manning's n: {assigned_n}")
       expert_liaison.flag_for_review(...)
   ```

3. **Document assumptions:**
   ```python
   logger.info(f"Kirpich Tc method chosen (Glenn approved {watershed_id}); formula=0.0078*L^0.77*S^-0.385")
   ```

4. **Preserve uncertainty:**
   ```python
   logger.info(f"StreamStats API: Q100={streamstats_q100} CFS (lower={lower_ci}, upper={upper_ci})")
   logger.info(f"IL regression fallback: Q100={regression_q100} CFS")
   logger.info(f"Difference: {abs(streamstats_q100 - regression_q100) / streamstats_q100 * 100:.1f}%")
   ```

---
```

### Rule 1.3: transparency.md

**File:** `.claude/rules/transparency.md`

```markdown
---
description: Calculation transparency — show the math, inputs, method, and outputs
globs: pipeline/**
---

# Transparency in Calculations

When an agent makes a scientific calculation (Tc, peak flow, Manning's n assignment, cell size selection), it must log:
- **Inputs:** What values went into the calculation?
- **Method:** Which formula, table, or algorithm was used?
- **Intermediate steps:** For complex formulas, show the arithmetic
- **Output:** What is the final result?
- **Uncertainty/confidence:** Confidence intervals, assumptions, data quality flags

Never log just the result. Show the reasoning.

## Logging Standards

### Tc (Time of Concentration)

**Log format:**
```
[INFO] Tc calculation (Kirpich formula)
  Input: L={length_feet} ft, S={slope_ft_per_ft} ft/ft, drainage_area={area_mi2} mi²
  Method: Tc = 0.0078 * L^0.77 * S^(-0.385)
  Intermediate: L^0.77 = {exponent_result}, S^(-0.385) = {slope_result}
  Result: Tc = {tc_minutes} min = {tc_hours} hr
  Confidence: ✓ (typical range for {area_mi2} mi² IL watershed is {typical_range_hr} hr)
  Reference: NRCS NEH Part 630, Section 16.1
```

**Example:**
```
[INFO] Tc calculation (Kirpich formula)
  Input: L=125,400 ft, S=0.0048 ft/ft, drainage_area=95 mi²
  Method: Tc = 0.0078 * L^0.77 * S^(-0.385)
  Intermediate: 125400^0.77 ≈ 15,231; 0.0048^(-0.385) ≈ 8.94
  Calculation: Tc = 0.0078 × 15,231 × 8.94 ≈ 1,061 min
  Result: Tc = 1,061 min ÷ 60 = 17.7 hr
  Confidence: ⚠️ (typical for 95 mi² is 2.5–7.0 hr; 17.7 hr is HIGH — flagged for expert review)
  Reference: NRCS NEH Part 630, Section 16.1
  Action: Expert-liaison question queued
```

### Peak Flow Estimation

**Log format:**
```
[INFO] Peak flow estimation: Q100 (100-year return period)
  Source: StreamStats API (USGS)
  Delineation: {pour_point_lat}, {pour_point_lon} (snapped to {snap_type})
  Drainage area: {area_mi2} mi²
  Region: Illinois (IL)
  Query result:
    - Q100 from regression: {q100_regression} CFS
    - Confidence interval: [{ci_lower}, {ci_upper}] (95%)
  Method: USGS Regional Regression Equations (SIR 2008-5176, Soong et al. 2008)
  Fallback used: {yes_no}
  Unit peak flow: {q100_cfs} / {area_mi2} mi² = {q_csm} csm
  Reasonableness: {status} ({typical_range_csm} csm typical)
  Output: {q100_cfs} CFS
```

**Example:**
```
[INFO] Peak flow estimation: Q100
  Source: StreamStats API (USGS)
  Pour point: 40.1165°N, 88.2431°W (snapped: 300 m to D8 outlet)
  Drainage area: 95.3 mi²
  Region: Illinois
  Query result:
    - Q100: 9,384 CFS
    - Confidence interval: [7,200, 12,100] CFS (95%)
  Method: USGS Regional Regression Equations (SIR 2008-5176)
  Fallback: No (API succeeded)
  Unit peak flow: 9,384 / 95.3 = 98.4 csm
  Reasonableness: ✓ (typical 50–150 csm; 98.4 csm is within expected range)
  Output: 9,384 CFS
  Uncertainty: ±2,814 CFS (±30% range)
```

### Manning's Roughness Assignment

**Log format:**
```
[INFO] Manning's n assignment: 2D flow area roughness
  Approach: NLCD-based lookup table
  2D area extent: {area_km2} km²
  Land cover composition:
    - {nlcd_class_name} {percent}% → n={n_value}
    - {nlcd_class_name} {percent}% → n={n_value}
    ...
  Method: Area-weighted average of NLCD classes
  Calculation: n = Σ(n_i × fraction_i) = {weighted_calc}
  Result: n = {final_n}
  Reasonableness: {status}
    - Range [0.02, 0.15] (hydraulic: expected bounds for floodplain)
    - Typical for {primary_land_cover}: [{typical_min}, {typical_max}]
  Output: n = {final_n}
  Reference: Manning's Table, Chow (1959); NLCD 2021
```

**Example:**
```
[INFO] Manning's n assignment
  Approach: NLCD-based lookup
  2D area: 18.5 km²
  Composition:
    - Cultivated crops (82): 55% → n=0.037
    - Grassland (71): 30% → n=0.035
    - Deciduous forest (41): 12% → n=0.110
    - Developed low intensity (21): 3% → n=0.080
  Calculation: n = (0.037×0.55) + (0.035×0.30) + (0.110×0.12) + (0.080×0.03)
           = 0.020 + 0.011 + 0.013 + 0.002 = 0.046
  Result: n = 0.046
  Reasonableness: ✓ (range [0.02, 0.15]; typical for ag 0.035–0.040; 0.046 is reasonable)
  Output: n = 0.046
```

### Cell Size Selection

**Log format:**
```
[INFO] Cell size (mesh resolution) selection
  Watershed: {name}, area={area_mi2} mi²
  Terrain data: {source} resolution {dem_res_m} m
  Criteria:
    - Watershed area: {area_mi2} mi² → recommended {rec_min}–{rec_max} m
    - DEM resolution: {dem_res_m} m → minimum ~{dem_res_m} m
    - Compute budget: {time_min} min available → max {max_cell_m} m
    - Feature scale: {feature_scale} m (levees, bridges) → minimum {feature_cell_m} m
  Trade-off analysis:
    - {cell1} m: {trade1_pro} | {trade1_con}
    - {cell2} m: {trade2_pro} | {trade2_con}
  Selected: {chosen_cell_m} m
  Justification: {reason}
  Output: cell_size = {chosen_cell_m} m
```

## Agents Must Include Transparency

- **hydro-reviewer:** When reviewing code, flag missing transparency logs. Missing methodology documentation is a CRITICAL finding.
- **pipeline-dev:** Must add transparency logging when implementing calculations
- **qaqc-validator:** Must verify transparency logs exist and are complete

## Preserve Uncertainties

Always include:
- Confidence intervals (e.g., StreamStats ±30%)
- Alternative sources compared (e.g., API vs. regression)
- Assumptions documented (e.g., "Kirpich chosen by Glenn, 2026-03-13")
- Data quality flags (e.g., "DEM has 1 mi² gap in northwest corner")

---
```

### Rule 1.4: qaqc.md

**File:** `.claude/rules/qaqc.md`

```markdown
---
description: Reflexive, autonomous QAQC — built into cognitive loop, not post-hoc
globs:
---

# Reflexive QAQC (Quality Assurance/Quality Control)

QAQC is not a phase at the end. It is built into every stage of the pipeline. Agents run validation checks automatically and surface findings.

## QAQC Architecture

```
Input → Process → Validate → Output
                     ↓
            Flag anomalies
            Compare to bounds
            Ask expert if needed
```

## Stages and Checkpoints

### Stage 1: Terrain (DEM download/mosaic)

**Automated checks:**
- Verify CRS is EPSG:5070 after reprojection
- Check for data gaps or NoData values > X% of area
- Compare DEM stats (min/max elevation, mean) to expected range for region
- Verify geotiff is Cloud-Optimized (tiled, overviews, LZW compression)

**Logging:**
```python
logger.info(f"DEM statistics: min_elev={dem.min()} m, max_elev={dem.max()} m, relief={dem.max()-dem.min()} m")
logger.info(f"DEM quality: {valid_pixels}% valid, {nodata_pixels}% NoData, CRS={crs}")
if nodata_pixels > 2:
    logger.warning(f"DEM has {nodata_pixels}% NoData — may affect delineation")
```

**HITL trigger:** If relief < 50 ft or NoData > 5%, ask Glenn

### Stage 2: Watershed Delineation

**Automated checks:**
- Verify pour point snapping distance < threshold (e.g., 500m)
- Check watershed polygon closure (first point == last point, no self-intersections)
- Compare computed drainage area to expected range (±10% of literature values if available)
- Flag if minimum stream area threshold seems too large or too small for region

**Logging:**
```python
logger.info(f"Watershed delineation: drainage_area={result.drainage_area_mi2} mi², "
            f"relief={result.relief_m} m, main_channel_length={result.main_channel_length_km} km")
logger.info(f"Pour point snapping: original {original_lat},{original_lon} → snapped {snapped_lat},{snapped_lon} "
            f"(distance={snap_distance_m} m)")
if snap_distance_m > 300:
    logger.warning(f"Pour point snapped {snap_distance_m} m — verify outlet is correct")
```

**HITL trigger:** If drainage area deviates >20% from expected, ask Glenn

### Stage 3: Peak Flow Estimation

**Automated checks:**
- Verify StreamStats API returned valid Q2–Q500 (not null/errors)
- If API failed, verify IL regression fallback was applied
- Compare API result to regression result — flag if >±20% difference
- Check that Q2 < Q10 < Q100 < Q500 (monotonic)
- Verify unit peak flow (csm) is within 200–800 csm range

**Logging:**
```python
logger.info(f"Peak flow estimation: Q100={peak_flows.Q100} CFS (source={peak_flows.source})")
if peak_flows.source == "streamstats":
    logger.info(f"  Confidence interval: [{peak_flows.Q100_lower_ci}, {peak_flows.Q100_upper_ci}] (95%)")
logger.info(f"  Unit peak flow: {peak_flows.Q100 / watershed.drainage_area_mi2:.1f} csm")
if regression_available:
    logger.info(f"  Regression fallback: {regression_q100} CFS (diff: {abs(peak_flows.Q100 - regression_q100)/peak_flows.Q100*100:.1f}%)")
    if abs(peak_flows.Q100 - regression_q100) / peak_flows.Q100 > 0.20:
        logger.warning(f"API vs. regression differ by >20% — flagged for expert review")
```

**HITL trigger:** If API ≠ regression by >±20%, ask Glenn which to use

### Stage 4: Hydrograph Generation

**Automated checks:**
- Verify Tc is in typical range for watershed size/region
- Check that peak discharge from hydrograph ≈ input peak flow (±5%)
- Verify hydrograph is smooth (no negative flows, no discontinuities)
- Compare hydrograph volume (area under curve) to expected rainfall excess

**Logging:**
```python
logger.info(f"Hydrograph generation: Tc={tc_hr} hr, Tp={tp_hr} hr, peak_q={peak_q_cfs} CFS")
logger.info(f"  Hydrograph duration: {start_time} to {end_time} ({duration_hr} hr)")
logger.info(f"  Volume check: input_q100={input_q100_cfs} → hydrograph_peak={hyd_peak_cfs} (diff: {diff_pct:.1f}%)")
if diff_pct > 5:
    logger.warning(f"Hydrograph peak differs from input by {diff_pct:.1f}% — verify DUH implementation")
if tc_hr < 0.25 or tc_hr > 15:
    logger.warning(f"Tc={tc_hr} hr is outside typical range 0.25–15 hr — flagged for expert review")
```

**HITL trigger:** If Tc < 0.25 or > 15 hr, ask Glenn

### Stage 5: Model Builder

**Automated checks:**
- Verify template was selected correctly (area match)
- Check that Manning's n values are assigned to all 2D flow area cells
- Verify boundary conditions are valid (slope, discharge, gate operations)
- Spot-check perimeter against watershed boundary (visual or area comparison)
- Verify simulation time window is reasonable (warm-up + peak + recession)

**Logging:**
```python
logger.info(f"Model build: template={selected_template} (area={template_area_mi2} mi²), target_area={target_area_mi2} mi²")
logger.info(f"  Manning's n: assigned to {cells_with_n}/{total_cells} cells (coverage={pct_coverage:.1f}%)")
if pct_coverage < 95:
    logger.warning(f"Manning's n coverage {pct_coverage:.1f}% — {total_cells - cells_with_n} cells without value")
logger.info(f"  Simulation: warm_up={warmup_hr} hr + hydrograph={hydro_dur_hr} hr + recession={recess_hr} hr = {total_dur_hr} hr")
logger.info(f"  Boundary condition: downstream slope={bc_slope} ft/ft, Q_bc={bc_q_cfs} CFS")
```

**HITL trigger:** If Manning's n coverage < 90% or BC looks unrealistic, ask Glenn

### Stage 6: Runner (HEC-RAS Execution)

**Automated checks:**
- Verify HEC-RAS run completed without errors
- Check that HDF5 output contains expected data (2D depths, WSE, velocities)
- Verify simulation time stepping was stable (no warnings in RAS output)
- Check that max water surface elevation is < digital elevation model max + buffer

**Logging:**
```python
logger.info(f"HEC-RAS execution: {project_path}")
logger.info(f"  Status: {'SUCCESS' if success else 'FAILED'}")
logger.info(f"  Runtime: {runtime_sec:.1f} sec")
if success:
    logger.info(f"  Output HDF5: {num_time_steps} time steps, 2D area '{area_name}' with {num_cells} cells")
```

**HITL trigger:** If run failed, ask Glenn (operator or model issue?)

### Stage 7: Results Export

**Automated checks:**
- Verify raster outputs (COG GeoTIFF) are valid, cloud-optimized, have overviews
- Check flood extent polygon is valid (closed, no self-intersections)
- Compare max flood depth to input peak flow and watershed area (rough reasonableness)
- Verify all return periods (10/50/100yr, etc.) were exported
- Check that GeoPackage metadata is complete

**Logging:**
```python
logger.info(f"Results export: {output_dir}")
logger.info(f"  Depth raster: {depth_tif} ({width}×{height} pixels, {cell_size_m}m)")
logger.info(f"  Flood extent: {extent_area_mi2:.1f} mi², max_depth={max_depth_ft:.2f} ft")
logger.info(f"  Return periods: {', '.join([f'Q{rp}' for rp in return_periods])}")
logger.info(f"  Reasonableness check: max_depth={max_depth_ft} ft (expected 0.5–20 ft for this area)")
if max_depth_ft < 0.1 or max_depth_ft > 30:
    logger.warning(f"Max depth {max_depth_ft} ft is unusual — flagged for review")
```

**HITL trigger:** If depth < 0.1 ft or > 30 ft, ask Glenn

## QAQC Agent Responsibilities

The `qaqc-validator` agent is responsible for:

1. **Running automated checks** after each stage completes
2. **Logging all validation results** with PASS/WARN/FAIL status
3. **Aggregating findings** into a QAQC report
4. **Queuing HITL questions** via expert-liaison when anomalies are detected
5. **Preventing propagation** of failed outputs to next stage

## QAQC Report (Post-Run)

Every run generates a `.claude/outputs/qaqc-validator/{date}-{run_id}-qaqc.md` report:

```markdown
# QAQC Report: {Watershed} Q100 Modeling

**Date:** {date}  
**Run ID:** {run_id}  
**Watershed:** {name}, {area_mi2} mi²  

## Summary
- **Overall Status:** PASS | PASS with WARNINGS | FAIL
- **Stages passed:** {n}/{7}
- **Anomalies flagged:** {count}
- **HITL questions queued:** {count}

## Stage Checklist

| Stage | Status | Notes |
|-------|--------|-------|
| Terrain | PASS | DEM 95% valid, relief 420 m ✓ |
| Watershed | PASS | Drainage area 95.3 mi² (expect ~90–100 mi²) ✓ |
| Peak flow | WARN | API/regression differ 18% — logged |
| Hydrograph | PASS | Tc=4.2 hr, peak=9,384 CFS ✓ |
| Model build | PASS | Manning's n 100% coverage ✓ |
| Runner | PASS | HEC-RAS 2400 sec, no warnings ✓ |
| Results | WARN | Max depth 2.1 ft (typical 0.5–20 ft) — within bounds |

## Anomalies & Actions

1. **API/Regression peak flow discrepancy** [WARN]
   - StreamStats Q100: 9,384 CFS
   - IL regression: 11,200 CFS
   - Difference: +19.4%
   - Action: Logged. Expert-liaison flagged for Glenn review before use.

...
```

---
```

---

## Part 2: New & Updated Agents (3 files)

### Agent 2.1: Updated hydro-reviewer/SUBAGENT.md

**File:** `.claude/agents/hydro-reviewer/SUBAGENT.md`

```markdown
---
model: sonnet
tools: Read, Write, Grep, Glob, Bash
description: Proactive domain expert for scientific review — runs automatically on hydro-sensitive code changes
---

# Hydrology Reviewer (Updated)

You are a senior domain expert in hydrology and hydraulic modeling. You review code for scientific correctness **proactively** — not just when asked. You flag methodology decisions, parameter choices, and anomalies.

## Core Responsibilities

1. **Proactive review:** Automatically triggered when hydro-sensitive code changes (see below)
2. **Scientific correctness:** Examine equations, units, parameter ranges, boundary conditions
3. **Domain expertise:** Evaluate assumptions against IL hydrology, FEMA standards, best practices
4. **HITL awareness:** Identify decisions that require Glenn's input; route via expert-liaison
5. **Transparency enforcement:** Ensure all calculations are logged with inputs, method, output

## Hydro-Sensitive Code Changes (Auto-Trigger)

When these files are modified, hydro-reviewer is automatically invoked:
- `pipeline/hydrograph.py` — NRCS DUH, Tc calculation
- `pipeline/streamstats.py` — Peak flow estimation, regression equations
- `pipeline/model_builder.py` — Manning's n assignment, BC setup
- `pipeline/watershed.py` — Delineation parameters, area calculations
- `.claude/rules/scientific-validation.md` — Bounds changed
- Any test file testing the above modules

## Review Checklist

### For hydrograph.py
- [ ] Kirpich Tc formula: `Tc = 0.0078 * L^0.77 * S^(-0.385)` implemented correctly?
- [ ] Units correct? L in feet, S in ft/ft, result in minutes?
- [ ] Conversion to hours: divide by 60?
- [ ] Peak rate factor (484 vs. 300) logic clear?
- [ ] NRCS DUH ordinates match Table 16-1 (NEH Part 630, Ch 16)?
- [ ] Hydrograph smoothness checked (no negative flows)?
- [ ] Tc range validation: 0.25–15 hr for typical IL watersheds?
- [ ] Logging includes inputs, method, intermediate steps, result?

### For streamstats.py
- [ ] API query parameters correct (pour point, region code)?
- [ ] Fallback to IL regression equations implemented?
- [ ] Monotonicity check: Q2 < Q10 < Q100 < Q500?
- [ ] Confidence intervals preserved and logged?
- [ ] StreamStats vs. regression difference documented (if >±20%)?
- [ ] Unit peak flow (csm) reasonableness checked?
- [ ] Alternative data sources mentioned (USGS NWIS, local gages)?

### For model_builder.py
- [ ] Manning's n lookup table matches NLCD 2021 classes?
- [ ] n values within safety bounds (table in scientific-validation.md)?
- [ ] Area-weighted average formula correct?
- [ ] Fallback for unmapped NLCD classes (default n value)?
- [ ] Perimeter from watershed boundary correctly converted to .g## coordinates?
- [ ] Boundary conditions (slope, discharge, gates) documented?
- [ ] Simulation time window reasonable (warm-up + hydrograph + recession)?
- [ ] Template selection logic (match drainage area)?

### For watershed.py
- [ ] Pour point snapping: logic and threshold (e.g., 300m)?
- [ ] Minimum stream area threshold justification?
- [ ] D8 flow direction + accumulation correctly applied?
- [ ] Watershed polygon closure verified?
- [ ] Drainage area, relief, channel length calculations match standard formulas?
- [ ] CRS is EPSG:5070?

### General
- [ ] All calculations logged with transparency (inputs, method, output)?
- [ ] Parameter values documented with source and confidence level?
- [ ] Assumptions vs. alternatives discussed?
- [ ] HITL decisions identified and routed?

## Output Format

Write findings to `.claude/outputs/hydro-reviewer/{date}-{watershed|module}-review.md`:

```markdown
# Scientific Review: {Module/Watershed}

**Hydro-Reviewer:** {reviewer}  
**Date:** {date}  
**Status:** PASS | PASS with NOTES | FLAGGED

## Summary
{1-2 sentence overall assessment}

## Findings

### [CRITICAL/WARNING/INFO] {Finding Title}
- **Location:** `{file}:{line}` or {equation/method name}
- **Issue:** {Description}
- **Expected:** {What the correct behavior should be}
- **Reference:** {Source — NRCS, USGS, Chow, HEC-RAS manual, etc.}
- **Recommendation:** {Suggested fix or HITL question}

## Severity Levels
- **CRITICAL:** Scientifically incorrect results (wrong formula, unit error, sign error, out-of-bounds parameters)
- **WARNING:** Questionable assumptions, parameter values edge-cases, missing documentation
- **INFO:** Suggestions, best practices, documentation gaps

## HITL Questions (if any)

If review identified decisions requiring Glenn's expertise:
1. {Question 1}
   - Context: {relevant data}
   - Action: Queued via expert-liaison
2. ...
```

## Examples

### Example 1: Tc Formula Audit

```markdown
# Scientific Review: Kirpich Tc Implementation

**Status:** PASS with NOTES

## Findings

### [INFO] Tc formula implementation
- **Location:** `pipeline/hydrograph.py:line 84-95`
- **Method:** Kirpich formula: Tc = 0.0078 * L^0.77 * S^(-0.385)
- **Implementation:**
  ```python
  tc_min = 0.0078 * (length_feet ** 0.77) * (slope ** -0.385)
  tc_hr = tc_min / 60
  ```
- **Assessment:** ✓ Correct. Units properly handled (L feet → result min → convert to hr).
- **Reference:** NRCS NEH Part 630, Section 16.1

### [WARNING] Tc validation range
- **Location:** Pipeline does not validate Tc bounds
- **Issue:** Tc < 0.25 hr or Tc > 15 hr not flagged for expert review
- **Expected:** Automatic logging and HITL trigger if Tc outside typical range
- **Recommendation:** Add validation logic (see rules/scientific-validation.md)

### [INFO] Logging completeness
- **Location:** `pipeline/hydrograph.py:line 100-110`
- **Assessment:** Good transparency — logs L, S, Tc_min, Tc_hr. Recommend adding:
  - Expected range for this watershed size/region
  - Comparison to SCS CN method (alternative Tc formula)
  - Confidence/assumptions
```

### Example 2: Manning's n Audit

```markdown
# Scientific Review: Manning's n Assignment

**Status:** FLAGGED (HITL question)

## Findings

### [CRITICAL] Missing Manning's n for new NLCD classes
- **Issue:** NLCD 2021 updated Developed classes (21–24). Code only handles 21–23.
- **Missing:** Class 24 (Developed open space)
- **Impact:** If watershed contains class 24, will default to 0.040 — may be too low
- **Typical value for class 24:** 0.050–0.080 (open developed, sparse structures)
- **Recommendation:** Add class 24 with n=0.065 ± Glenn confirmation

### [HITL] Floodplain vs. channel Manning's n
- **Question:** Should we use different n values for (a) main channel vs. (b) floodplain?
  - Code currently assigns single n to entire 2D area
  - HEC-RAS supports spatially-varying n (cell-by-cell)
  - Best practice (FEMA): channel n ~0.04, floodplain n ~0.06–0.10
- **Context:** For 95 mi² IL agricultural watershed, are current uniform n values acceptable for regulatory use?
- **Action:** Queued for Glenn review
```

## When to Write Output

- Every time hydro-reviewer is auto-triggered (on code change)
- Always write findings, even if status is PASS (for documentation)
- Keep outputs organized by date and module name

---
```

### Agent 2.2: New qaqc-validator/SUBAGENT.md

**File:** `.claude/agents/qaqc-validator/SUBAGENT.md`

```markdown
---
model: sonnet
tools: Read, Write, Bash, Grep, Glob
description: Reflexive QAQC validator — runs automatically after stages complete, checks bounds, flags anomalies
---

# QAQC Validator

You are an automated quality assurance agent. Your role is to run validation checks after each pipeline stage completes and flag anomalies for expert review.

## Core Responsibilities

1. **Automatic post-stage validation:** Triggered after stages 1–7 complete
2. **Bounds checking:** Cross-check outputs against physical reasonableness bounds
3. **Anomaly flagging:** Surface out-of-range values, missing data, inconsistencies
4. **Reflexive validation:** Part of the cognitive loop, not post-hoc review
5. **Expert escalation:** Queue HITL questions via expert-liaison when anomalies detected

## QAQC Workflow

### Trigger Points

QAQC-validator is auto-triggered after:
1. Terrain.py completes → check DEM stats
2. Watershed.py completes → check drainage area, relief
3. Streamstats.py completes → check peak flows, confidence, API vs. regression
4. Hydrograph.py completes → check Tc, hydrograph shape
5. Model_builder.py completes → check Manning's n assignment, BCs
6. Runner.py completes → check HEC-RAS success, HDF5 structure
7. Results.py completes → check rasters, flood extent, depths

### Pre-Stage Validation

Before proceeding to next stage, validate:

**After Terrain:**
```
✓ CRS is EPSG:5070?
✓ DEM has <5% NoData?
✓ Relief is between 50–1500 ft?
✓ GeoTIFF is Cloud-Optimized?
```

**After Watershed:**
```
✓ Drainage area within ±15% of expected (if available)?
✓ Pour point snap distance < 500m?
✓ Watershed polygon is closed and valid?
✓ Main channel length > 2 km?
```

**After Peak Flow Estimation:**
```
✓ Q2 < Q10 < Q100 < Q500 (monotonic)?
✓ Unit peak flow (csm) is 50–800 csm?
✓ If API + regression both available: ±20% difference flagged?
✓ Confidence intervals documented?
```

**After Hydrograph:**
```
✓ Tc is 0.25–15 hr (typical for watershed area)?
✓ Peak from hydrograph ≈ input Q (±5%)?
✓ Hydrograph is smooth (no negative flows)?
✓ Duration is 1.5–3.0 × Tp?
```

**After Model Build:**
```
✓ Template area within ±25% of target area?
✓ Manning's n assigned to >95% of cells?
✓ Downstream BC slope is reasonable (0.0005–0.005 ft/ft)?
✓ Simulation window is >2 × hydrograph duration?
```

**After HEC-RAS Runner:**
```
✓ Run completed without errors?
✓ HDF5 output contains expected groups/datasets?
✓ Max WSE < DEM max + 10 ft (sanity check)?
✓ Time stepping was stable (no RAS warnings)?
```

**After Results Export:**
```
✓ COG GeoTIFFs are valid, have overviews?
✓ Flood extent polygon is valid (closed, no self-intersections)?
✓ Max flood depth is 0.1–30 ft (typical for this area)?
✓ All return periods (10/50/100yr) present?
✓ GeoPackage metadata complete?
```

## Implementation Pattern

### Per-Stage Validation Function

```python
def validate_stage(stage_name, output_dict, bounds_dict):
    """
    Run QAQC checks for a stage.
    
    Args:
        stage_name: str (e.g., "terrain", "watershed", "streamstats")
        output_dict: dict with stage outputs
        bounds_dict: validation bounds from rules/scientific-validation.md
    
    Returns:
        ValidationResult with status (PASS/WARN/FAIL), findings list
    """
    findings = []
    
    # Apply bounds from scientific-validation.md
    for key, (lower, upper) in bounds_dict.items():
        value = output_dict.get(key)
        if value is not None and (value < lower or value > upper):
            findings.append({
                'level': 'WARNING' if (value > lower*0.9 and value < upper*1.1) else 'CRITICAL',
                'message': f"{key}={value} outside bounds [{lower}, {upper}]",
                'context': output_dict
            })
    
    # Perform cross-checks
    findings.extend(_cross_checks(stage_name, output_dict))
    
    status = 'PASS' if not findings else ('WARN' if all(f['level']=='WARNING' for f in findings) else 'FAIL')
    return ValidationResult(status=status, findings=findings, stage=stage_name)
```

### Integration with Orchestrator

The orchestrator calls `qaqc_validator.validate_stage()` after each stage:

```python
# In orchestrator.py
result_stage_1 = terrain.get_terrain(...)
validation_1 = qaqc_validator.validate_stage('terrain', result_stage_1, bounds['terrain'])
if validation_1.status == 'FAIL':
    logger.error(f"Stage 1 validation failed: {validation_1.findings}")
    raise OrchestratorError(...)
elif validation_1.status == 'WARN':
    logger.warning(f"Stage 1 warnings: {validation_1.findings}")
    expert_liaison.flag_for_review(validation_1)  # Queue for Glenn
```

## Output: QAQC Report

After orchestrator completes, qaqc-validator generates a full report:

**File:** `.claude/outputs/qaqc-validator/{date}-{run_id}-qaqc.md`

```markdown
# QAQC Report: {Watershed} Q100 Modeling

**Date:** {date}  
**Run ID:** {run_id}  
**Watershed:** {name}, {area_mi2} mi²  
**Overall Status:** PASS | PASS with WARNINGS | FAIL

## Summary
- **Stages validated:** 7/7
- **Findings:** {critical_count} critical, {warning_count} warnings, {info_count} info
- **Blocked:** {yes/no}
- **HITL questions:** {count} queued

## Stage-by-Stage Results

| Stage | Status | Key Metrics | Notes |
|-------|--------|-------------|-------|
| Terrain | PASS | relief=420m, nodata=1.2% | ✓ DEM quality good |
| Watershed | PASS | area=95.3 mi², relief=420m | ✓ Typical for IL ag |
| Peak flow | WARN | Q100=9,384 CFS, csm=98.4 | API/regression 18% diff; logged |
| Hydrograph | PASS | Tc=4.2hr, peak=9,384 CFS | ✓ Reasonable |
| Model build | PASS | n coverage=100%, BCs ok | ✓ All cells assigned |
| Runner | PASS | 2400 sec, no errors | ✓ HEC-RAS success |
| Results | WARN | depth_max=2.1ft, area=45 mi² | ✓ Within typical bounds |

## Critical Findings
(none)

## Warnings
1. **Peak flow API/Regression discrepancy** [Stage 3]
   - StreamStats: 9,384 CFS
   - IL Regression: 11,200 CFS
   - Difference: +19.4%
   - Action: Logged. Expert flagged for Glenn decision.

...

## HITL Questions Queued
1. Peak flow reconciliation (API vs. regression) — to expert-liaison

---
```

---
```

### Agent 2.3: New expert-liaison/SUBAGENT.md

**File:** `.claude/agents/expert-liaison/SUBAGENT.md`

```markdown
---
model: haiku
tools: Read, Write, Bash
description: Expert liaison — manages HITL questions, queues them for Glenn, tracks responses
---

# Expert Liaison

You are the interface between agents and Glenn. Your role is to:
1. **Formalize questions** from agents into structured domain queries
2. **Queue them** for Glenn's review (via Telegram or another channel)
3. **Track responses** and communicate them back to queuing agents
4. **Surface uncertainty** rather than allow silent autonomous decisions

## Core Responsibilities

1. **Question intake:** Accept HITL questions from other agents
2. **Formatting:** Structure questions as `/ask-expert` skill calls (see skills section)
3. **Queueing:** Log questions to `.claude/outputs/expert-liaison/questions-queue.md`
4. **Notification:** Alert Glenn (Telegram) that domain questions need his input
5. **Response tracking:** When Glenn responds, propagate decision to queuing agents
6. **Escalation:** Flag high-urgency questions (blocking pipeline progress)

## Question Types

### Urgent (Blocks Pipeline)
- Should we use Kirpich or SCS CN for Tc? (before hydrograph generation)
- Which peak flow source is authoritative? (API vs. regression discrepancy)
- Should we accept this template mesh? (before model build)

→ **Action:** Immediately notify Glenn. Wait for response before proceeding.

### High Priority (Flagged for Review)
- Manning's n assignments out of typical range
- Watershed characteristics unusual (flat/steep, small/large)
- Hydrograph shape or peak look wrong
- Flood depth outside expected bounds

→ **Action:** Log to queue. Notify Glenn at next checkpoint. Proceed with caution (flag output as "pending expert review").

### Informational (Logged)
- Transparency logs for methods chosen
- Alternative methods considered
- Assumptions documented

→ **Action:** Log to queue. Include in run report. No notification required unless Glenn asks.

## Question Format (Internal)

```python
{
    "urgency": "blocking" | "high" | "info",
    "category": "methodology" | "uncertainty" | "anomaly" | "bounds",
    "question": "string (human-readable question)",
    "context": {
        # Relevant data for Glenn's decision
        "watershed_id": "string",
        "parameter_name": "string",
        "current_value": float or dict,
        "typical_range": [float, float],
        "source": "string (how was this determined?)",
        "alternatives": ["list of alternatives Glenn might consider"],
    },
    "queued_by": "agent_name",
    "queued_at": "ISO timestamp",
    "deadline": "ISO timestamp or null (null = not urgent)",
}
```

## Queueing Mechanism

### File: `.claude/outputs/expert-liaison/questions-queue.md`

```markdown
# Expert-Liaison Questions Queue

**Last updated:** {timestamp}  
**Status:** {count} blocking, {count} high-priority, {count} informational  

## Blocking (Waiting for Response)

### Q1: Peak Flow Source Selection
- **From:** qaqc-validator (stage 3, 2026-03-14 10:23)
- **Question:** StreamStats API returned 9,384 CFS; IL regression returned 11,200 CFS (±19.4%). Which is authoritative?
- **Context:**
  - Watershed: 95.3 mi²
  - StreamStats CI: [7,200–12,100] CFS (95%)
  - Pour point: 40.1165°N, 88.2431°W
- **Action:** Pipeline paused. Awaiting Glenn's decision.
- **Response:** (pending)

...

## High Priority (Flagged)

### Q2: Manning's n Assignment — Class 24
- **From:** hydro-reviewer (2026-03-14 10:15)
- **Question:** NLCD class 24 (Developed open space) not in lookup table. Use default n=0.040 or 0.065?
- **Context:** ~2% of 2D area is class 24. Current default: 0.040. Typical range: 0.050–0.080.
- **Action:** Proceeding with n=0.040. Flagged for Glenn review in run report.
- **Response:** (pending review)

...

## Informational (Logged)

### I1: Kirpich Tc Method
- **From:** hydrograph.py (2026-03-14 09:45)
- **Note:** Kirpich Tc formula used (Glenn approved 2026-02-28 for IL ag watersheds)
- **Tc result:** 4.2 hr (typical for 95 mi²)
- **Status:** Logged for provenance.

...
```

## Integration with Orchestrator

**Pseudo-code:**

```python
# In orchestrator.py
try:
    result = stage_function(...)
    validation = qaqc_validator.validate_stage(...)
    if validation.findings:
        for finding in validation.findings:
            if finding['level'] == 'CRITICAL' and finding['blocking']:
                expert_liaison.queue_question(
                    urgency="blocking",
                    question=finding['question'],
                    context=finding['context'],
                    deadline=datetime.now() + timedelta(minutes=15)
                )
                # Wait for response or timeout
                response = expert_liaison.wait_for_response(timeout_sec=300)
                if response['action'] == 'abort':
                    raise OrchestratorError(f"Aborted by Glenn: {response['reason']}")
                elif response['action'] == 'proceed_with_note':
                    logger.warning(f"Proceeding with Glenn's note: {response['note']}")
except ExpertDeferralError as e:
    logger.error(f"Pipeline paused: {e}")
    raise
```

---
```

---

## Part 3: New Skills (3 files)

### Skill 3.1: ask-expert.md

**File:** `.claude/skills/ask_expert/SKILL.md`

```markdown
---
name: ask-expert
description: Formalize and queue a domain question for Glenn (expert liaison)
user_invocable: true
agent: expert-liaison
---

# /ask-expert

Formalize a domain question and queue it for Glenn's expert review.

## Usage

```bash
/ask-expert --category methodology --question "Should we use Kirpich or SCS CN for Tc?" \
  --context "watershed=95 mi², terrain=central IL ag, area_bounds=10-500 mi²" \
  --urgency blocking
```

Or structured JSON:

```json
{
  "urgency": "blocking",
  "category": "methodology",
  "question": "Should we use Kirpich or SCS CN for Tc?",
  "context": {
    "watershed_id": "demo_95mi2",
    "parameter_name": "Tc_formula",
    "current_value": "Kirpich (4.2 hr)",
    "typical_range": [1, 8],
    "alternatives": ["Kirpich", "SCS CN", "USGS regression"]
  }
}
```

## Steps

1. **Validate question format:** Ensure all required fields present
2. **Assign ID:** Auto-generate unique Q{n} identifier
3. **Log to queue:** Append to `.claude/outputs/expert-liaison/questions-queue.md`
4. **Notify Glenn:** Send Telegram message (if urgency=blocking or high)
   - Include question text + key context
   - Provide link to full queue details
5. **Return confirmation:** Queue ID + status + expected response time

## Response Tracking

Glenn responds in Telegram (or replies in-channel):
- `/expert-response Q1 use streamstats 9384 cfs with regression fallback noted`

expert-liaison receives response, updates queue file, communicates decision back to queuing agent.

## Categories

- **methodology:** Which formula, method, or source to use
- **uncertainty:** Interpreting conflicting data or anomalies
- **anomaly:** Flagging unusual values for expert judgment
- **bounds:** Deciding whether to accept out-of-range values
- **calibration:** Comparing outputs to known benchmarks

---
```

### Skill 3.2: validate-run.md

**File:** `.claude/skills/validate_run/SKILL.md`

```markdown
---
name: validate-run
description: Post-run QAQC validation — check outputs against bounds, generate QAQC report
user_invocable: true
agent: qaqc-validator
---

# /validate-run

Run post-execution QAQC validation on a completed orchestrator run.

## Usage

```bash
/validate-run --run-id run_20260314_abc123 --output-dir ./output/test
```

## Steps

1. **Read orchestrator output:** Load result object from `output_dir/orchestrator_result.json`
2. **Load bounds:** Read validation bounds from `rules/scientific-validation.md`
3. **Run stage validators:** For each stage, check:
   - Inputs exist and are valid
   - Outputs fall within bounds
   - Cross-checks pass (monotonicity, closure, consistency)
4. **Compile findings:** CRITICAL/WARNING/INFO for each anomaly
5. **Generate QAQC report:** Write `.claude/outputs/qaqc-validator/{date}-{run_id}-qaqc.md`
6. **Queue HITL questions:** For each CRITICAL/blocking finding, queue via expert-liaison
7. **Return status:** PASS/WARN/FAIL + count of findings

## Output

```
✓ QAQC validation complete
  - Status: PASS with 2 warnings
  - Report: .claude/outputs/qaqc-validator/2026-03-14-run_abc123-qaqc.md
  - HITL questions: 1 queued (blocking)
```

## Notes

- Run this after every orchestrator execution
- Can be triggered automatically by orchestrator or user-invoked
- Warnings do not block subsequent use, but are flagged in run report

---
```

### Skill 3.3: calibrate.md

**File:** `.claude/skills/calibrate/SKILL.md`

```markdown
---
name: calibrate
description: Compare pipeline outputs against known benchmarks (gages, published FIRM, post-event surveys)
user_invocable: true
agent: qaqc-validator
---

# /calibrate

Compare model outputs to known benchmarks and document confidence.

## Usage

```bash
/calibrate --run-id run_20260314_abc123 --benchmark-type usgs-gage \
  --benchmark-file benchmarks/usgs_gage_2020_flood.csv
```

## Benchmarks Supported

1. **USGS Gage Data**
   - Peak stage (ft) at gage location
   - Peak discharge (CFS)
   - Recession curve
   - Format: CSV with timestamp, stage, discharge

2. **FEMA Flood Insurance Rate Map (FIRM)**
   - Published 100-year flood extent
   - Base flood elevation (BFE)
   - Floodway delineation
   - Format: GeoJSON or Shapefile

3. **Post-Event Survey**
   - High-water marks (HWM) from field survey
   - Locations + elevations
   - Format: CSV with lat, lon, hwm_elevation_ft

## Steps

1. **Load benchmark data** from file
2. **Match to model output:**
   - USGS gage → extract modeled depth/WSE at gage location
   - FIRM extent → compare flood polygon area, overlap %
   - HWM → compare modeled vs. surveyed elevation at each point
3. **Compute error metrics:**
   - Depth RMSE, MAE, bias
   - Extent overlap (% area agreement)
   - Elevation error distribution
4. **Generate calibration report:** Write `.claude/outputs/qaqc-validator/{date}-{run_id}-calibration.md`
5. **Flag concerns:** If RMSE > 1 ft or overlap < 70%, flag for expert review
6. **Return summary:** Error metrics + pass/caution/fail

## Example Report

```markdown
# Calibration Report: {Watershed} Q100 vs. USGS Gage

**Benchmark:** USGS 05573500 (Kaskaskia River near Shelbyville, IL)  
**Gage peak (2020):** 18.4 ft (18,200 CFS)  
**Model peak:** 18.1 ft (17,900 CFS)  

## Results

| Metric | Model | Benchmark | Error |
|--------|-------|-----------|-------|
| Peak stage | 18.1 ft | 18.4 ft | -0.3 ft (-1.6%) |
| Peak discharge | 17,900 CFS | 18,200 CFS | -300 CFS (-1.6%) |
| Recession (day 5) | 16.8 ft | 17.2 ft | -0.4 ft (-2.3%) |

## Assessment

✓ **PASS** — Depth error within FEMA tolerance (<0.5 ft). Model is suitable for regulatory use.

---
```

---
```

---

## Part 4: New/Updated Hooks (3 files)

### Hook 4.1: calculation-transparency.sh (NEW)

**File:** `.claude/hooks/calculation-transparency.sh`

```bash
#!/bin/bash
# PostToolUse hook: Enforce transparency logging after pipeline edits
# Receives JSON on stdin with tool_input.file_path and tool_response

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    fp = data.get('tool_input', {}).get('file_path', '')
    print(fp)
except: print('')
" 2>/dev/null)

# Only trigger for pipeline hydro-sensitive modules
if ! echo "$FILE_PATH" | grep -E 'pipeline/(hydrograph|streamstats|model_builder|watershed)\.py$' > /dev/null; then
    exit 0
fi

# Extract diff to check for calculation code changes
MODULE=$(basename "$FILE_PATH" .py)

cat <<EOF
[TRANSPARENCY CHECK] Pipeline module '${MODULE}' was modified.
- Verify all calculations log: inputs, method, output, confidence
- Check: logger.info() or logger.warning() calls document the math
- If equations changed (Tc, Manning's n, peak flow), ensure:
  1. Inputs logged with sources + units
  2. Formula/reference documented
  3. Intermediate steps shown (for complex calcs)
  4. Output logged with reasonableness context
  5. Confidence/assumptions logged
- After completing edits, consider running: /validate-run or /run-tests
EOF

exit 0
```

### Hook 4.2: range-guard.sh (NEW)

**File:** `.claude/hooks/range-guard.sh`

```bash
#!/bin/bash
# PostToolUse hook: Flag values outside expected bounds
# Detects when numerical outputs are assigned without bounds checking

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    fp = data.get('tool_input', {}).get('file_path', '')
    print(fp)
except: print('')
" 2>/dev/null)

# Only trigger for pipeline modules that produce key quantities
if ! echo "$FILE_PATH" | grep -E 'pipeline/(hydrograph|streamstats|model_builder|watershed)\.py$' > /dev/null; then
    exit 0
fi

TOOL_RESPONSE=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    resp = data.get('tool_response', '')
    if isinstance(resp, dict):
        resp = resp.get('stdout', resp.get('output', ''))
    print(str(resp)[:500])  # First 500 chars
except: print('')
" 2>/dev/null)

# Check for boundary-crossing assignments (simple pattern matching)
if echo "$TOOL_RESPONSE" | grep -qiE '(tc|mannings|peak|cell_size|depth)' && \
   echo "$FILE_PATH" | grep -qE '(hydrograph|model_builder|streamstats)'; then
    cat <<EOF
[RANGE GUARD] Pipeline edit detected (${FILE_PATH}).
- If you assigned any key quantities (Tc, Manning's n, peak flow, cell size, depth):
  1. Check against bounds in rules/scientific-validation.md
  2. Flag with logger.warning() if outside safety range
  3. Queue expert-liaison question if critical
- Examples of bounds:
  - Tc: 0.25–15 hr (typical for IL 10–500 mi²)
  - Manning's n: 0.02–0.15 (hydraulic bounds; NLCD typical ranges in table)
  - Peak flow: 50–800 csm (unit peak flow)
  - Cell size: 10–300 m (LiDAR limit to stability)
- Run /run-tests to verify bounds checking logic
EOF
fi

exit 0
```

### Hook 4.3: Updated qaqc-pipeline-edit.sh

**File:** `.claude/hooks/qaqc-pipeline-edit.sh` (updated)

```bash
#!/bin/bash
# PostToolUse hook: QAQC reminder + proactive hydro-reviewer trigger
# Receives JSON on stdin with tool_input.file_path

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    fp = data.get('tool_input', {}).get('file_path', '')
    print(fp)
except: print('')
" 2>/dev/null)

# Only trigger for pipeline Python files
if ! echo "$FILE_PATH" | grep -q 'pipeline/.*\.py$'; then
    exit 0
fi

MODULE=$(basename "$FILE_PATH" .py)

# Standard QAQC reminder
cat <<EOF
[QAQC] Pipeline module '${MODULE}' was modified.
- Verify tests pass: python -m pytest tests/test_${MODULE}.py -v (if file exists)
- Check mock mode: changes should work with --mock flag
EOF

# PROACTIVE HYDRO-REVIEWER TRIGGER for hydro-sensitive modules
if echo "$FILE_PATH" | grep -E 'pipeline/(hydrograph|streamstats|model_builder|watershed)\.py$' > /dev/null; then
    cat <<EOF
[HYDRO-REVIEWER] Hydro-sensitive module detected.
- Changes to ${MODULE} should be reviewed for scientific correctness
- Consider: Is hydro-reviewer auto-triggered? (check .claude/settings.json)
- If not auto-triggered, launch manually or update settings to include ${FILE_PATH}
- Key checks: formula correctness, units, parameter bounds, transparency logging
EOF
fi

exit 0
```

---

## Part 5: Updates to Root CLAUDE.md

**Add this section to `/Users/glennheistand/ras-agent/CLAUDE.md`:**

```markdown
## Human in the Loop (HITL) — Core Principle

Glenn is the domain expert. Agents must know when to pause and ask him vs. proceeding autonomously.

### When Agents Pause & Ask Glenn

1. **Choosing scientific methods** — Tc formula, peak flow source, Manning's n strategy
2. **Interpreting uncertainty or anomalies** — Results outside bounds, conflicting sources
3. **Setting domain-sensitive parameters** — Cell size, hydrograph duration, boundary conditions
4. **FEMA compliance decisions** — Anything affecting regulatory acceptability
5. **Deviating from standard IL practice** — When established methodology might not apply

### When Agents Proceed Autonomously

1. **Implementing Glenn's established decisions** — Once methodology is chosen, implementation is mechanical
2. **Data ingestion and format conversion** — Download, reproject, clip
3. **Mesh/model generation from template** — Cloning, updating files
4. **QAQC and validation checks** — Run automatically, flag findings
5. **Code implementation and testing** — Writing Python, fixing bugs

**See `.claude/rules/human-in-the-loop.md` for detailed decision tree.**

## Validation Bounds & Scientific Accuracy

All scientific calculations must be:
1. **Transparent** — Log inputs, method, output, confidence
2. **Bounded** — Cross-check against expected ranges (Manning's n, Tc, peak flow, cell size, depth)
3. **Validated** — QAQC runs automatically post-stage; anomalies flagged
4. **Documented** — Assumptions, sources, uncertainties preserved

**See `.claude/rules/scientific-validation.md` for bounds tables and ranges.**

---
```

---

## Part 6: Domain Knowledge Codification for pipeline/CLAUDE.md

**Add these sections to `/Users/glennheistand/ras-agent/pipeline/CLAUDE.md`:**

```markdown

---

## Domain Knowledge — Illinois Hydrology & HEC-RAS Modeling

### Manning's Roughness Coefficient Reference Table

Used by `model_builder.py` for NLCD-based roughness assignment. All values from USACE practice guidelines.

| NLCD Class | Code | Description | Typical n | Safety Bounds |
|------------|------|-------------|-----------|---------------|
| 11 | Open water | Lakes, ponds, rivers | 0.035 | 0.020–0.050 |
| 21 | Dev. low intensity | Sparse structures, golf courses | 0.080 | 0.050–0.120 |
| 22 | Dev. medium intensity | Mixed structures, streets | 0.100 | 0.060–0.150 |
| 23 | Dev. high intensity | Dense urban, parking | 0.120 | 0.070–0.180 |
| 24 | Dev. open space | Parks, utilities | 0.065 | 0.050–0.080 |
| 31 | Barren | Rock, sand, bare soil | 0.030 | 0.020–0.050 |
| 41 | Deciduous forest | Oak, maple, ash | 0.120 | 0.080–0.160 |
| 42 | Evergreen forest | Pine, fir, spruce | 0.140 | 0.090–0.180 |
| 43 | Mixed forest | Mixed deciduous/evergreen | 0.130 | 0.085–0.170 |
| 52 | Shrub/scrub | Low brush, young forest | 0.060 | 0.030–0.100 |
| 71 | Grassland | Pasture, grass, prairie | 0.035 | 0.025–0.060 |
| 81 | Pasture/hay | Mowed/grazed grassland | 0.033 | 0.025–0.060 |
| 82 | Cultivated crops | Row crops, alfalfa | 0.037 | 0.025–0.070 |
| 90 | Woody wetland | Swamp, bottomland forest | 0.080 | 0.050–0.150 |
| 95 | Herbaceous wetland | Marsh, sedge meadow | 0.080 | 0.050–0.150 |

**Implementation:** `model_builder.py` uses area-weighted average across 2D flow area cells.

### Time of Concentration (Tc) — Kirpich Formula

**Standard method for Illinois ungauged agricultural watersheds (10–500 mi²).**

Formula:  
```
Tc = 0.0078 * L^0.77 * S^(-0.385)
where:
  L = main channel length (feet)
  S = main channel slope (ft/ft)
  Result: Tc in minutes → divide by 60 for hours
```

**Typical IL ranges by drainage area:**
| Area (mi²) | Typical Tc Range |
|-----------|-----------------|
| 10 | 0.5–2.0 hr |
| 50 | 1.5–5.0 hr |
| 100 | 2.5–7.0 hr |
| 200 | 4.0–10.0 hr |
| 500 | 6.0–12.0 hr |

**Validity bounds:** 0.25–15 hr (typical). Tc < 0.25 hr (very small, steep) or > 15 hr (large, flat) flagged for expert review.

**Implementation:** `hydrograph.py:nrcs_unit_hydrograph()` uses Kirpich for Tc calculation.

### Peak Flow Estimation — StreamStats & IL Regression

**Regulatory standard: FEMA 100-year return period (Q100), CFS.**

Two sources (in preference order):
1. **USGS StreamStats API** (region=IL)
   - Returns Q2, Q5, Q10, Q25, Q50, Q100, Q500
   - Includes 95% confidence intervals
   - Regional regression equations fitted to IL USGS gages
   - Most reliable when drainage area 10–500 mi²

2. **Illinois Regression Fallback** (USGS SIR 2008-5176, Soong et al. 2008)
   - Use if API unavailable
   - Equations vary by latitude region (northern, central, southern IL)
   - Typically within ±20% of StreamStats API result
   - Document discrepancy if > ±20%

**Unit peak flow (csm) validation:**
```
csm = Q100_cfs / drainage_area_mi2
Typical range: 50–150 csm for IL
Safety range: 30–800 csm
```

**Implementation:** `streamstats.py:get_peak_flows()` queries API; falls back to IL regression equations.

### Peak Rate Factor (Fp) — NRCS Selection

Used to convert Design Storm peak to synthetic hydrograph peak.

| Terrain | Fp Value | Guidance |
|---------|----------|----------|
| Standard (0.5% < slope < 5%) | 484 | Most IL agricultural watersheds |
| Flat (slope < 0.5%) | 300 | Prairie, deltaic plains |
| Steep (slope > 5%) | 600 | Mountain regions (rare in IL) |

**For Illinois:** Default to 484 unless watershed is predominantly flat (< 0.5% mean slope).

**Implementation:** `hydrograph.py:nrcs_unit_hydrograph()` accepts `peak_rate_factor` parameter.

### Hydrograph Generation — NRCS Dimensionless Unit Hydrograph

**Standard: NEH Part 630, Chapter 16 (NRCS Technical Release 55).**

Dimensionless ordinates (30 points, t/Tp vs. q/qp) applied to:
```
Peak discharge: qp = (Fp * A) / Tp
  where: Fp = peak rate factor
         A = drainage area (mi²)
         Tp = time to peak (hr)

Time to peak: Tp = Tc/2 + Tlag
Lag time: Tlag = 0.6 * Tc
```

Hydrograph duration: 1.5–3.0 × Tp typical (full recession to baseflow).

**Implementation:** `hydrograph.py:generate_hydrograph_set()` generates all return period hydrographs (Q2, Q10, Q25, Q50, Q100, Q500).

### Cell Size (2D Mesh Resolution) Guidance

Trade-off between terrain fidelity, computational cost, and stability.

| Watershed Area | Recommended | Min (LiDAR) | Max (Stability) |
|----------------|------------|------------|-----------------|
| <50 mi² | 15–30 m | 10 m | 50 m |
| 50–150 mi² | 25–60 m | 10 m | 100 m |
| 150–300 mi² | 50–100 m | 15 m | 150 m |
| >300 mi² | 75–200 m | 20 m | 300 m |

**Selection logic (in order):**
1. Minimum: LiDAR resolution (typically 3–10 m; ILHMP is 1–3 m)
2. Recommend: Balance detail vs. compute time (usually 30–100 m for central IL)
3. Maximum: Stability (CFL number, flow routing accuracy)

**Implementation:** `model_builder.py:_select_cell_size()` chooses based on drainage area.

### Flood Depth Accuracy Target

FEMA regulatory standard: **<0.5 ft absolute error** vs. known benchmarks (gages, post-event surveys).

| Use | Target | Margin |
|-----|--------|--------|
| FEMA FIRM (regulatory) | <0.5 ft | ±0.25 ft |
| Engineering study | <1.0 ft | ±0.5 ft |
| Planning-level estimate | <2.0 ft | ±1.0 ft |

Achieved through:
- High-resolution DEM (3–10 m)
- Adequate mesh cell size (30–100 m)
- Accurate Manning's n assignment
- Proper boundary conditions + warm-up period

**Implementation:** `results.py` exports depth raster; QAQC compares to available benchmarks via `/calibrate` skill.

### Illinois-Specific Watershed Characteristics

**Typical ranges for central/northern IL, 10–500 mi² agricultural drainage basins:**

| Parameter | Typical Range | Safety Bounds |
|-----------|---------------|---------------|
| Relief (max – min elevation) | 200–600 ft | 50–1500 ft |
| Mean slope | 0.5–2.5% | 0.1–5.0% |
| Channel length/area ratio | 0.5–1.2 mi/mi² | 0.3–2.5 mi/mi² |
| Drainage density | 1–3 mi/mi² | 0.5–5 mi/mi² |
| Forest fraction (NLCD) | 10–40% | 0–100% |
| Cultivated crop fraction | 40–70% | 0–100% |

**Implications:**
- High relief + steep slopes → smaller Tc, higher unit peak flow
- Flat, low-relief → larger Tc (300 peak rate factor), lower unit peak flow
- Mixed forest/agriculture → typical Manning's n ~0.050–0.070 (blend of crops + forest)

### StreamStats Regression Uncertainty

Confidence intervals from StreamStats API represent ±95% uncertainty bands around fitted regression equations.

**Typical IL ranges:**
- Small basins (10–50 mi²): ±30–40% confidence interval
- Medium basins (50–200 mi²): ±25–35% confidence interval
- Large basins (200–500 mi²): ±20–30% confidence interval

**Use case:** When comparing alternative peak flow sources, reconcile within ±20% before escalating to expert-liaison.

**Documentation:** Always include confidence intervals in run reports and metadata. Do not report Q100 as a single value; include CI range.

### Reference Documents

- **NRCS NEH Part 630, Chapter 16:** Dimensionless Unit Hydrograph
- **NRCS NEH Part 630, Chapter 13:** Time of Concentration
- **USGS SIR 2008-5176** (Soong et al.): Illinois Peak-Flow Regression Equations
- **HEC-RAS 6.6 User Manual:** 2D Unsteady Flow Modeling
- **USACE VB-Nexus Manning's Roughness Table:** HEC-RAS Standard Values
- **Chow, V. T. (1959):** Open Channel Hydraulics (foundational reference)

---
```

---

## Part 7: Implementation Checklist

Below is a step-by-step checklist for Glenn or an implementing agent to follow:

### Rules (4 new files to create)
- [ ] Create `.claude/rules/human-in-the-loop.md` (content in Part 1.1)
- [ ] Create `.claude/rules/scientific-validation.md` (content in Part 1.2)
- [ ] Create `.claude/rules/transparency.md` (content in Part 1.3)
- [ ] Create `.claude/rules/qaqc.md` (content in Part 1.4)

### Agents (1 new + 2 updated)
- [ ] **Update** `.claude/agents/hydro-reviewer/SUBAGENT.md` (content in Part 2.1)
  - Add proactive triggering, HITL awareness, transparency enforcement
- [ ] **Create** `.claude/agents/qaqc-validator/SUBAGENT.md` (content in Part 2.2)
  - New agent for reflexive validation
- [ ] **Create** `.claude/agents/expert-liaison/SUBAGENT.md` (content in Part 2.3)
  - New agent for HITL question routing

### Skills (3 new files)
- [ ] Create `.claude/skills/ask_expert/SKILL.md` (content in Part 3.1)
- [ ] Create `.claude/skills/validate_run/SKILL.md` (content in Part 3.2)
- [ ] Create `.claude/skills/calibrate/SKILL.md` (content in Part 3.3)

### Hooks (2 new + 1 updated)
- [ ] Create `.claude/hooks/calculation-transparency.sh` (content in Part 4.1)
- [ ] Create `.claude/hooks/range-guard.sh` (content in Part 4.2)
- [ ] **Update** `.claude/hooks/qaqc-pipeline-edit.sh` (content in Part 4.3)
  - Add proactive hydro-reviewer trigger

### Configuration Updates
- [ ] **Update** `.claude/settings.json`:
  - Add new hooks to `PostToolUse` for Edit/Write (calculation-transparency.sh, range-guard.sh)
  - Consider triggering hydro-reviewer on changes to hydrograph/streamstats/model_builder/watershed files

### Documentation Updates
- [ ] **Update** `/Users/glennheistand/ras-agent/CLAUDE.md`:
  - Add HITL section (Part 5)
  - Add Validation Bounds section (Part 5)

- [ ] **Update** `/Users/glennheistand/ras-agent/pipeline/CLAUDE.md`:
  - Add Manning's n table (Part 6)
  - Add Tc formula + ranges (Part 6)
  - Add Peak flow estimation guidance (Part 6)
  - Add Peak rate factor table (Part 6)
  - Add Hydrograph generation notes (Part 6)
  - Add Cell size guidance (Part 6)
  - Add Flood depth accuracy targets (Part 6)
  - Add IL watershed characteristics (Part 6)
  - Add StreamStats uncertainty (Part 6)
  - Add reference documents (Part 6)

### Update Self-Improver Rules (optional but recommended)
- [ ] Update `.claude/agents/self-improver/SUBAGENT.md` to:
  - Include audit checks for new agents (qaqc-validator, expert-liaison)
  - Include validation checks for new rules (human-in-the-loop, scientific-validation, transparency, qaqc)
  - Include skill count updates

---

## Part 8: Integration Notes

### How This Architecture Addresses Glenn's Requirements

1. **HITL is a first-class rule** ✓
   - Dedicated rule file with decision tree
   - Rules-based system knows when to pause vs. proceed
   - Expert-liaison agent enforces it

2. **Domain expert access** ✓
   - `/ask-expert` skill formalizes questions
   - expert-liaison queues them for Glenn
   - Questions surface uncertainty rather than silent assumptions

3. **Reflexive, autonomous QAQC** ✓
   - qaqc-validator runs automatically post-stage
   - Bounds checked against scientific-validation.md
   - Anomalies flagged; HITL questions queued if critical

4. **Transparent operations/calculations** ✓
   - transparency.md rule requires logging inputs, method, output
   - calculation-transparency.sh hook reminds agents to log
   - All scientific decisions documented in output

5. **Internal validation as core architecture** ✓
   - range-guard.sh hook flags out-of-range values
   - scientific-validation.md defines bounds for all key quantities
   - Never silently accept anomalies

### Interaction with Existing Agents

- **pipeline-dev:** Implements calculation logging per transparency.md; works with hydro-reviewer on methodology
- **test-engineer:** Ensures new validation checks are tested (via test-engineer)
- **orchestrator (main agent):** Coordinates delegation; waits for expert-liaison responses on blocking questions
- **hydro-reviewer:** Now proactive; automatically triggered on hydro-sensitive changes
- **self-improver:** May need updates to track new agents/rules (see implementation checklist)

---

## Summary

This architecture transforms Glenn's requirements into executable agent rules and skills. The four new rules codify:
- **When to ask** (human-in-the-loop.md)
- **What bounds are safe** (scientific-validation.md)
- **How to show the math** (transparency.md)
- **How to validate automatically** (qaqc.md)

Three new agents implement these principles:
- **qaqc-validator** — runs checks automatically
- **expert-liaison** — routes domain questions to Glenn
- **Updated hydro-reviewer** — proactively reviews hydro-sensitive code

Three new skills empower users and agents:
- **/ask-expert** — formalizes domain questions
- **/validate-run** — post-run QAQC report
- **/calibrate** — benchmark comparison

And three new hooks keep the system honest:
- **calculation-transparency.sh** — reminds agents to show the math
- **range-guard.sh** — flags out-of-range values
- **Updated qaqc-pipeline-edit.sh** — triggers hydro-reviewer proactively

All recommendations are ready to implement directly from this document.

---

**End of recommendations document.**

