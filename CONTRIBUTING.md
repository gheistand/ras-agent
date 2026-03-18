# Contributing to RAS Agent

## Our Philosophy: LLM-Forward, Human in the Loop

RAS Agent was **built with LLM coding agents** and is **designed for LLM-assisted workflows**. We welcome contributions prepared with agent assistance — in fact, we require it.

This is safety-critical flood modeling software. Every PR must be reviewed by a coding agent using our style rules before submission. An agent-reviewed PR that follows the rules takes minutes to review. One that doesn't takes hours.

**Why it works**: The `.claude/` directory contains machine-readable style rules, domain specialist agents, and workflow templates. When your agent reads these before writing code, your contribution is consistent, tested, and easy to merge.

**Any agent works**: Claude Code, Codex CLI, Cursor, OpenCode, Gemini CLI — the rules are plain markdown, every LLM can read them.

---

## Quick Start

```bash
# 1. Fork and clone
git clone https://github.com/YOUR_USERNAME/ras-agent.git
cd ras-agent

# 2. Set up environment (macOS)
brew install gdal geos proj
pip install gdal==$(gdal-config --version)
pip install -r pipeline/requirements.txt pytest

# 3. Launch your agent
claude          # Claude Code
codex           # OpenAI Codex CLI
cursor .        # Cursor IDE
```

Your agent will find `CLAUDE.md` and `AGENTS.md` files throughout the repo. Have it read `.claude/rules/` before writing code.

---

## The Contribution Contract

1. **Before writing code**: Agent reads relevant rules in `.claude/rules/python/`
2. **Before submitting**: Run through the Self-Review Checklist (`.claude/rules/python/self-review-checklist.md`)
3. **When opening PR**: Fill out the PR template honestly

---

## Self-Review Checklist

See `.claude/rules/python/self-review-checklist.md` for the full checklist. Your agent can verify each item programmatically.

Key items:
- Google-style docstrings on all public functions
- Type hints on all signatures
- Tests for new functionality (count must not decrease from 125)
- All HTTP calls mocked in tests (no live network)
- EPSG:5070 for all geospatial operations
- Mock mode supported for new pipeline stages
- HITL guidelines followed for any automated data modification

---

## Domain-Specific Rules

This is flood modeling software. Before contributing to domain-sensitive areas:

| Area | Rule File | Key Requirement |
|------|-----------|-----------------|
| Hydrology | `.claude/rules/scientific-validation.md` | Cite authoritative sources (NRCS, USGS, HEC) |
| Automation | `.claude/rules/human-in-the-loop.md` | Licensed engineer reviews hydraulic outputs |
| Results | `.claude/rules/transparency.md` | Reproducible, auditable, GUI-verifiable |
| QAQC | `.claude/rules/qaqc.md` | Flag anomalies; never silently pass bad results |

---

## What We Accept

- Bug fixes with test validation
- New pipeline stages (terrain, hydrology, model building, results)
- New data source integrations (USGS, NLCD, NHD, etc.)
- Windows agent improvements (pipeline/windows_agent.py)
- Docker / deployment improvements
- Web dashboard features (web/src/)
- Documentation improvements

## What We Don't Accept

- Changes that reduce test count below 125
- Mock-free tests that require live network or HEC-RAS installation
- Hardcoded file paths or CRS values
- Changes to hydrologic methods without peer-reviewed citations
- PRs that bypass the self-review checklist

---

## Commit Messages

Use conventional commit format:

```
feat(terrain): Add USGS 3DEP fallback for non-IL watersheds
fix(runner): Handle RasUnsteady timeout on large meshes
docs(contributing): Add self-review checklist
refactor(results): Simplify COG export pipeline
test(windows_agent): Add RasPreprocess integration tests
```

---

## Attribution

RAS Agent is a collaboration between CHAMP (Illinois State Water Survey) and CLB Engineering Corporation. Key dependencies:

- [RAS Commander](https://github.com/gpt-cmdr/ras-commander) — William Katzenmeyer, P.E., CLB Engineering (Apache 2.0)
- HEC-RAS Linux binaries — US Army Corps of Engineers (public domain)
- [pysheds](https://github.com/mdbartos/pysheds) — Matt Bartos
- [pyHMT2D](https://github.com/psu-efd/pyHMT2D) — Xiaofeng Liu, Penn State

See README.md for full attribution.
