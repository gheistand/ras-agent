# agent_tasks/ — Coordination System

Session-level state and task tracking for Claude Code agent work.

## Structure

```
agent_tasks/
├── README.md              ← this file (tracked)
├── .agent/
│   ├── CONSTITUTION.md    ← project principles (tracked)
│   ├── STATE.template.md  ← session state template (tracked)
│   ├── BACKLOG.template.md
│   ├── PROGRESS.template.md
│   └── LEARNINGS.template.md
├── tasks/                 ← active task files (gitignored)
├── plans/                 ← session plans (gitignored)
└── .old/                  ← archived tasks (gitignored)
```

## How It Works

### Session State (`.agent/`)
At the start of a work session, copy templates to create live state files:
- `STATE.md` — Current session context (what you're working on, blockers)
- `BACKLOG.md` — Known work items not yet started
- `PROGRESS.md` — Completed items this session
- `LEARNINGS.md` — Discoveries, gotchas, decisions made

These files are gitignored — they're ephemeral per-session state.

### Tasks (`tasks/`)
Individual task files for tracking multi-step work:
- One markdown file per task
- Include status, assigned agent, and acceptance criteria
- Move to `.old/` when complete

### Plans (`plans/`)
Session-level implementation plans created by Claude Code's plan mode.

## What's Tracked vs Gitignored

**Tracked (framework):** README.md, CONSTITUTION.md, template files, .gitkeep files
**Gitignored (state):** STATE.md, BACKLOG.md, PROGRESS.md, LEARNINGS.md, task files, plan files, .old/ contents
