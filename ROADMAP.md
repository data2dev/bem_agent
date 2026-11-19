# Roadmap: bem_agent MVP

This is the working plan for the MVP: what's in scope, the build order, and where we are right now. Update the **Current status** section and the checkboxes at the end of each work session.

## Current status

_Last updated: 2026-11-18_

- **Phase:** 0, project setup
- **Done:** README, roadmap, minimal package skeleton, CI pipeline (GitHub Actions) on branch `feat/scaffold-docs`
- **Next:** create the dedicated GCP project and run the setup commands; then start Phase 1 (model builder)
- **Where simulations run:**
  - **CI (GitHub Actions):** installs EnergyPlus 25.1.0 and runs simulation tests on every PR.
  - **Claude's sandbox:** can't download EnergyPlus, so Claude writes code and unit tests here and relies on CI for the simulation tests.
  - **GCP (Cloud Run):** the deployed demo. Your local PC is not used, even though Docker works on it.
- **Open questions:**
  - Project IDs for `bem-agent-demo` (billing on) and `bem-agent-llm` (billing off), and a region.
  - Gemini API key, created in `bem-agent-llm` (goes into Secret Manager, never into the repo or chat).

## Why this project

A portfolio demo for an LLM-agents + building energy modeling role. It covers each responsibility in that kind of job description: agentic tool-calling workflows over simulation tools (geometry, thermal loads, equipment sizing, schedules, parametric runs), ETL and preprocessing pipelines, verification and validation tools, and generative AI through APIs.

## Scope

**In:** MCP connector, model-agnostic CLI agent loop (Gemini by default), geometry, schedules, loads, equipment sizing, parametric runs, V&V, ETL into SQLite, SQL across runs, hourly time-series and plots, Docker, GitHub repo with CI.

**Out (decided):** MongoDB / NoSQL run log, Flask API, React / Next.js frontend, Azure deployment.

**Naming:** no "Trane" or "TRACE" anywhere in the repo. Both are trademarks; the repo should not read as company work.

## Safety guardrails (GCP and LLM)

Context: an AI tool once changed an unrelated GCP project. These rules keep `bem_agent` fully isolated.

1. **Two dedicated GCP projects**, separate from all other work:
   - `bem-agent-demo` (billing on, budget alert): Cloud Run, GCS bucket, Artifact Registry, Secret Manager.
   - `bem-agent-llm` (**billing off**): holds only the Gemini API key. Keeping billing off keeps it on the AI Studio free tier (projects with billing are charged per token), and it has no resources to change.
2. **LLM access by API key only** (Gemini Developer API, not Vertex AI), restricted to the Gemini API and stored in Secret Manager in `bem-agent-demo`. No AI Studio app or agent features touch GCP.
3. **The agent can call only our simulation tools.** No shell, no `gcloud`, no arbitrary file access; all file paths confined to the runs directory and validated.
4. **Least-privilege service accounts:** the Cloud Run runtime account gets access to its own bucket only; the GitHub deploy account (Workload Identity Federation, no key files, limited to this repo) can only push images and update this service.
5. **Budget alert** on the project.
6. **Every `gcloud` command names `--project` explicitly**; the user reviews and runs them; Claude has no GCP access.

## Workflow

- Claude works on feature branches (`feat/...`) and pushes them; you review and merge into `main`.
- One branch per phase (or per sizable piece of a phase) keeps reviews small.
- Each phase ends with passing tests and an updated **Current status** above.

## Phases

### Phase 0: Setup
- [x] Create repo
- [x] README and ROADMAP
- [x] Pin EnergyPlus version (25.1.0) and install it in CI
- [x] Python package skeleton (`src/bem_agent/`), `pyproject.toml`, ruff, pytest
- [x] CI: GitHub Actions running lint, unit tests, and an EnergyPlus smoke test + simulation tests
- [x] `.gitignore`
- [ ] `.env.example` (for `ANTHROPIC_API_KEY`), license
- [x] Check Docker on your PC (works, but we won't use the local PC)
- [ ] Create `bem-agent-llm` (no billing) + Gemini key; create `bem-agent-demo` with budget alert, Artifact Registry, GCS bucket, Secret Manager, Workload Identity Federation for GitHub

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
- [ ] Model-agnostic CLI agent loop with tool calling (Gemini default; Groq and Claude supported)
- [ ] Example prompts and a recorded end-to-end transcript for the README

### Phase 6: Packaging and CI
- [ ] Dockerfile: EnergyPlus + `bem_agent`
- [ ] CI: build the Docker image and run the regression case inside it
- [ ] CD: on merge to `main`, push the image to GCP Artifact Registry and deploy to Cloud Run
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
| 2026-11-18 | EnergyPlus 25.1.0, installed in CI from the official release. CI is the main place real simulations run. |
| 2026-11-18 | No local PC: everything runs on GCP (Cloud Run service for the MCP server, Cloud Run jobs for batches, GCS for outputs). |
| 2026-11-18 | LLM: model-agnostic agent loop. Default Gemini Flash (AI Studio free tier), Groq as fallback, Claude optional (API or claude.ai connector). |
| 2026-11-18 | Safety guardrails adopted (see section above): dedicated GCP project, restricted API key, tool-only agent, least-privilege accounts. |
| 2026-11-18 | Split into two projects: the Gemini key lives in a billing-free project so it stays on the free tier (billing-enabled projects and Vertex AI are paid). |
