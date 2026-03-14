---
name: improve
description: Recursive self-improvement — audit, fix, and validate .claude/ configuration
user_invocable: true
disable_model_invocation: true
agent: self-improver
---

# /improve

Recursive self-improvement meta-skill. Audits the `.claude/` configuration, applies fixes, and validates — up to 3 iterations.

## Mode Selection

- **`/improve`** — Full recursive improvement (audit → fix → validate loop via `self-improver` agent)
- **`/improve --plan-only`** — Read-only audit, produces plan without making changes
- **`/improve --quick`** — Same as `/self-audit` (quick consistency check)

## Recursive Improvement Steps

The `self-improver` agent runs up to 3 iterations:

### Iteration 1: Audit
1. Read all `.claude/` configuration files
2. Read all CLAUDE.md files (root, pipeline, tests, web) for ground truth
3. Scan codebase for current state:
   - Count pipeline modules (`pipeline/*.py`)
   - Count test files and test functions
   - Check web dependencies and brand colors
   - Check Docker/CI configuration
   - Verify hook scripts exist and paths are correct
4. Cross-reference ras-commander patterns (`C:\GH\ras-commander\.claude\`) for new ideas

### Iteration 2: Fix
5. Apply fixes to configuration files:
   - Update module lists, counts, and references
   - Update test baselines
   - Add missing module references to agent descriptions
   - Remove stale references
   - Update conventions to match current patterns
6. Write change summary to `.claude/outputs/self-improver/{date}-audit.md`

### Iteration 3: Validate
7. Re-read all modified files
8. Cross-reference against codebase again
9. Fix any remaining discrepancies (max 1 more pass)
10. Report final state

## Notes

- The `self-improver` agent can modify `.claude/` files but NOT source code
- Run after major feature work, new module additions, or when hooks flag drift
- Cross-references ras-commander for pattern ideas to adopt
- Maximum 3 iterations prevents runaway loops
