---
name: run-tests
description: Run the pytest test suite with optional arguments
user_invocable: true
disable_model_invocation: true
context: fork
agent: test-engineer
---

# /run-tests

Run the RAS Agent test suite.

## Usage

```
/run-tests              # run all tests
/run-tests -k terrain   # run tests matching "terrain"
/run-tests --tb=short   # short tracebacks
```

## Steps

1. Run: `python -m pytest tests/ -v $ARGUMENTS`
2. Report: total tests, passed, failed, errors
3. If any tests fail, analyze the failure and suggest fixes
4. Verify test count >= 112

## Arguments

Pass any valid pytest arguments. Common options:
- `-k EXPRESSION` — run tests matching expression
- `--tb=short` — shorter tracebacks
- `-x` — stop on first failure
- `tests/test_runner.py` — specific test file
