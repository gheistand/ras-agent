---
description: LLM self-review checklist to run before opening any PR
---

# LLM Self-Review Checklist

Run through this checklist before opening any PR. Your agent can verify each item.

## Style Compliance
- [ ] Google-style docstrings on all public functions (Args, Returns, Raises with units)
- [ ] Type hints on all public function signatures
- [ ] `pathlib.Path` used internally; parameters accept both `str` and `Path`
- [ ] `snake_case` functions, `PascalCase` classes, `UPPER_SNAKE` constants
- [ ] `logging` (not `print`) for all operational output
- [ ] Lazy imports in api.py for heavy modules

## Domain Correctness
- [ ] CRS: all geospatial operations use EPSG:5070 (NAD83 Albers, metres)
- [ ] Graceful degradation: ras-commander → shutil fallback; StreamStats → IL regression fallback
- [ ] Mock mode: new pipeline stages support `mock=True` for tests

## Tests
- [ ] New functionality has corresponding tests in `tests/test_{module}.py`
- [ ] All HTTP calls mocked (no live network in tests)
- [ ] Tests pass with `python -m pytest tests/ -v` — count must not decrease
- [ ] No HEC-RAS installation required to run tests

## HITL / Safety
- [ ] Any automated action that modifies data follows HITL guidelines (.claude/rules/human-in-the-loop.md)
- [ ] Scientific/hydrologic decisions follow validation rules (.claude/rules/scientific-validation.md)
- [ ] Results are verifiable in HEC-RAS GUI

## Copyright
- [ ] New Python files include Apache 2.0 copyright header matching existing files
