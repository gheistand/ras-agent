---
description: How subagents return work products and findings to the orchestrator
globs:
---

# Subagent Output Pattern

## Purpose

Subagents produce detailed findings, reviews, and analysis that may be too verbose for the main conversation. This pattern keeps the main context clean while preserving detailed work products.

## Output File Convention

Subagents write results to: `.claude/outputs/{agent-name}/{date}-{task-slug}.md`

Example: `.claude/outputs/hydro-reviewer/2026-03-13-mannings-n-audit.md`

## Output Format

```markdown
# {Task Title}

**Agent:** {agent-name}
**Date:** {YYYY-MM-DD}
**Status:** complete | partial | blocked

## Summary
{2-3 sentence executive summary}

## Findings
{Detailed findings, code references, recommendations}

## Actions Taken
{List of files modified, tests run, etc.}
```

## Orchestrator Responsibilities

1. After a subagent completes, read its output file if the returned summary lacks detail
2. Summarize key findings to the user — do not dump the full output
3. Reference the output file path so the user can review details

## Lifecycle

- **Active:** Files in `.claude/outputs/` are current work products
- **Archive:** Move stale outputs to `.claude/outputs/.old/` when no longer relevant
- **Delete:** Remove archived files after they've been superseded

## When Subagents Should Write Output Files

- Code reviews and audits (always)
- Scientific/hydrology reviews (always)
- Large implementation summaries (when >5 files changed)
- Pipeline status reports (always)

Subagents do NOT need output files for:
- Simple bug fixes (1-3 files)
- Running tests (just return pass/fail)
- Single-file edits
