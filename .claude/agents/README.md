# Agents

Specialist subagents for the RAS Agent project. The main agent orchestrates; these agents implement.

## Agent Inventory

| Agent | Model | Domain | Tools | Writes Code? |
|-------|-------|--------|-------|-------------|
| `pipeline-dev` | sonnet | `pipeline/` modules | Read, Edit, Write, Bash, Grep, Glob | Yes |
| `web-dev` | sonnet | `web/` dashboard | Read, Edit, Write, Bash, Grep, Glob | Yes |
| `test-engineer` | sonnet | `tests/` suite | Read, Edit, Write, Bash, Grep, Glob | Yes |
| `devops` | sonnet | Docker, CI, deploy | Read, Edit, Write, Bash, Grep, Glob | Yes |
| `hydro-reviewer` | sonnet | Scientific review | Read, Write, Grep, Glob | **No** (writes findings to outputs/ only) |
| `self-improver` | sonnet | `.claude/` config | Read, Edit, Write, Bash, Grep, Glob | Yes (`.claude/` files only) |

## When to Use Each Agent

| Task | Agent |
|------|-------|
| "Fix the terrain download" | `pipeline-dev` |
| "Add a loading spinner" | `web-dev` |
| "Write tests for batch mode" | `test-engineer` |
| "Update the Dockerfile" | `devops` |
| "Is our Manning's n correct?" | `hydro-reviewer` |
| "Add a new stage and test it" | `pipeline-dev` + `test-engineer` (parallel) |
| "Config is stale after refactor" | `self-improver` (or `/improve`) |

## SUBAGENT.md Format

Each agent is defined by a `SUBAGENT.md` file in its directory with YAML frontmatter:

```yaml
---
model: sonnet          # or opus, haiku
tools: Read, Edit, Write, Bash, Grep, Glob
working_directory: pipeline/    # optional
description: One-line description
---
```

The markdown body contains the agent's instructions, domain knowledge, conventions, and post-change verification steps.

## Model Selection

All agents use `sonnet` — the project is small enough that sonnet handles all tasks well, and it keeps costs consistent. Upgrade to `opus` for specific agents if their tasks require deeper reasoning.
