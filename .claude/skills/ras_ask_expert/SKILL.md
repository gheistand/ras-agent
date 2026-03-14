---
name: ask-expert
description: Formalize and queue a domain question for the expert via HITLConfig channel
user_invocable: true
agent: expert-liaison
---

# /ask-expert

Formalize a domain question and route it to the operator/domain expert using the
configured HITLConfig channel (default: blocking + stdin).

## Usage

```
/ask-expert "Should we use peak rate factor 484 or 300 for this watershed?"
/ask-expert "StreamStats returned 9,384 CFS but IL regression gave 11,200 CFS — which do we use?"
/ask-expert --urgency blocking "Manning's n for channelized agricultural ditch not in NLCD table"
```

## Steps

1. Read `.claude/rules/human-in-the-loop.md` and `.claude/rules/scientific-validation.md`
2. Check whether the answer already exists in:
   - `pipeline/CLAUDE.md` (domain knowledge)
   - `.claude/outputs/expert-liaison/questions-queue.md` (previously answered)
   - `agent_tasks/.agent/CONSTITUTION.md` (project decisions)
   If found → surface the existing answer instead of re-asking
3. Format the HITL question using the standard block (from `human-in-the-loop.md`):
   - Context, current behavior/default, options, recommendation, code location
4. Assign urgency: `blocking` | `high` | `info` (default: `blocking`)
5. Append to `.claude/outputs/expert-liaison/questions-queue.md`
6. Route via configured HITLConfig channel:
   - `stdin`: print to terminal, wait for reply
   - `telegram`: send Telegram message (reference deployment)
   - `file` / `webhook` / `email` / `api`: per HITLConfig config
7. Return: question ID + status + channel used

## Rules

- **Always include a recommendation** — the expert confirms, not decides from scratch
- **One question per topic** — don't bundle unrelated decisions
- **Include numeric context** — actual values, bounds, percent differences
- **Never ask what's already answered** — check queue and docs first

## Example Output

```
HITL QUESTION — Peak Flow Source Selection [BLOCKING]

Context: StreamStats API (9,384 CFS) and IL regression (11,200 CFS) differ by 19.4%
         for 95.3 mi² central IL watershed. Discrepancy exceeds ±20% threshold.

Current default (async mode): Use StreamStats API result.

Options:
  A. Use StreamStats API (9,384 CFS) — watershed-specific delineation, 95% CI documented
  B. Use IL regression (11,200 CFS) — regional equation, higher uncertainty
  C. Average both — unconventional but conservative

Recommendation: Option A — StreamStats is the preferred source; regression is the fallback.
The 19.4% difference is near the boundary; worth confirming given regulatory use.

Location: streamstats.py:get_peak_flows:line 142
Waiting for: Expert confirmation
```
