#!/bin/bash
# PreToolUse hook: Remind about CI checks before git commit
# Receives JSON on stdin with tool_input.command

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('tool_input', {}).get('command', ''))
except: print('')
" 2>/dev/null)

# Only trigger for git commit commands (not git status, git diff, etc.)
if echo "$COMMAND" | grep -qP 'git\s+commit'; then
    # Check if tests were run recently by looking for pytest cache
    if [ ! -d ".pytest_cache" ] || [ "$(find .pytest_cache -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
        echo "[PRE-COMMIT] Tests may not have been run recently. Consider running /check-ci first."
    fi
fi

exit 0
