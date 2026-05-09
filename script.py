import asyncio
import math
from typing import Any

from fastapi import WebSocket


def move_east_meters(position: dict[str, Any], meters: float) -> dict[str, Any]:
    lat = float(position["lat"])
    lng = float(position["lng"])
    alt = float(position["alt"])
    lat_rad = math.radians(lat)
    delta_lng = meters / (111_111 * math.cos(lat_rad))
    return {
        "lat": lat,
        "lng": lng + delta_lng,
        "alt": alt,
    }


async def send_command(websocket: WebSocket, command: dict[str, Any]) -> None:
    payload = {
        "kind": "simulation_command",
        "command": command,
    }
    print("[orchestrator:outbound]", payload)
    await websocket.send_json(payload)


async def run_demo_script(websocket: WebSocket, start: dict[str, Any]) -> None:
    await send_command(
        websocket,
        {
            "type": "spawn_agent",
            "modality": "quadcopter",
            "start": start,
        },
    )

    await asyncio.sleep(10)
    await send_command(
        websocket,
        {
            "type": "set_waypoints",
            "agentId": "quadcopter-1",
            "waypoints": [move_east_meters(start, 500)],
        },
    )

    await asyncio.sleep(10)
    await send_command(
        websocket,
        {
            "type": "set_sensor_orientation",
            "agentId": "quadcopter-1",
            "sensorId": "quadcopter-camera-1",
            "panDeg": 0,
            "tiltDeg": -45,
        },
    )

