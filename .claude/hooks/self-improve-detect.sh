#!/bin/bash
# PostToolUse hook: Detect .claude/ config changes and suggest validation
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

# Only trigger for .claude/ config files (not outputs)
if echo "$FILE_PATH" | grep -q '\.claude/' && ! echo "$FILE_PATH" | grep -q '\.claude/outputs/'; then
    cat <<EOF
[SELF-IMPROVE] Configuration file modified: $(basename "$FILE_PATH")
- After completing current task, consider running /self-audit to validate consistency
- Check that rules, agents, and skills still align with codebase state
EOF
fi

exit 0
