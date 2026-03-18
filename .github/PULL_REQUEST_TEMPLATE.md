## Summary

<!-- One paragraph: what this PR does and why -->

## Changes

<!-- Bullet list of files changed and what each does -->

## Self-Review Checklist

I have run through `.claude/rules/python/self-review-checklist.md` with my agent. Confirming:

### Style
- [ ] Google-style docstrings on all new/modified public functions
- [ ] Type hints on all public signatures
- [ ] `pathlib.Path` internally; parameters accept `str | Path`
- [ ] `logging` not `print` for operational output

### Tests
- [ ] New tests added for new functionality
- [ ] Test count has not decreased (currently 125 baseline)
- [ ] All tests pass: `python -m pytest tests/ -v`
- [ ] No live network calls in tests (all HTTP mocked)

### Domain
- [ ] EPSG:5070 used for all geospatial operations
- [ ] New pipeline stages support `mock=True`
- [ ] Hydrologic methods cite authoritative sources

### Safety
- [ ] HITL guidelines followed (`.claude/rules/human-in-the-loop.md`)
- [ ] Results verifiable in HEC-RAS GUI

## Agent Used

<!-- e.g. Claude Code, Codex CLI, Cursor — which rules did you load? -->

## Testing Notes

<!-- How did you test this? What environments? -->
