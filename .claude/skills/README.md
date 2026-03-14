# Skills

User-invocable slash commands for the RAS Agent project.

## Skill Inventory

| Command | Description | Modifies Code? |
|---------|-------------|---------------|
| `/run-tests` | Run pytest suite | No |
| `/check-ci` | Local CI verification (pytest + lint + build) | No |
| `/run-pipeline` | Mock pipeline execution | No |
| `/pipeline-status` | Module overview with stats | No |
| `/add-module [name]` | Scaffold new module + test | Yes |
| `/module-map` | Pipeline data flow diagram | No |
| `/improve` | Recursive self-improvement (audit → fix → validate) | Yes (`.claude/` only) |
| `/self-audit` | Quick config consistency check | No |

## Naming Convention

Skills follow the `ras_verb_modifier` pattern:
- `ras_run_tests` → `/run-tests`
- `ras_check_ci` → `/check-ci`
- `ras_scaffold_module` → `/add-module`
- `ras_self_audit` → `/self-audit`

The directory name uses underscores; the slash command uses hyphens.

## SKILL.md Format

```yaml
---
name: command-name           # slash command name
description: One-line description
user_invocable: true         # can be called with /command-name
disable_model_invocation: true  # optional — prevents auto-invocation
context: fork                # optional — runs in forked context
agent: agent-name            # optional — delegates to specific agent
allowed_tools: Read, Grep    # optional — restrict available tools
---
```

## Adding New Skills

1. Create `skills/ras_verb_modifier/SKILL.md`
2. Follow the frontmatter format above
3. Add step-by-step instructions in the markdown body
4. Update the inventory table in this README
5. Run `/improve` to verify configuration consistency
