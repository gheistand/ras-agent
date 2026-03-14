---
description: Calculation transparency — show inputs, method, and output for all scientific calculations
globs: pipeline/**
---

# Transparency in Calculations

Every scientific calculation must be logged with:
1. **Inputs** — parameter values and their sources
2. **Method** — equation name, reference, and assumptions
3. **Output** — result with units
4. **Validity** — pass/warn/fail against bounds in `scientific-validation.md`

Never log just the result. Show the reasoning.

## Log Format Standard

Prefix all transparency logs with `[CALC]` for easy filtering:

```python
logger.info("[CALC] %s: %s → %s [%s]", method_name, inputs_str, result_str, validity_str)
```

---

## Required Transparency Points

### Time of Concentration (hydrograph.py)

```python
logger.info(
    "[CALC] Tc (Kirpich): L=%.0f ft (longest flow path), S=%.5f ft/ft "
    "→ Tc=%.2f min = %.2f hr [%s]",
    L_ft, S_ftft, tc_min, tc_hr,
    "VALID" if 0.25 <= tc_hr <= 15 else "OUT OF RANGE — flagged for review"
)
# Reference: NRCS NEH Part 630, Section 16.1
```

### Peak Flows (streamstats.py)

```python
logger.info(
    "[CALC] Peak flows: source=%s, DA=%.1f mi², Q100=%.0f CFS (%.0f csm) [%s]",
    source, drainage_area_mi2, Q100, Q100 / drainage_area_mi2,
    "VALID" if 30 <= Q100 / drainage_area_mi2 <= 800 else "OUT OF RANGE"
)
# If both API and regression available:
logger.info(
    "[CALC] Peak flow cross-check: API=%.0f CFS, regression=%.0f CFS, diff=%.1f%% [%s]",
    api_q100, regression_q100,
    abs(api_q100 - regression_q100) / api_q100 * 100,
    "OK" if abs(api_q100 - regression_q100) / api_q100 < 0.20 else "DISCREPANCY — HITL"
)
# Always log confidence intervals
logger.info("[CALC] Q100 confidence interval: [%.0f, %.0f] CFS (95%%)", ci_lower, ci_upper)
```

### Hydrograph Generation (hydrograph.py)

```python
logger.info(
    "[CALC] NRCS DUH: peak_rate_factor=%d, Tp=%.2f hr, Qp=%.0f CFS (Q%d)",
    peak_rate_factor, tp_hr, qp_cfs, return_period
)
logger.info(
    "[CALC] Hydrograph volume: %.1f ac-ft (Q%d, duration=%.1f hr)",
    volume_ac_ft, return_period, duration_hr
)
```

### Manning's n Assignment (model_builder.py)

```python
logger.info(
    "[CALC] Manning's n: NLCD class %d (%s) → n=%.3f [safety bounds: %.3f–%.3f] [%s]",
    nlcd_code, nlcd_description, n_value, n_min, n_max,
    "VALID" if n_min <= n_value <= n_max else "OUT OF BOUNDS — flagged"
)
# For area-weighted composite:
logger.info(
    "[CALC] Manning's n composite: area-weighted average = %.3f "
    "(dominant class: %s at %.0f%%)",
    composite_n, dominant_class_name, dominant_pct
)
```

### Cell Size Selection (model_builder.py)

```python
logger.info(
    "[CALC] Cell size: DA=%.0f mi² → selected %dm "
    "(recommended %d–%dm, min %dm, max %dm) [%s]",
    drainage_area_mi2, selected_m,
    rec_min_m, rec_max_m, abs_min_m, abs_max_m,
    "VALID" if abs_min_m <= selected_m <= abs_max_m else "OUT OF RANGE"
)
```

### Mesh Perimeter Update (model_builder.py)

```python
logger.info(
    "[CALC] Perimeter update: %d vertices written to %s "
    "(bbox EPSG:5070: %.0f, %.0f → %.0f, %.0f)",
    vertex_count, geom_file_path,
    bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax
)
```

---

## What Must NOT Happen

```python
# ❌ BAD — result with no context
logger.info("Tc = 4.2 hr")

# ❌ BAD — inputs logged but no validity check
logger.info("L=50000 ft, S=0.002, Tc=4.2 hr")

# ✅ GOOD — full transparency
logger.info("[CALC] Tc (Kirpich): L=50000 ft (longest flow path), S=0.002 ft/ft "
            "→ Tc=252 min = 4.2 hr [VALID: typical for 95 mi² IL watershed]")
```

---

## Agents Must Enforce Transparency

- **hydro-reviewer:** When reviewing code, flag missing `[CALC]` logs as a WARNING finding
- **pipeline-dev:** Add `[CALC]` logging when implementing or modifying calculations
- **qaqc-validator:** Verify `[CALC]` log entries exist in run output for key quantities

## Preserve Uncertainty

Always document:
- Confidence intervals from StreamStats (not just point estimate)
- Alternative methods considered and why one was chosen
- Assumptions made (e.g., "Kirpich chosen per Glenn's guidance for this watershed class")
- Data quality flags (e.g., "DEM has 0.8% NoData in northwest corner — gap-filled by bilinear interpolation")
