# .claude/ — Claude Code Configuration

This directory configures Claude Code's behavior for the RAS Agent project using a **three-tier agent model**.

## Architecture

```
┌─────────────────────────────────────────┐
│           Main Agent (Orchestrator)       │
│  Understands intent → delegates → summarizes │
└──────────┬──────────┬──────────┬────────┘
           │          │          │
    ┌──────▼──┐ ┌─────▼────┐ ┌──▼──────────┐
    │pipeline-│ │  web-dev  │ │test-engineer │  ...
    │  dev    │ │           │ │             │
    └─────────┘ └──────────┘ └─────────────┘
         Specialist Subagents (implement changes)

    ┌──────────────┐
    │hydro-reviewer│  ← Read-only domain advisor
    └──────────────┘
```

### Tier 1: Orchestrator (Main Agent)
The main Claude Code agent. Reads user intent, selects the right specialist, delegates work, and summarizes results. Rarely writes code directly.

### Tier 2: Specialist Subagents
Domain-specific workers that implement changes. Each has constrained tools, a working directory, and deep knowledge of their domain.

### Tier 3: Domain Advisors
Read-only agents that review code for domain correctness without modifying it. Currently: `hydro-reviewer` for scientific review.

## Directory Layout

```
.claude/
├── rules/          # Auto-loaded conventions (path-scoped or always-on)
├── agents/         # Subagent definitions (SUBAGENT.md per agent)
├── skills/         # User-invocable slash commands (SKILL.md per skill)
├── hooks/          # QAQC and self-improvement hook scripts
├── outputs/        # Subagent work products (gitignored)
└── settings.json   # Hooks config + permissions
```

## Rules (auto-loaded by path)
- `orchestrator.md` — Always loaded. Delegation protocol.
- `subagent-output-pattern.md` — Always loaded. How agents return results.
- `pipeline.md` — Loaded when working in `pipeline/**`
- `web.md` — Loaded when working in `web/**`
- `testing.md` — Loaded when working in `tests/**`
- `devops.md` — Loaded when working on Docker/CI files

## Agents (6)
- `pipeline-dev` — Python pipeline modules
- `web-dev` — React dashboard
- `test-engineer` — Test suite
- `devops` — Docker, CI, deployment
- `hydro-reviewer` — Scientific correctness (read-only)
- `self-improver` — Recursive config self-improvement (meta-agent)

## Skills (8 slash commands)
- `/run-tests` — Run pytest suite
- `/check-ci` — Local CI verification
- `/run-pipeline` — Mock pipeline execution
- `/pipeline-status` — Module overview with line counts and test mapping
- `/add-module` — Scaffold new pipeline module + test
- `/module-map` — Pipeline data flow diagram
- `/improve` — Recursive self-improvement (audit → fix → validate)
- `/self-audit` — Quick config consistency check (read-only)

## Hooks (4 QAQC hooks)
Configured in `settings.json`, scripts in `hooks/`:
- **Pipeline edit QAQC** (PostToolUse) — Reminds about testing after editing pipeline modules
- **Test count guard** (PostToolUse) — Warns if pytest count drops below baseline
- **Self-improvement detection** (PostToolUse) — Suggests `/self-audit` after `.claude/` edits
- **Pre-commit reminder** (PreToolUse) — Reminds to run `/check-ci` before git commit
