---
name: pipeline-status
description: Show pipeline module status — line counts, functions, test coverage mapping
user_invocable: true
allowed_tools: Read, Grep, Glob, Bash
---

# /pipeline-status

Display a status overview of all pipeline modules.

## Steps

1. **Module inventory:** For each `.py` file in `pipeline/`, count:
   - Total lines
   - Number of functions (`def `)
   - Number of classes (`class `)

2. **Test coverage mapping:** For each pipeline module, identify:
   - Corresponding test file(s)
   - Number of test functions in each test file

3. **Dependency check:** Note any modules that import optional dependencies (ras-commander, h5py, gdal)

4. **Output as table:**

```
Module              Lines  Funcs  Classes  Test File              Tests
──────────────────  ─────  ─────  ───────  ─────────────────────  ─────
runner.py            XXX    XX      X      test_runner.py           XX
terrain.py           XXX    XX      X      test_terrain.py          XX
streamstats.py       XXX    XX      X      (none — tested via orchestrator)
...
```

Note: Some modules (e.g., `streamstats.py`, `watershed.py`) lack dedicated test files — flag these as coverage gaps.

5. **Summary:** Total lines, total tests, any modules without test coverage

## Notes

- This is read-only — no files are modified
- Useful for understanding codebase health at a glance
