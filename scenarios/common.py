import math
from typing import Any

from fastapi import WebSocket


def summarize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("kind") == "score_update" and isinstance(value.get("update"), dict):
            update = value["update"]
            event = update.get("event") or {}
            return {
                "kind": "score_update",
                "update": {
                    "reason": event.get("reason"),
                    "pointsDelta": event.get("pointsDelta"),
                    "totalPoints": update.get("totalPoints"),
                    "worldTimeMs": event.get("worldTimeMs"),
                    "hazardId": event.get("hazardId"),
                    "zoneId": event.get("zoneId"),
                    "agentId": event.get("agentId"),
                },
            }
        if value.get("kind") == "scoring_report" and isinstance(value.get("report"), dict):
            report = value["report"]
            return {
                "kind": "scoring_report",
                "report": {
                    "type": report.get("type"),
                    "agentId": report.get("agentId"),
                    "hazardType": report.get("hazardType"),
                    "location": summarize_payload(report.get("location")),
                },
            }
        if value.get("kind") == "simulation_event" and isinstance(value.get("event"), dict):
            event = value["event"]
            event_type = event.get("type")
            if event_type == "simulation_ready":
                objectives = event.get("worldObjectives") or {}
                building_hazards = objectives.get("buildingHazards") or []
                road_hazards = objectives.get("roadHazards") or []
                connectivity_zones = objectives.get("connectivityZones") or []
                return {
                    "kind": "simulation_event",
                    "event": {
                        "type": "simulation_ready",
                        "worldTimeMs": event.get("worldTimeMs"),
                        "suggestedSpawn": summarize_payload(event.get("suggestedSpawn")),
                        "worldObjectives": {
                            "commandCenter": "<present>" if objectives.get("commandCenter") else "<missing>",
                            "buildingHazards": len(building_hazards),
                            "roadHazards": len(road_hazards),
                            "connectivityZones": len(connectivity_zones),
                        } if objectives else None,
                    },
                }
            if event_type == "agent_tick":
                snapshot = event.get("snapshot", {})
                agents = snapshot.get("agents") or []
                summarized_agents = []
                for agent in agents:
                    orientation = agent.get("orientation", {})
                    position = agent.get("position", {})
                    summarized_agents.append({
                        "id": agent.get("id"),
                        "modality": agent.get("modality"),
                        "position": {
                            "x": round(float(position.get("x", 0.0)), 2),
                            "y": round(float(position.get("y", 0.0)), 2),
                            "z": round(float(position.get("z", 0.0)), 2),
                        },
                        "orientation": {
                            "headingDeg": round(float(orientation.get("headingDeg", 0.0)), 1),
                            "pitchDeg": round(float(orientation.get("pitchDeg", 0.0)), 1),
                            "rollDeg": round(float(orientation.get("rollDeg", 0.0)), 1),
                        },
                        "speed": round(float(agent.get("speed", 0.0)), 2),
                    })
                return {
                    "kind": "simulation_event",
                    "event": {
                        "type": "agent_tick",
                        "worldTimeMs": snapshot.get("worldTimeMs"),
                        "agents": summarized_agents,
                        "captureRequests": len(snapshot.get("captureRequests") or []),
                    },
                }
            if event_type == "sensor_capture":
                payload = event.get("payload", {})
                return {
                    "kind": "simulation_event",
                    "event": {
                        "type": "sensor_capture",
                        "worldTimeMs": event.get("worldTimeMs"),
                        "agentId": event.get("agentId"),
                        "sensorId": event.get("sensorId"),
                        "sensorType": event.get("sensorType"),
                        "orientation": summarize_payload(event.get("orientation", {})),
                        "payload": summarize_payload(payload),
                    },
                }
        if "points" in value and isinstance(value["points"], list):
            summarized = {key: summarize_payload(inner) for key, inner in value.items() if key != "points"}
            summarized["points"] = f"<points len={len(value['points'])}>"
            return summarized
        return {key: summarize_payload(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [summarize_payload(inner) for inner in value]
    if isinstance(value, str):
        if value.startswith("data:image/"):
            return f"<image-data len={len(value)} prefix={value[:32]}...>"
        if len(value) > 160:
            return f"{value[:80]}...<{len(value) - 160} chars omitted>...{value[-80:]}"
    return value


async def send_command(websocket: WebSocket, command: dict[str, Any]) -> None:
    payload = {
        "kind": "simulation_command",
        "command": command,
    }
    print("[orchestrator:outbound]", summarize_payload(payload))
    await websocket.send_json(payload)


async def send_command_batch(websocket: WebSocket, commands: list[dict[str, Any]]) -> None:
    payload = {
        "kind": "simulation_command_batch",
        "commands": commands,
    }
    print("[orchestrator:outbound]", summarize_payload(payload))
    await websocket.send_json(payload)


async def send_scoring_report(websocket: WebSocket, report: dict[str, Any]) -> None:
    payload = {
        "kind": "scoring_report",
        "report": report,
    }
    print("[orchestrator:outbound]", summarize_payload(payload))
    await websocket.send_json(payload)


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def compute_camera_orientation(agent: dict[str, Any], target: dict[str, float]) -> tuple[float, float]:
    position = agent["position"]
    orientation = agent["orientation"]

    dx = float(target["x"]) - float(position["x"])
    dy = float(target["y"]) - float(position["y"])
    dz = float(target["z"]) - float(position["z"])

    heading_rad = math.radians(float(orientation["headingDeg"]))
    pitch_rad = math.radians(float(orientation["pitchDeg"]))
    roll_rad = math.radians(float(orientation["rollDeg"]))

    ch = math.cos(heading_rad)
    sh = math.sin(heading_rad)
    cp = math.cos(pitch_rad)
    sp = math.sin(pitch_rad)
    cr = math.cos(roll_rad)
    sr = math.sin(roll_rad)

    forward_x = sh * cp
    forward_y = sp
    forward_z = ch * cp

    right_x = ch * cr + sh * sp * sr
    right_y = -cp * sr
    right_z = -sh * cr + ch * sp * sr

    up_x = forward_y * right_z - forward_z * right_y
    up_y = forward_z * right_x - forward_x * right_z
    up_z = forward_x * right_y - forward_y * right_x

    target_len = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
    target_x = dx / target_len
    target_y = dy / target_len
    target_z = dz / target_len

    local_x = target_x * right_x + target_y * right_y + target_z * right_z
    local_y = target_x * up_x + target_y * up_y + target_z * up_z
    local_z = target_x * forward_x + target_y * forward_y + target_z * forward_z

    pan_deg = math.degrees(math.atan2(local_x, local_z))
    tilt_deg = math.degrees(math.atan2(local_y, math.hypot(local_x, local_z)))
    return pan_deg, tilt_deg


class Scenario:
    async def handle_event(self, websocket: WebSocket, event: dict[str, Any]) -> None:
        raise NotImplementedError

    async def dispose(self) -> None:
        return


def distance_sq(a: dict[str, Any], b: dict[str, Any]) -> float:
    dx = float(a.get("x", 0.0)) - float(b.get("x", 0.0))
    dy = float(a.get("y", 0.0)) - float(b.get("y", 0.0))
    dz = float(a.get("z", 0.0)) - float(b.get("z", 0.0))
    return dx * dx + dy * dy + dz * dz


def offset_geo(geo: dict[str, Any], *, lat_offset_deg: float, lng_offset_deg: float) -> dict[str, float]:
    return {
        "lat": float(geo.get("lat", 0.0)) + lat_offset_deg,
        "lng": float(geo.get("lng", 0.0)) + lng_offset_deg,
    }
