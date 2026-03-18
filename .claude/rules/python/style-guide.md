---
description: Python style conventions for pipeline modules
globs: pipeline/**
---

# Python Style Guide

## Naming Conventions
- `snake_case` for functions and variables
- `PascalCase` for classes
- `UPPER_SNAKE` for module-level constants
- Descriptive names: `pour_point_lon` not `x`, `drainage_area_mi2` not `da`

## Docstrings
- Google-style docstrings on all public functions (Args, Returns, Raises)
- One-line summary + blank line + extended description when needed
- Include units in parameter descriptions: `drainage_area_mi2: float — drainage area in square miles`

## Logging
- All pipeline modules use stdlib `logging`
- `orchestrator.py`, `batch.py`, `notify.py` use `loguru` (do not mix within a module)
- Never use `print()` for operational output — use logging

## Error Handling
- Informative exception messages with context values
- Never silently swallow exceptions in pipeline stages
- Stages 1-2: raise OrchestratorError; Stages 3-7: append to errors list

## Imports
- Standard library first, then third-party, then local
- Lazy imports for heavy modules in api.py (import inside endpoint functions)
- Bare imports only: `import terrain` not `from pipeline import terrain`
