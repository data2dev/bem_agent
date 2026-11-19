# Roadmap: bem_agent MVP

This is the working plan for the MVP: what's in scope, the build order, and where we are right now. Update the **Current status** section and the checkboxes at the end of each work session.

## Current status

_Last updated: 2026-11-18_

- **Phase:** 0, project setup
- **Done:** repo created, README and roadmap drafted (branch `feat/scaffold-docs`)
- **Next:** confirm how EnergyPlus gets installed for local dev and CI, then scaffold the Python package
- **Blockers / open questions:**
  - EnergyPlus binaries can't be downloaded inside Claude's sandbox (GitHub release downloads are blocked there). Options: install it through Docker / CI only, or find a pip-installable build. To decide in Phase 0.
  - Running the agent loop end to end needs an Anthropic API key (yours, set locally; never committed).

## Why this project

A portfolio demo for an LLM-agents + building energy modeling role. It covers each responsibility in that kind of job description: agentic tool-calling workflows over simulation tools (geometry, thermal loads, equipment sizing, schedules, parametric runs), ETL and preprocessing pipelines, verification and validation tools, and generative AI through APIs.

## Scope

**In:** MCP connector, CLI agent loop (Anthropic API), geometry, schedules, loads, equipment sizing, parametric runs, V&V, ETL into SQLite, SQL across runs, hourly time-series and plots, Docker, GitHub repo with CI.

**Out (decided):** MongoDB / NoSQL run log, Flask API, React / Next.js frontend, Azure / GCP deployment.

**Naming:** no "Trane" or "TRACE" anywhere in the repo. Both are trademarks; the repo should not read as company work.

## Workflow

- Claude works on feature branches (`feat/...`) and pushes them; you review and merge into `main`.
- One branch per phase (or per sizable piece of a phase) keeps reviews small.
- Each phase ends with passing tests and an updated **Current status** above.

## Phases

### Phase 0: Setup
- [x] Create repo
- [x] README and ROADMAP
- [ ] Decide the EnergyPlus install path (Docker image vs. pip build) and pin a version
- [ ] Python package skeleton (`src/bem_agent/`), `pyproject.toml`, linting, pytest
- [ ] `.gitignore`, `.env.example` (for `ANTHROPIC_API_KEY`), license

### Phase 1: Model builder
- [ ] Parametric geometry: footprint, floors, floor-to-floor height, window-to-wall ratio, orientation
- [ ] Constructions and internal loads from simple presets
- [ ] Schedules: occupancy, lighting, HVAC operation (presets plus custom)
- [ ] HVAC templates: 2–3 systems (e.g. ideal loads, VAV, heat pump)
- [ ] Weather file handling (a few bundled EPWs for demo cities)
- [ ] Output: a valid IDF ready to run

### Phase 2: Runner and V&V
- [ ] Single-run wrapper around EnergyPlus with SQLite output enabled
- [ ] Pre-run validation: geometry bounds, WWR range, schedule completeness, missing fields
- [ ] Post-run checks: severe errors in `.err`, unmet hours, implausible EUI or loads per area
- [ ] Reference case and regression test (stored expected loads, with tolerance)

### Phase 3: Results, ETL and analysis
- [ ] Extract peak heating and cooling loads and autosized equipment capacities
- [ ] ETL: EnergyPlus SQLite → cleaned tables in `results.db` (runs, loads, sizing, hourly)
- [ ] SQL query helpers to compare runs
- [ ] Hourly time-series aggregation (daily / monthly) and plots

### Phase 4: Parametric runs
- [ ] Case-matrix builder (e.g. systems × WWR × orientation)
- [ ] Batch runner (parallel where possible), each case validated
- [ ] Comparison table and summary across cases

### Phase 5: MCP server and agent
- [ ] MCP server exposing tools: `create_building`, `set_schedules`, `set_hvac_system`, `run_simulation`, `run_parametric`, `get_loads`, `get_equipment_sizes`, `query_results`, `plot_timeseries`
- [ ] Clear tool descriptions and error messages the LLM can recover from
- [ ] CLI agent loop using the Anthropic API with tool calling
- [ ] Example prompts and a recorded end-to-end transcript for the README

### Phase 6: Packaging and CI
- [ ] Dockerfile: EnergyPlus + `bem_agent`
- [ ] GitHub Actions: lint, unit tests, regression case inside the Docker image
- [ ] README quick start verified from a clean clone

### Phase 7: Polish for the demo
- [ ] Architecture diagram and sample plots in the README
- [ ] Short walkthrough (GIF or transcript) of a full agent session
- [ ] Talking points: design decisions, V&V approach, what you'd add next

## Decisions log

| Date | Decision |
|---|---|
| 2026-11-18 | Repo name `bem_agent`. Scope = all core items; stretch items (Mongo, Flask) and skips (React, cloud deploy) are out. |
| 2026-11-18 | Claude builds on feature branches; you merge to `main`. |
