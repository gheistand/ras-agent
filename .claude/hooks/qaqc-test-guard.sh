#!/bin/bash
# PostToolUse hook: Guard test count after pytest runs
# Receives JSON on stdin with tool_input.command and tool_response

INPUT=$(cat)

# Check if the command was a pytest run
COMMAND=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('tool_input', {}).get('command', ''))
except: print('')
" 2>/dev/null)

if ! echo "$COMMAND" | grep -q 'pytest'; then
    exit 0
fi

# Extract test count from the tool response
RESPONSE=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    resp = data.get('tool_response', '')
    if isinstance(resp, dict):
        resp = resp.get('stdout', resp.get('output', ''))
    print(str(resp))
except: print('')
" 2>/dev/null)

# Look for pytest summary line: "XX passed"
PASSED=$(echo "$RESPONSE" | grep -oP '\d+(?= passed)' | tail -1)

if [ -n "$PASSED" ] && [ "$PASSED" -lt 112 ] 2>/dev/null; then
    cat <<EOF
[QAQC WARNING] Test count dropped to ${PASSED} — baseline is 112.
- Investigate which tests were removed or broken
- Test count must never decrease below the baseline
- Run: python -m pytest tests/ -v --tb=short to see failures
EOF
fi

# Also check for failures
FAILED=$(echo "$RESPONSE" | grep -oP '\d+(?= failed)' | tail -1)
if [ -n "$FAILED" ] && [ "$FAILED" -gt 0 ] 2>/dev/null; then
    cat <<EOF
[QAQC] ${FAILED} test(s) failed. Fix before proceeding.
EOF
fi

exit 0
