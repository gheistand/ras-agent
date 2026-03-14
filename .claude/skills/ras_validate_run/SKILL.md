---
name: validate-run
description: Post-run QAQC validation — check all stage outputs against bounds, generate report, route HITL questions
user_invocable: true
agent: qaqc-validator
---

# /validate-run

Run post-execution QAQC validation on a completed orchestrator output directory.

## Usage

```
/validate-run ./output/test
/validate-run /app/output/watershed_123
/validate-run ./output/test --mode async      # don't block on HITL findings
```

## Steps

1. Read `.claude/rules/scientific-validation.md` for bounds
2. Load orchestrator outputs from `{output_dir}/`:
   - `jobs.db` — stage completion status and metadata
   - `terrain/` — DEM statistics
   - `model/` — project files, Manning's n coverage
   - `results/{rp}yr/` — depth rasters, flood extent polygons
   - `report.html` — summary stats (if generated)
3. Run per-stage validation checks (see `qaqc-validator/SUBAGENT.md` for full list)
4. Classify each check: PASS ✅ / WARN ⚠️ / HITL ❌
5. Generate QAQC report → `.claude/outputs/qaqc-validator/{date}-{run_id}-qaqc.md`
6. For any HITL findings:
   - Route via expert-liaison (`/ask-expert` format)
   - In `blocking` mode: wait for response
   - In `async` mode: flag output as "pending expert review"
7. Return: overall status + finding counts + report path

## Output

```
QAQC Validation Complete
  Run: ./output/test
  Overall: PASS with 1 warning
  Checks: 28 pass, 1 warn, 0 HITL
  Report: .claude/outputs/qaqc-validator/2026-03-14-test-qaqc.md
  HITL questions: 0 queued
```

## When to Run

- After every non-mock orchestrator run (recommended)
- Before using results for engineering or regulatory purposes
- After any pipeline parameter change to verify outputs remain valid
- Can also run on mock outputs to validate synthetic parameter choices

## Notes

- Mock runs still validate synthetic parameters (Tc, peak flows, Manning's n)
- WARN does not block use but is noted in the run report
- HITL means Glenn (or configured expert) must review before results are used
- `/calibrate` (benchmark comparison) is a separate skill — Phase C+
