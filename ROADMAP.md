# Roadmap: bem_agent MVP

This is the working plan for the MVP: what's in scope, the build order, and where we are right now. Update the **Current status** section and the checkboxes at the end of each work session.

## Current status

_Last updated: 2026-11-20_

- **Phase:** 5 done; next is Phase 6
- **Done:** Phases 0-5 merged; GCP set up (`projectbem`, `us-east1`, values in `deploy/gcp.env`); first live agent session on Groq from Cloud Shell (see README)
- **Next:** Phase 6 (Dockerfile, CI image build, deploy the MCP server to Cloud Run)
- **Where simulations run:**
  - **CI (GitHub Actions):** installs EnergyPlus 25.1.0 and runs simulation tests on every PR.
  - **Claude's sandbox:** can't download EnergyPlus, so Claude writes code and unit tests here and relies on CI for the simulation tests.
  - **GCP (Cloud Run):** the deployed demo. The local PC is not used.
- **LLM:** Groq free tier, default model `openai/gpt-oss-120b` (30 requests/min, 1,000 requests/day, 8K tokens/min, 200K tokens/day). `llama-3.3-70b-versatile` is the alternative to compare against in Phase 5.
- **Open questions:**
  - Groq API key (goes into Secret Manager, never into the repo or chat).

## Goal

An agent that takes a plain-language building energy question and answers it by driving EnergyPlus through tool calls: it builds the model, runs and checks the simulations, and compares the results. The MVP covers geometry, schedules, thermal loads, equipment sizing, and parametric runs, with validation around every simulation and the results stored in a queryable database.

## Scope

**In:** MCP connector, model-agnostic CLI agent loop (Groq by default), geometry, schedules, loads, equipment sizing, parametric runs, V&V, ETL into SQLite, SQL across runs, hourly time-series and plots, Docker, GitHub Actions CI/CD, Cloud Run deployment.

**Out (decided):** MongoDB / NoSQL run log, Flask API, React / Next.js frontend, Azure deployment.

## Safety guardrails (GCP and LLM)

Context: an AI tool once changed an unrelated GCP project. These rules keep `bem_agent` fully isolated.

1. **One dedicated GCP project**, `projectbem`, separate from all other work, with a budget alert. It holds Cloud Run, the GCS bucket, Artifact Registry and Secret Manager, and nothing else.
2. **LLM access by Groq API key only**, stored in Secret Manager in `projectbem`. The LLM provider has no access to GCP.
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
- [x] GCP project `projectbem` created; region `us-east1`
- [x] Setup script `deploy/gcp_setup.sh`: APIs, Artifact Registry, GCS bucket, Secret Manager (`GROQ_API_KEY`), runtime + deployer service accounts, Workload Identity Federation (this repo, `main` only)
- [x] Run `deploy/gcp_setup.sh` in Cloud Shell; identifiers saved in `deploy/gcp.env`
- [ ] Confirm a budget alert is set on `projectbem`

### Phase 1: Model builder
- [x] Parametric geometry: footprint (or total area + aspect ratio), floors, floor-to-floor height, window-to-wall ratio, orientation; perimeter/core zoning
- [x] Constructions and internal loads from presets (`efficient` / `typical` / `older`; `office` / `retail` / ...) or custom values
- [x] Schedules from operating hours: presets (`office`, `retail`, `24_7`) plus custom hours and setpoints
- [x] HVAC templates: ideal loads, VAV with chiller/boiler, PTHP (HVACTemplate objects written in full from IDD defaults)
- [x] Weather: 5 demo cities from EnergyPlus's bundled EPWs, or any `.epw` with its `.ddy`; design days read from the `.ddy`
- [x] Output: a valid IDF ready to run (reference checks on every model; EnergyPlus sizing runs pass in CI for all 3 systems)
- [x] CLI: `python -m bem_agent.model ... -o in.idf`

### Phase 2: Runner and V&V
- [x] Runner: EnergyPlus via shell-free subprocess with timeout; `eplusout.err` parsed into warnings, severe errors and messages
- [x] Pre-run validation (`validate_spec`): setpoint deadband and setback logic, geometry and load sanity, weather file, model references. Errors block the run; warnings don't
- [x] Post-run checks: clean completion, SQLite written, every zone sized, peak load density, EUI and unmet hours (annual runs)
- [x] `simulate()`: validate → build → run → check; one folder per run under `$BEM_RUNS_DIR` with `spec.json`, `model.idf`, outputs, `run.json`; compact summary for the LLM
- [x] Reference case and regression test (`tests/reference/office_chicago_ideal.json`, ±2%; regenerate with `scripts/update_reference.py`)
- Note: peak loads are zone design loads (what the rooms need), so they are the same for every HVAC system and exclude ventilation air. Installed equipment capacity (Phase 4) includes ventilation air and fan heat and is what differs between systems

