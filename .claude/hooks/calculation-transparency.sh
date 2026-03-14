#!/bin/bash
# PostToolUse hook: Remind about [CALC] transparency logging after editing hydro modules
# Triggered on Edit/Write to pipeline hydro-sensitive files

FILE_PATH="${CLAUDE_TOOL_INPUT_FILE_PATH:-}"

# Only trigger for hydro-sensitive pipeline modules
case "$FILE_PATH" in
  *pipeline/hydrograph.py | *pipeline/streamstats.py | \
  *pipeline/model_builder.py | *pipeline/watershed.py)
    MODULE=$(basename "$FILE_PATH" .py)
    cat <<EOF
[TRANSPARENCY] $MODULE was modified.
Verify all scientific calculations include [CALC] log entries:
  - Inputs (values + sources + units)
  - Method (formula name + reference)
  - Output (result + units)
  - Validity (pass/warn/fail vs. rules/scientific-validation.md bounds)

Key calculations requiring [CALC] logs in this module:
EOF
    case "$MODULE" in
      hydrograph)    echo "  Tc (Kirpich), Tp, Qp, hydrograph volume" ;;
      streamstats)   echo "  Q100 (source, csm, CI), API vs. regression diff" ;;
      model_builder) echo "  Manning's n (by NLCD class, composite), cell size selection" ;;
      watershed)     echo "  Pour point snap distance, drainage area, relief, channel length" ;;
    esac
    echo "See .claude/rules/transparency.md for format."
    ;;
esac

exit 0
