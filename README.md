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
| Delivery | Docker image with EnergyPlus and the server; GitHub Actions CI running tests and the regression case |

## Quick start

> Setup instructions will be filled in as the components land. The intended flow:

```bash
# Run everything in Docker (EnergyPlus included)
docker build -t bem_agent .
docker run -it -e GROQ_API_KEY=... bem_agent "Size HVAC for a 2-story office in Chicago"

# Or connect the MCP server to an MCP client such as Claude Desktop
python -m bem_agent.server
```

You'll need one of:

- a free **Groq API key**, to run the built-in agent loop, or
- an **MCP-capable app** (e.g. Claude Desktop), to call the tools directly.

## Tech stack

Python · EnergyPlus · MCP · Groq (OpenAI-compatible tool calling) · GCP Cloud Run · SQLite / SQL · pandas · Docker · GitHub Actions

## License

TBD
