---
name: self-audit
description: Quick validation of .claude/ configuration consistency against codebase
user_invocable: true
allowed_tools: Read, Grep, Glob, Bash
---

# /self-audit

Quick consistency check between `.claude/` configuration and the actual codebase. Read-only — reports issues but does not fix them.

## Steps

1. **Module inventory check:**
   ```bash
   ls pipeline/*.py | wc -l
   ```
   Compare against module count in `agents/pipeline-dev/SUBAGENT.md`

2. **Test baseline check:**
   ```bash
   python -m pytest tests/ --co -q 2>/dev/null | tail -1
   ```
   Compare against baseline in `rules/testing.md`

3. **Test file coverage check:**
   For each `pipeline/*.py`, check if `tests/test_*.py` exists.
   Compare against `agents/test-engineer/SUBAGENT.md` table.

4. **Brand color check:**
   Read `web/tailwind.config.js` colors.
   Compare against `agents/web-dev/SUBAGENT.md` description.

5. **CI structure check:**
   Read `.github/workflows/ci.yml` job names.
   Compare against `rules/devops.md` description.

6. **Hook script check:**
   Verify all hook scripts referenced in `settings.json` exist on disk.

7. **Cross-reference rules vs CLAUDE.md:**
   Check that conventions in rules match the CLAUDE.md files.

8. **Report findings** as a table:

```
Check                  Status   Details
─────────────────────  ──────   ───────
Module count           PASS     13 modules, agent says 13
Test baseline          WARN     114 tests, rules say 112 — update baseline
Test file coverage     PASS     11/13 modules have dedicated tests
Brand colors           PASS     navy, teal, amber match agent description
CI structure           PASS     2 jobs match devops rule
Hook scripts           PASS     4/4 scripts exist
Rules vs CLAUDE.md     PASS     No drift detected
```

## After Audit

- If issues found: suggest running `/improve` or launching `self-improver` agent
- If all pass: report clean bill of health
- Always report the test count for baseline tracking
