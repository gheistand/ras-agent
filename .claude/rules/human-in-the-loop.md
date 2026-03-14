---
description: First-class rule — when agents pause to ask the domain expert vs. proceed autonomously
globs:
---

# Human in the Loop (HITL) — First-Class Rule

HITL is a core architectural principle of RAS Agent. Agents must know when to pause and ask
the domain expert rather than making silent autonomous decisions on scientific or methodological
questions. This rule applies to all agents, all workflows, and all deployments.

## The Domain Expert

The authoritative domain expert for this project is the **operator** — the licensed engineer,
hydrologist, or floodplain manager responsible for the modeling results. On the reference
deployment (CHAMP / Illinois State Water Survey), this is **Glenn Heistand, PE, CFM**.

Domain expertise assumed:
- Hydrology and hydraulic (H&H) modeling
- FEMA floodplain management and NFIP
- HEC-RAS 2D modeling
- NRCS methods (NEH Part 630)
- USGS StreamStats and regional regression equations
- Illinois-specific watershed hydrology

**Defer to the domain expert on all scientific and methodological decisions.**

## HITL Configuration (`HITLConfig`)

HITL behavior is controlled by a `HITLConfig` object (defined in `pipeline/notify.py` pattern).
This keeps the repo portable (Apache 2.0 open source) — each deployment chooses its channel.

### Modes

| Mode | Behavior | Use Case |
|------|----------|----------|
| `blocking` | Pause pipeline, send question, wait for reply before proceeding | Interactive use, regulatory runs |
| `async` | Log question, proceed with best-guess default, flag output as "pending expert review" | Batch/unattended runs |
| `abort` | Stop run entirely when HITL trigger fires | Strict production/regulatory mode |

### Channels

| Channel | Description | Default? |
|---------|-------------|---------|
| `stdin` | Print to terminal, wait for keyboard reply | ✅ Yes (zero-config) |
| `file` | Write to queue file; user edits response file to resume | — |
| `webhook` | POST structured JSON question; receive response via callback | — |
| `email` | Send via SMTP (existing `notify.py` pattern); poll for reply | — |
| `telegram` | Route through operator's Telegram bot (reference deployment) | — |
| `api` | Expose via `/api/hitl/questions` FastAPI endpoint; respond in browser | — |

**Default (zero-config):** `mode=blocking`, `channel=stdin` — anyone who clones the repo
gets a working HITL loop with no external setup. Type your answer in the terminal.

**Reference deployment config** (Glenn's instance — env vars or `config/hitl.yaml`):
```yaml
hitl:
  mode: blocking
  channel: telegram
  telegram:
    bot_token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}
  timeout_sec: 300
```

The channel is **never baked into agent definitions**. Agents call `expert_liaison.ask()`
and the config determines how the question is routed.

## HITL Decision Tree

### Always PAUSE (regardless of mode — `abort` if async)

1. **Choosing scientific methods**
   - Which Tc formula applies to this watershed?
   - Which peak flow source is authoritative when API ≠ regression by >20%?
   - Which Manning's n for land cover types not in the lookup table?
   - Which boundary condition type (slope, discharge, rating curve)?

2. **Out-of-bounds results** (per `rules/scientific-validation.md`)
   - Tc < 0.25 hr or > 15 hr
   - Q100 unit peak flow outside 30–800 csm
   - Manning's n outside safety bounds for NLCD class
   - Max flood depth > 30 ft or < 0.1 ft anywhere in domain

3. **Conflicting data sources**
   - StreamStats API ≠ IL regression by > ±20%
   - DEM sources disagree at watershed boundary
   - Pour point location ambiguous (multiple D8 outlets within threshold)

4. **FEMA compliance decisions**
   - Any choice affecting regulatory acceptability
   - Depth accuracy target selection
   - Floodway vs. flood fringe treatment

5. **Deviating from established methodology**
   - Non-rural watershed (>10% impervious) — regression may not apply
   - Karst terrain, channelized drainage, or controlled outlet
   - First model in a new region without local calibration

### Proceed Autonomously

1. **Implementing a confirmed decision** — once the expert has chosen a method, implementation is mechanical
2. **Data acquisition and format conversion** — download, reproject, clip, mosaic
3. **Template-based model generation** — clone, update Manning's n from approved table, write BCs
4. **QAQC and validation** — run checks automatically, queue findings for expert review
5. **Bug fixes** that do not change scientific logic
6. **Infrastructure** — Docker, CI, web dashboard, deployment

## HITL Question Format

When an agent triggers a HITL question, it must use this format:

```
## HITL QUESTION — [topic] [BLOCKING | WARN | INFO]

**Context:** [What you're doing and why this decision matters]

**Current behavior / proposed default:**
  [What the code does now, or what default would be used in async mode]

**Options:**
  A. [Option A] — [tradeoff / implication]
  B. [Option B] — [tradeoff / implication]

**Recommendation:** Option [X] because [reason]

**Location:** `{file}:{function}:{line}` (if applicable)
**Waiting for:** Expert confirmation before proceeding (blocking mode)
          — OR —
**Flagged:** Proceeding with Option [X]. Please review before using outputs. (async mode)
```

## Per-Module HITL Triggers

### terrain.py
- **PAUSE:** DEM source selection when ILHMP has gaps and 3DEP quality differs
- **PAUSE:** NoData > 5% of watershed area
- **PROCEED:** Download, mosaic, reproject, clip

### watershed.py
- **PAUSE:** Pour point snap distance > 500m (which outlet is correct?)
- **PAUSE:** Multiple possible outlets within threshold distance
- **PAUSE:** Delineated drainage area differs > 20% from expected (if known)
- **PROCEED:** Pit filling, D8 flow direction, accumulation, polygon extraction

### streamstats.py
- **PAUSE:** API result ≠ IL regression by > ±20% (which to use?)
- **PAUSE:** Watershed characteristics outside regression validity range
- **PROCEED:** API query, parsing, fallback equation application

### hydrograph.py
- **PAUSE:** Tc < 0.25 hr or > 15 hr
- **PAUSE:** Peak rate factor selection (484 vs. 300 — confirm for this watershed)
- **PROCEED:** DUH ordinate application, hydrograph volume calculation

### model_builder.py
- **PAUSE:** Manning's n for NLCD class not in lookup table
- **PAUSE:** Template area differs > 25% from target watershed area
- **PAUSE:** Boundary condition slope outside 0.0005–0.005 ft/ft
- **PROCEED:** Template clone, Manning's n application from approved table, perimeter writing

### results.py
- **PAUSE:** Max depth > 30 ft or < 0.1 ft (model instability or dry domain)
- **PAUSE:** Flood extent > 40% of watershed area
- **PROCEED:** HDF5 extraction, raster export, polygon generation

## When in Doubt

If an agent is uncertain whether a decision requires HITL, **err toward asking**.
Surfacing uncertainty is a feature. Silent autonomy on a scientifically sensitive decision
is the failure mode this architecture exists to prevent.