### Phase 3: Results, ETL and analysis
- [x] Extract peak heating and cooling loads per zone and autosized equipment capacities (`ComponentSizes`)
- [x] ETL: each run's `eplusout.sql` → `results.db` tables `runs`, `zone_sizing`, `equipment_sizing`, `end_uses`, `hourly` (J→kWh, weather-file period only, warmup and design days dropped). `simulate()` loads every run automatically, failed and invalid ones included
- [x] SQL helpers: `compare_runs`, `equipment_sizes`, `end_uses`, and `run_sql` for the agent (read-only connection, SELECT only, one statement, row cap)
- [x] Hourly time series aggregated hourly / daily / monthly / annual (sum for energy, mean for temperatures; per zone or averaged across zones)
- [x] Plots: metric comparison bars and time-series lines (PNG), with a CSV table beside each
- Modeling fixes found by the post-run checks while testing: winter design day now has no internal gains (standard heating-load practice); ground-floor slab is insulated. Reference values regenerated for both

### Phase 4: Parametric runs
- [x] Case-matrix builder: base building + whitelisted parameters (HVAC, WWR, orientation, envelope/loads/schedule presets or single values, ...), every combination, up to 48 cases. Every case is validated before any simulation starts
- [x] Batch runner: cases run in parallel (threads around EnergyPlus subprocesses, each in its own folder); results loaded into SQLite from one thread; a failing case never stops the others
- [x] Comparison: `studies` / `study_cases` tables, `study_table`, study summary with best case per metric and main effects (mean of each metric per parameter value), `plot_study` sorted bar chart
- [x] Installed cooling / heating capacity per run from autosized equipment (chiller or cooling coils; boiler or heating coils), since zone loads alone can't tell HVAC systems apart
- Bug found by the parallel CI study: EnergyPlus's ExpandObjects writes `in.idf` to the working directory, so parallel runs collided. Each run now executes inside its own folder

### Phase 5: MCP server and agent
- [x] Provider-agnostic LLM layer (`bem_agent.llm`): Groq default; Claude, Gemini, OpenAI, any OpenAI-compatible server by config; retry with backoff on HTTP 429
- [x] Tool registry (`bem_agent.tools`), shared by the MCP server and the agent: `list_options`, `run_simulation`, `run_parametric_study`, `get_run_details`, `compare_runs`, `get_timeseries`, `query_results`, `make_chart`, `list_history`
- [x] Tool schemas kept to ~1.5K tokens; results are compact JSON capped at 4K characters; errors go back to the model as `{"error": ...}`
- [x] Guardrails in code: validation before any run, bundled locations only (no model-chosen file paths), read-only SQL, per-session simulation budget (invalid runs refunded)
- [x] MCP server (`bem-mcp`): stdio or streamable HTTP; charts returned as images; simulations run off the event loop
- [x] Agent loop + CLI (`bem-agent`): token-budget history trimming (shorten old results, then drop oldest exchanges), step limit with a forced final answer, interactive follow-ups, transcripts saved
- [x] Tests: scripted-LLM agent tests, in-process MCP client tests; in CI the agent runs a real study and the MCP server a real simulation
- [x] `scripts/install_energyplus.sh` (used by CI; works in Cloud Shell)
- [x] First live session on Groq (Cloud Shell); example recorded in the README. Answer quality follow-up: the system prompt now asks for % differences and the sizing basis of each system
- [ ] Optional: connect the Cloud Run MCP server to claude.ai as a custom connector for the live demo

### Phase 6: Packaging and CI/CD
- [ ] Dockerfile: EnergyPlus + `bem_agent`
- [ ] CI: build the Docker image and run the regression case inside it
- [ ] CD: on merge to `main`, push the image to GCP Artifact Registry and deploy to Cloud Run
- [ ] Keep `results.db` off the GCS mount: SQLite needs file locking, which Cloud Storage FUSE doesn't provide. Write the db to local disk and copy it to the bucket after each run (run folders can stay on the mount)
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
| 2026-11-20 | Write IDF directly (no eppy, which needs the IDD at runtime). HVAC via HVACTemplate + `--expandobjects`; template fields generated from the 25.1 IDD because ExpandObjects ignores defaults. |
| 2026-11-20 | Simplified geometry: rectangular perimeter/core zones, adiabatic partitions and interior floors. Fine for load and sizing comparisons. |
| 2026-11-20 | GCP project `projectbem`, region `us-east1`. Setup is a reviewed script you run in Cloud Shell; Claude has no GCP access. |
| 2026-11-20 | Ideal-loads models get explicit `Sizing:Zone` objects (the template creates none). Caught by the post-run `ZONES_NOT_SIZED` check. |
| 2026-11-20 | Results live in one SQLite `results.db`; the agent gets read-only SQL over it. Charts are static PNG + CSV so any MCP client can show them. |
| 2026-11-20 | Winter design day without internal gains; insulated slab. Both changed the regression reference on purpose. |
| 2026-11-20 | Parametric studies vary only whitelisted parameters (max 48 cases) and validate every case up front. Parallel runs via threads; SQLite written from one thread. |
| 2026-11-20 | Compare HVAC systems on installed capacity and annual energy, not zone peak loads (which are system-independent). |
| 2026-11-21 | One tool registry for both MCP and the agent loop. Tools accept only bundled locations, so the model never chooses file paths. |
| 2026-11-21 | Groq key in Secret Manager had a terminal escape code from the hidden prompt (bare HTTP 400). Keys are now cleaned in code and in `gcp_setup.sh`; secret version 2 is the clean key. |
