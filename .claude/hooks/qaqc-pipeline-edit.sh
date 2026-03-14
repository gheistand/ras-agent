#!/bin/bash
# PostToolUse hook: QAQC reminder + proactive hydro-reviewer trigger on pipeline edits
# Triggered on Edit/Write to pipeline files

FILE_PATH="${CLAUDE_TOOL_INPUT_FILE_PATH:-}"

# Only trigger for pipeline Python files
case "$FILE_PATH" in
  *pipeline/*.py)
    MODULE=$(basename "$FILE_PATH" .py)
    cat <<EOF
[QAQC] Pipeline module '$MODULE' was modified.
- Run tests: python -m pytest tests/test_${MODULE}.py -v (if test file exists)
- Verify mock mode: changes must work with --mock flag
- Verify test count >= 117 after any test changes
EOF

    # Proactive hydro-reviewer trigger for scientifically sensitive modules
    case "$FILE_PATH" in
      *pipeline/hydrograph.py | *pipeline/streamstats.py | \
      *pipeline/model_builder.py | *pipeline/watershed.py)
        cat <<EOF
[HYDRO-REVIEWER] Hydro-sensitive module '$MODULE' changed.
- Launch hydro-reviewer agent to verify scientific correctness before committing
- Key checks: formula correctness, units, parameter bounds, [CALC] transparency logs
- If Manning's n, Tc, or peak flow logic changed -> HITL may be required
- See .claude/rules/human-in-the-loop.md for decision tree
EOF
        ;;
    esac
    ;;
esac

exit 0
