---
name: add-module
description: Scaffold a new pipeline module with matching test file
user_invocable: true
disable_model_invocation: true
context: fork
agent: pipeline-dev
---

# /add-module

Scaffold a new pipeline module and its test file.

## Usage

```
/add-module validation     # creates pipeline/validation.py + tests/test_validation.py
```

## Steps

1. **Create `pipeline/{name}.py`** with:
   - Apache 2.0 copyright header (match existing files)
   - Module docstring describing purpose
   - Loguru logger setup: `from loguru import logger`
   - A placeholder main function with type hints
   - Bare import style

2. **Create `tests/test_{name}.py`** with:
   - sys.path setup matching existing test files
   - Import of the new module
   - A placeholder test function
   - Mock fixtures as needed

3. **Update `pipeline/CLAUDE.md`:**
   - Add the new module to the Module Map table

4. **Run the test:**
   ```bash
   python -m pytest tests/test_{name}.py -v
   ```

5. Report what was created and test results

## Notes

- The new module should follow all pipeline conventions (bare imports, EPSG:5070, graceful degradation)
- The test must pass before reporting success
