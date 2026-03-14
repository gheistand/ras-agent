---
name: module-map
description: Display pipeline data flow diagram showing how data moves between modules
user_invocable: true
allowed_tools: Read, Grep, Glob
---

# /module-map

Display the pipeline data flow — how data types flow between modules.

## Steps

1. **Read all pipeline modules** and identify:
   - Input parameters for each main function
   - Return types (dataclasses, dicts, paths)
   - Inter-module dependencies (which modules import which)

2. **Build a data flow diagram** showing:
   ```
   terrain.py ──[DEMResult]──→ watershed.py ──[WatershedResult]──→ streamstats.py
                                     │                                    │
                                     │                          [PeakFlowEstimates]
                                     │                                    │
                                     └──────────────→ hydrograph.py ──[HydrographSet]──→ model_builder.py
                                                                                              │
                                                                                        [HecRasProject]
                                                                                              │
                                                                                         runner.py
                                                                                              │
                                                                                        [HDF5 output]
                                                                                              │
                                                                                         results.py
   ```

3. **List key data types** with their fields and which module produces/consumes them

4. **Identify the orchestrator wiring** — show how `orchestrator.py` chains the stages

## Notes

- This is read-only — no files are modified
- Useful for understanding how the pipeline fits together before making changes
