import asyncio
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from script import run_demo_script

app = FastAPI(title="Simulation Orchestrator")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    print("[orchestrator] browser connected")

    demo_task: asyncio.Task[None] | None = None

    try:
        while True:
            payload = await websocket.receive_json()
            print("[orchestrator:inbound]", payload)

            if payload.get("kind") != "simulation_event":
                continue

            event = payload.get("event", {})
            event_type = event.get("type")
            if event_type == "simulation_ready" and demo_task is None:
                start = event.get("suggestedSpawn") or {"lat": 0, "lng": 0, "alt": 100}
                demo_task = asyncio.create_task(run_demo_script(websocket, start))
    except WebSocketDisconnect:
        print("[orchestrator] browser disconnected")
    finally:
        if demo_task is not None:
            demo_task.cancel()
            try:
                await demo_task
            except asyncio.CancelledError:
                pass
