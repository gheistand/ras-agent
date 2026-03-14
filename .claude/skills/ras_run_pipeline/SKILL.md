---
name: run-pipeline
description: Run the pipeline orchestrator in mock mode
user_invocable: true
disable_model_invocation: true
---

# /run-pipeline

Run the RAS Agent pipeline orchestrator in mock mode.

## Usage

```
/run-pipeline                                    # default IL coordinates
/run-pipeline --lon -88.578 --lat 40.021         # custom coordinates
```

## Steps

1. Create a temp output directory
2. Run the orchestrator:
   ```bash
   python pipeline/orchestrator.py --lon $LON --lat $LAT --output ./output/test --mock
   ```
   Default coordinates: `--lon -88.578 --lat 40.021` (Champaign, IL)
3. Report:
   - Which stages completed
   - Output files generated
   - Any errors or partial results
   - Total runtime

## Notes

- `--mock` flag means no HEC-RAS installation is needed
- Mock mode creates fake HDF5 output so downstream stages work
- Output goes to `./output/test/` — clean up when done
