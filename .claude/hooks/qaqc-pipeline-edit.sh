#!/bin/bash
# PostToolUse hook: QAQC reminder after editing pipeline files
# Receives JSON on stdin with tool_input.file_path

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    fp = data.get('tool_input', {}).get('file_path', '')
    print(fp)
except: print('')
" 2>/dev/null)

# Only trigger for pipeline Python files
if echo "$FILE_PATH" | grep -q 'pipeline/.*\.py$'; then
    MODULE=$(basename "$FILE_PATH" .py)
    cat <<EOF
[QAQC] Pipeline module '${MODULE}' was modified.
- Run tests: python -m pytest tests/test_${MODULE}.py -v (if test file exists)
- If scientific logic changed (Manning's n, hydrology, equations), consider launching hydro-reviewer agent
- Verify mock mode still works if you changed data flow
EOF
fi

exit 0
