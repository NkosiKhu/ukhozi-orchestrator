import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from logger import SimulationEventLogger
from scenarios import SCENARIOS, summarize_payload

app = FastAPI(title="Simulation Orchestrator")
ACTIVE_SCENARIO = "pyromaniac"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    print("[orchestrator] browser connected")

    scenario = SCENARIOS[ACTIVE_SCENARIO]()
    logger = SimulationEventLogger(ACTIVE_SCENARIO)
    await logger.start()

    try:
        while True:
            payload = await websocket.receive_json()
            print("[orchestrator:inbound]", summarize_payload(payload))

            if payload.get("kind") == "score_update":
                logger.handle_score_update(payload.get("update") or {})
                continue

            if payload.get("kind") != "simulation_event":
                continue

            event = payload.get("event", {})
            logger.handle_event(event)
            await scenario.handle_event(websocket, event)
    except WebSocketDisconnect:
        print("[orchestrator] browser disconnected")
    finally:
        await logger.close()
        await scenario.dispose()
