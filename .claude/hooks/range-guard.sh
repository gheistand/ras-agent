#!/bin/bash
# PostToolUse hook: Remind about bounds checking after editing pipeline modules
# Triggered on Edit/Write to pipeline files

FILE_PATH="${CLAUDE_TOOL_INPUT_FILE_PATH:-}"

# Only trigger for pipeline modules that produce scientific quantities
case "$FILE_PATH" in
  *pipeline/hydrograph.py | *pipeline/streamstats.py | \
  *pipeline/model_builder.py | *pipeline/watershed.py | \
  *pipeline/results.py | *pipeline/orchestrator.py)
    cat <<EOF
[RANGE GUARD] Pipeline module edited — verify bounds checks.
Key bounds (from .claude/rules/scientific-validation.md):
  Tc:               0.25–15 hr     (HITL if outside)
  Q100 unit flow:   50–150 csm     (typical IL); 30–800 csm (safety bounds)
  Manning's n:      see table in scientific-validation.md by NLCD class
  Cell size:        10–300 m       (HITL if outside)
  Max flood depth:  0.1–30 ft      (HITL if > 30 ft)
  Flood extent:     < 40% of watershed area

If you assigned any of these values, ensure:
  1. logger.warning() fires when outside safety bounds
  2. expert_liaison.ask() is called (or documented) for HITL triggers
  3. [CALC] log entry documents the value and its validity check
EOF
    ;;
esac

exit 0
