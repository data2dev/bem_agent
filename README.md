# bem_agent

An LLM agent that drives building energy modeling (BEM) simulations through tool calls.

`bem_agent` exposes EnergyPlus as a set of tools over the [Model Context Protocol](https://modelcontextprotocol.io) (MCP). An LLM can then do the multi-step work an energy modeler does by hand: define a building, set schedules, run a sizing simulation, read back thermal loads and equipment capacities, sweep design options, and compare the results.

> **Status:** early development. See [ROADMAP.md](ROADMAP.md) for what's built and what's next.

## What it does

Ask the agent something like:

> "For a 3-story, 30,000 ft² office in Charlotte, compare a VAV system with a heat pump system at 30% and 50% window-to-wall ratio. Which has the lowest peak cooling load and annual energy use?"

The agent breaks that into tool calls:

1. **Geometry**: builds a parametric box model (footprint, floors, window-to-wall ratio, orientation).
2. **Schedules**: applies occupancy, lighting, and HVAC operating schedules.
3. **Loads and sizing**: runs an EnergyPlus sizing simulation and reports peak heating and cooling loads.
4. **Equipment sizing**: reads autosized equipment capacities back from the results.
5. **Parametric runs**: builds a case matrix (here 2 systems × 2 window ratios) and runs it in batch.
6. **Comparison**: queries the results database and summarizes the trade-offs.

Every run passes through validation before and after the simulation, so the agent can't silently report results from a broken model.

## Architecture

```
LLM (Groq gpt-oss-120b by default, or any MCP client)
        │  tool calls
        ▼
MCP server (bem_agent)
  ├── model builder     geometry, constructions, schedules, HVAC templates → IDF
  ├── validation        input checks before a run, sanity checks after
  ├── runner            EnergyPlus runs, single or parametric batch
  └── ETL               EnergyPlus SQLite output → cleaned results database
        │
        ▼
EnergyPlus  +  results.db (SQLite)
```

## Features

| Area | What's included |
|---|---|
| Agent and connector | MCP server exposing simulation tools; a model-agnostic CLI agent loop (Groq by default) with tool calling |
| Simulation | Geometry, schedules, thermal loads, autosized equipment, parametric batches |
| Verification and validation | Pre-run input validation, post-run sanity checks, a reference-case regression test |
| Data | ETL from EnergyPlus SQLite output into a results database; SQL queries across runs; hourly time-series aggregation and plots |
| Delivery | Docker image with EnergyPlus and the server; CI runs tests, the regression case and an HTTP smoke test of the image; merges to `main` deploy to Cloud Run |

## Quick start

Works anywhere with Linux x86_64 and Python 3.11+. Google Cloud Shell is a free option
that needs nothing on your own machine.

```bash
git clone https://github.com/data2dev/bem_agent && cd bem_agent
bash scripts/install_energyplus.sh ~/energyplus        # EnergyPlus 25.1, ~2 minutes
export ENERGYPLUS_DIR=~/energyplus PATH=~/energyplus:$PATH
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
export GROQ_API_KEY=...                                  # free key from console.groq.com
```

### Ask the agent

```bash
bem-agent "Compare VAV and packaged heat pumps for a 3-story, 30,000 ft2 office in \
Washington DC at 30% and 50% window-to-wall ratio. Which needs less cooling capacity?"
bem-agent                       # interactive; follow-up questions keep the context
```

Each step is printed as it happens, and the full transcript is saved under `runs/transcripts/`.

Example session (Groq `openai/gpt-oss-120b`, free tier, run in Cloud Shell):

```text
$ bem-agent "Compare VAV and packaged heat pumps for a 3-story, 30,000 ft2 office in
  Washington DC at 30% and 50% window-to-wall ratio. Which needs less cooling capacity?"
[groq / openai/gpt-oss-120b]  runs: runs
  → run_parametric_study({"base":{"floor_area_m2":2790,"location":"washington_dc",
      "num_floors":3,...},"parameters":{...}})
    ✓ study_id=study_20261121T055240_904ae8

| WWR  | HVAC                 | Cooling capacity | Run ID                             |
|------|----------------------|------------------|------------------------------------|
| 30 % | VAV (chiller-boiler) | 165.5 kW (47.1 t)| 20261121T055240_wwr-30-vav_583519  |
| 30 % | Packaged heat pump   | 158.8 kW (45.2 t)| 20261121T055240_wwr-30-pthp_6f8a71 |
| 50 % | VAV (chiller-boiler) | 190.9 kW (54.3 t)| 20261121T055240_wwr-50-vav_d6e2c5  |
| 50 % | Packaged heat pump   | 185.2 kW (52.7 t)| 20261121T055240_wwr-50-pthp_b0b59f |

For both window-to-wall ratios the packaged heat-pump system requires the smaller
cooling capacity ...

[2 LLM calls, 1 tool calls, 4,489 in / 1,157 out tokens]
```

The model converted ft² to m² itself, chose one parametric study over four single runs,
and quoted only numbers from the tool result. (Answer abbreviated; the full session is
four EnergyPlus runs in parallel, about a minute.)

### Or use the tools from any MCP client

```bash
bem-mcp                          # stdio, e.g. Claude Desktop
bem-mcp --http --port 8080       # streamable HTTP at http://localhost:8080/mcp
```

Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "bem_agent": {
      "command": "/path/to/bem_agent/.venv/bin/bem-mcp",
      "env": { "ENERGYPLUS_DIR": "/path/to/energyplus", "BEM_RUNS_DIR": "/path/to/runs" }
    }
  }
}
```

### Tools

| Tool | What it does |
|---|---|
| `list_options` | Locations, HVAC systems, presets, study parameters, units |
| `run_simulation` | Validate, build and run one model; peak loads, installed capacity, EUI |
| `run_parametric_study` | Every combination of options in parallel; best case and main effects |
| `get_run_details` | Issues, zone loads, equipment sizes or end uses for one run |
| `compare_runs` | Several runs side by side |
| `get_timeseries` | Daily / monthly energy and temperatures from annual runs |
| `query_results` | Read-only SQL over the results database |
| `make_chart` | PNG chart + CSV (returned as an image over MCP) |
| `list_history` | Earlier runs and studies |

Guardrails are enforced in code, not left to the model: inputs are validated before
anything runs, locations are limited to bundled weather files, SQL is read-only, and
each session has a simulation budget (`BEM_MAX_SIMULATIONS`, default 60).

## Deployment (Cloud Run)

The MCP server runs on Google Cloud Run, built from the [`Dockerfile`](Dockerfile)
(EnergyPlus included). Every merge to `main` builds the image, pushes it to Artifact
Registry and deploys it ([`deploy.yml`](.github/workflows/deploy.yml)). GitHub signs in
through Workload Identity Federation, so no cloud keys are stored anywhere.

- **API keys:** every request needs a key (`Authorization: Bearer <key>` or
  `X-API-Key: <key>`), one per person or client, kept in Secret Manager. Requests without
  one get `401` before reaching any tool. Each key has its own simulation budget, and
  every tool call is logged with the key's name. Manage keys with
  [`deploy/mcp.sh`](deploy/mcp.sh) (`add`, `remove`, `list`).
- **Storage:** simulations run on the instance's local disk; finished runs, charts and
  snapshots of `results.db` are copied to a Cloud Storage bucket, and the database is
  restored from there when a new instance starts.
- **Cost guard:** at most one instance, scales to zero when idle, CPU billed only during
  requests.

Check a deployment end to end (auth, tools, one simulation):

```bash
BEM_MCP_KEY=<your key> python scripts/smoke_mcp.py https://<service>.run.app
```

### Connect Grok

At [grok.com/connectors](https://grok.com/connectors): **New Connector → Custom**, then

- URL: `https://<service>.run.app/mcp` (`bash deploy/mcp.sh url` prints it)
- Header: `Authorization` = `Bearer <your grok-connector key>`

Grok then calls the same tools directly; no other LLM is involved.

## Choosing the LLM

The agent is model-agnostic. One setting picks the provider; agent code never changes.

| `BEM_LLM_PROVIDER` | Default model | API key variable |
|---|---|---|
| `groq` (default, free tier) | `openai/gpt-oss-120b` | `GROQ_API_KEY` |
| `anthropic` | `claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
| `gemini` | `gemini-3.5-flash` | `GEMINI_API_KEY` |
| `openai` | set `BEM_LLM_MODEL` | `OPENAI_API_KEY` |
| `openai_compatible` (OpenRouter, vLLM, Ollama, ...) | set `BEM_LLM_MODEL` | `BEM_LLM_API_KEY` + `BEM_LLM_BASE_URL` |

Override the model with `BEM_LLM_MODEL`, e.g. `BEM_LLM_PROVIDER=anthropic BEM_LLM_MODEL=claude-opus-5-5`. See [`.env.example`](.env.example).

## Tech stack

Python · EnergyPlus · MCP · Groq (OpenAI-compatible tool calling) · GCP Cloud Run · SQLite / SQL · pandas · Docker · GitHub Actions

## License

TBD
