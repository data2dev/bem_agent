# Roadmap: bem_agent MVP

This is the working plan for the MVP: what's in scope, the build order, and where we are right now. Update the **Current status** section and the checkboxes at the end of each work session.

## Current status

_Last updated: 2026-11-19_

- **Phase:** 0, project setup
- **Done:** README, roadmap, minimal package skeleton, CI pipeline (GitHub Actions), all merged to `main`
- **Next:** create the GCP project and run the setup commands; start Phase 1 (model builder) in parallel
- **Where simulations run:**
  - **CI (GitHub Actions):** installs EnergyPlus 25.1.0 and runs simulation tests on every PR.
  - **Claude's sandbox:** can't download EnergyPlus, so Claude writes code and unit tests here and relies on CI for the simulation tests.
  - **GCP (Cloud Run):** the deployed demo. The local PC is not used.
- **LLM:** Groq free tier, default model `openai/gpt-oss-120b` (30 requests/min, 1,000 requests/day, 8K tokens/min, 200K tokens/day). `llama-3.3-70b-versatile` is the alternative to compare against in Phase 5.
- **Open questions:**
  - GCP project ID for `bem-agent-demo`, and a region.
  - Groq API key (goes into Secret Manager, never into the repo or chat).

## Goal

An agent that takes a plain-language building energy question and answers it by driving EnergyPlus through tool calls: it builds the model, runs and checks the simulations, and compares the results. The MVP covers geometry, schedules, thermal loads, equipment sizing, and parametric runs, with validation around every simulation and the results stored in a queryable database.

## Scope

**In:** MCP connector, model-agnostic CLI agent loop (Groq by default), geometry, schedules, loads, equipment sizing, parametric runs, V&V, ETL into SQLite, SQL across runs, hourly time-series and plots, Docker, GitHub Actions CI/CD, Cloud Run deployment.

**Out (decided):** MongoDB / NoSQL run log, Flask API, React / Next.js frontend, Azure deployment.

## Safety guardrails (GCP and LLM)

Context: an AI tool once changed an unrelated GCP project. These rules keep `bem_agent` fully isolated.

1. **One dedicated GCP project**, `bem-agent-demo`, separate from all other work, with a budget alert. It holds Cloud Run, the GCS bucket, Artifact Registry and Secret Manager, and nothing else.
2. **LLM access by Groq API key only**, stored in Secret Manager in `bem-agent-demo`. The LLM provider has no access to GCP.
3. **The agent can call only our simulation tools.** No shell, no `gcloud`, no arbitrary file access; all file paths confined to the runs directory and validated.
4. **Least-privilege service accounts:** the Cloud Run runtime account gets access to its own bucket and the Groq key secret only; the GitHub deploy account (Workload Identity Federation, no key files, limited to this repo) can only push images and update this service.
5. **Every `gcloud` command names `--project` explicitly**; you review and run them; Claude has no GCP access.

## Workflow

- Claude works on feature branches (`feat/...`, `docs/...`) and pushes them; you review and merge into `main`.
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
- [x] Check Docker on the local PC (works, but we won't use it)
- [x] `.env.example`
- [ ] License
- [ ] Create a Groq account and API key
- [ ] Create `bem-agent-demo` with budget alert, Artifact Registry, GCS bucket, Secret Manager (`GROQ_API_KEY`), Workload Identity Federation for GitHub

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
- [x] Provider-agnostic LLM layer (`bem_agent.llm`): Groq default; Claude, Gemini, OpenAI, any OpenAI-compatible server by config; retry with backoff on HTTP 429
- [ ] CLI agent loop with tool calling on top of `bem_agent.llm`
- [ ] Stay within Groq's free-tier limits: compact tool outputs (summaries and run IDs, not raw tables), history trimming
- [ ] Compare `gpt-oss-120b` and `llama-3.3-70b-versatile` on the same prompt set (task completion, malformed tool calls, tokens used) and pick the default
- [ ] Example prompts and a recorded end-to-end transcript for the README
- [ ] Optional: connect the Cloud Run MCP server to claude.ai as a custom connector

### Phase 6: Packaging and CI/CD
- [ ] Dockerfile: EnergyPlus + `bem_agent`
- [ ] CI: build the Docker image and run the regression case inside it
- [ ] CD: on merge to `main`, push the image to GCP Artifact Registry and deploy to Cloud Run
- [ ] README quick start verified from a clean clone

### Phase 7: Polish
- [ ] Architecture diagram and sample plots in the README
- [ ] Short walkthrough (GIF or transcript) of a full agent session
- [ ] Design notes: key decisions, V&V approach, future work

## Decisions log

| Date | Decision |
|---|---|
| 2026-11-18 | Repo name `bem_agent`. Out of scope: MongoDB, Flask, React / Next.js, Azure. |
| 2026-11-18 | Claude builds on feature branches; you merge to `main`. |
| 2026-11-18 | EnergyPlus 25.1.0, installed in CI from the official release. CI is the main place real simulations run. |
| 2026-11-18 | No local PC: everything runs on GCP (Cloud Run service for the MCP server, Cloud Run jobs for batches, GCS for outputs). |
| 2026-11-18 | Safety guardrails adopted (see section above): dedicated GCP project, tool-only agent, least-privilege accounts. |
| 2026-11-19 | LLM: Groq free tier, `openai/gpt-oss-120b` by default, behind a swappable provider interface. Replaces the earlier Gemini plan, so the separate billing-free Gemini project is dropped; one GCP project is enough. |
| 2026-11-20 | Keep Claude (and other providers) switchable: one `BEM_LLM_PROVIDER` / `BEM_LLM_MODEL` setting. Claude via Anthropic API key only, not Vertex, so the LLM never needs GCP credentials (guardrail 2). |
