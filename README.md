# Ukhozi Orchestrator

Bare minimum to run the `pyromaniac` scenario against the Kingston, Jamaica world.

Repo: https://github.com/NkosiKhu/ukhozi-orchestrator

## What this does

This FastAPI app accepts simulation events from the Ukhozi browser sim over WebSocket and sends control commands back. The current default scenario is `pyromaniac`, set in [main.py](/Users/nkosinathikhumalo/sota/orchestrator/main.py:7).

`pyromaniac` spawns two fixed-wing agents and assigns them to the closest fire hazards to the command center.

## Requirements

- Python 3.11+
- The Ukhozi frontend running separately with the Kingston, Jamaica roads/world loaded

Install Python dependencies:

```bash
pip install -r requirements.txt
```

## Run

Start the orchestrator:

```bash
uvicorn main:app --reload
```

Local sandbox URLs:

- HTTP health check: `http://127.0.0.1:8000/health`
- WebSocket endpoint: `ws://127.0.0.1:8000/ws`

## Connect It To Ukhozi

1. Start this orchestrator.
2. Start the Ukhozi frontend separately.
3. Make sure the frontend connects to `ws://127.0.0.1:8000/ws`.
4. Open the sim and let world generation finish.
5. On `simulation_ready`, the orchestrator will spawn the `pyromaniac` agents automatically.

## Notes

- Session logs are written to `sim_logs.sqlite3`.
- Scenario registration lives in [scenarios/__init__.py](/Users/nkosinathikhumalo/sota/orchestrator/scenarios/__init__.py:1).
- The active scenario switch is [main.py](/Users/nkosinathikhumalo/sota/orchestrator/main.py:7).
