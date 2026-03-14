---
name: check-ci
description: Run local CI verification (pytest + web lint + web build)
user_invocable: true
disable_model_invocation: true
context: fork
---

# /check-ci

Run the same checks that CI runs, locally.

## Steps

1. **Python tests:**
   ```bash
   python -m pytest tests/ -v --tb=short
   ```

2. **Web lint:**
   ```bash
   cd web && npm run lint
   ```

3. **Web build:**
   ```bash
   cd web && npm run build
   ```

4. Report results for each step:
   - Tests: count, passed, failed
   - Lint: pass/fail with any errors
   - Build: pass/fail with any errors

5. Overall verdict: PASS (all green) or FAIL (with details on what failed)

## Notes

- This mirrors the two CI jobs: `test-pipeline` and `build-web`
- Run this before pushing to `main` to catch issues early
- Web deps must be installed (`npm ci` in `web/`) — if `node_modules/` is missing, run it first
