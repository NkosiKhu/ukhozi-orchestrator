import asyncio
import math
import random
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


def compute_camera_orientation(
    agent: dict[str, Any],
    target: dict[str, float],
) -> tuple[float, float]:
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


class FixedWingExploreScenario(Scenario):
    def __init__(self) -> None:
        self.spawned = False
        self.center_point: dict[str, float] | None = None
        self.cruise_altitude_y: float | None = None
        self.state = "straight"
        self.state_started_at_ms: int | None = None
        self.state_duration_ms = self._next_straight_duration_ms()
        self.bank_direction = 1.0
        self.last_logged_second = -1

    async def handle_event(self, websocket: WebSocket, event: dict[str, Any]) -> None:
        event_type = event.get("type")

        if event_type == "simulation_ready" and not self.spawned:
            self.spawned = True
            start = event.get("suggestedSpawn") or {"lat": 0, "lng": 0, "alt": 100}
            print("[scenario] ready -> spawn fixed-wing explore")
            await send_command(
                websocket,
                {
                    "type": "spawn_agent",
                    "modality": "fixed_wing",
                    "start": start,
                    "headingDeg": 0,
                    "sensors": [
                        {
                            "sensorId": "primary-camera-1",
                            "type": "camera",
                            "mount": "sensor_mount",
                            "offset": {"x": 0.0, "y": 0.0, "z": 0.0},
                            "orientation": {"panDeg": 0, "tiltDeg": -45},
                            "captureIntervalMs": 1000,
                            "config": {
                                "fovDeg": 60,
                                "aspect": 16 / 9,
                            },
                        },
                        {
                            "sensorId": "primary-lidar-1",
                            "type": "lidar",
                            "mount": "sensor_mount",
                            "offset": {"x": 0.0, "y": 0.0, "z": 0.0},
                            "orientation": {"panDeg": 0, "tiltDeg": -60},
                            "captureIntervalMs": 1000,
                            "config": {
                                "hFovDeg": 60,
                                "vFovDeg": 20,
                                "hDensity": 24,
                                "vDensity": 8,
                                "maxRangeM": 300,
                                "nearPlaneM": 0.05,
                            },
                        },
                    ],
                },
            )
            return

        if event_type == "agent_spawned" and event.get("agentId") == "fixed-wing-1":
            print("[scenario] spawned fixed-wing-1")
            return

        if event_type != "agent_tick":
            return

        snapshot = event.get("snapshot")
        if not snapshot:
            return
        agents = snapshot.get("agents") or []
        agent = next((candidate for candidate in agents if candidate.get("id") == "fixed-wing-1"), None)
        if not agent:
            return

        world_time_ms = int(snapshot.get("worldTimeMs") or 0)
        if self.center_point is None:
            position = agent["position"]
            self.center_point = {
                "x": float(position["x"]),
                "y": 0.0,
                "z": float(position["z"]),
            }
            print(f"[scenario] center fixed at ({self.center_point['x']:.2f}, {self.center_point['z']:.2f})")
        if self.cruise_altitude_y is None:
            units_per_meter = float(agent["position"]["y"]) / 200.0
            self.cruise_altitude_y = float(agent["position"]["y"]) - 50.0 * units_per_meter
            print(f"[scenario] cruise altitude fixed at y={self.cruise_altitude_y:.2f}")

        if self.state_started_at_ms is None:
            self.state_started_at_ms = world_time_ms
            print(f"[scenario] enter {self.state} for {self.state_duration_ms / 1000:.1f}s")

        if world_time_ms - self.state_started_at_ms >= self.state_duration_ms:
            self._advance_state(agent, world_time_ms)

        roll_deg = float(agent["orientation"]["rollDeg"])
        pitch_deg = float(agent["orientation"]["pitchDeg"])
        altitude_y = float(agent["position"]["y"])

        if self.state == "straight":
            altitude_error = (self.cruise_altitude_y or altitude_y) - altitude_y
            aileron = clamp((-roll_deg) * 0.05, -1, 1)
            elevator = clamp(altitude_error * 0.08 + (-pitch_deg) * 0.04, -1, 1)
            rudder = 0.0
            throttle = 0.55
            pan_deg = 0.0
            tilt_deg = -40.0
        else:
            target_bank_deg = self.bank_direction * 45.0
            aileron = clamp((target_bank_deg - roll_deg) * 0.05, -1, 1)
            elevator = clamp(0.15 - pitch_deg * 0.03, -1, 1)
            rudder = clamp((0.0 - pitch_deg) * 0.06, -1, 1)
            throttle = 0.55
            pan_deg, tilt_deg = compute_camera_orientation(agent, self.center_point)

        await send_command_batch(
            websocket,
            [
                {
                    "type": "set_fixed_wing_controls",
                    "agentId": "fixed-wing-1",
                    "aileron": aileron,
                    "elevator": elevator,
                    "rudder": rudder,
                    "throttle": throttle,
                },
                {
                    "type": "set_sensor_orientation",
                    "agentId": "fixed-wing-1",
                    "sensorId": "primary-camera-1",
                    "panDeg": pan_deg,
                    "tiltDeg": tilt_deg,
                },
                {
                    "type": "set_sensor_orientation",
                    "agentId": "fixed-wing-1",
                    "sensorId": "primary-lidar-1",
                    "panDeg": pan_deg,
                    "tiltDeg": tilt_deg,
                },
            ],
        )

        tick_second = world_time_ms // 1000
        if tick_second != self.last_logged_second:
            self.last_logged_second = tick_second
            position = agent["position"]
            print(
                "[scenario] "
                f"{self.state} pos=({float(position['x']):.1f},{float(position['z']):.1f}) "
                f"hdg={float(agent['orientation']['headingDeg']):.1f} "
                f"pitch={pitch_deg:.1f} roll={roll_deg:.1f} "
                f"ctl=({aileron:.2f},{elevator:.2f},{rudder:.2f},{throttle:.2f})"
            )

    def _advance_state(self, agent: dict[str, Any], world_time_ms: int) -> None:
        if self.state == "straight":
            self.state = "banking"
            self.bank_direction = self._bank_direction_toward_center(agent)
            self.state_duration_ms = self._next_bank_duration_ms()
            direction_label = "right" if self.bank_direction > 0 else "left"
            print(f"[scenario] enter banking {direction_label} for {self.state_duration_ms / 1000:.1f}s")
        else:
            self.state = "straight"
            self.state_duration_ms = self._next_straight_duration_ms()
            print(f"[scenario] enter straight for {self.state_duration_ms / 1000:.1f}s")
        self.state_started_at_ms = world_time_ms

    def _bank_direction_toward_center(self, agent: dict[str, Any]) -> float:
        if self.center_point is None:
            return 1.0
        position = agent["position"]
        forward = agent["forward"]
        dx = float(self.center_point["x"]) - float(position["x"])
        dz = float(self.center_point["z"]) - float(position["z"])
        fx = float(forward["x"])
        fz = float(forward["z"])
        cross = fx * dz - fz * dx
        return -1.0 if cross > 0 else 1.0

    def _next_straight_duration_ms(self) -> int:
        return random.randint(3000, 8000)

    def _next_bank_duration_ms(self) -> int:
        return random.randint(3000, 10000)

    async def dispose(self) -> None:
        return


SCENARIOS: dict[str, type[Scenario]] = {
    "fixed_wing_circle_track": FixedWingExploreScenario,
    "fixed_wing_explore": FixedWingExploreScenario,
}


class FixedWingDualExploreScenario(Scenario):
    def __init__(self) -> None:
        self.spawned = False
        self.agent_ids = ["fixed-wing-1", "fixed-wing-2"]
        self.spawn_altitude_m = 200.0
        self.objective_targets: dict[str, dict[str, float] | None] = {
            "fixed-wing-1": None,
            "fixed-wing-2": None,
        }
        self.controllers: dict[str, dict[str, Any]] = {
            agent_id: {
                "center_point": None,
                "cruise_altitude_y": None,
                "state": "straight",
                "state_started_at_ms": None,
                "state_duration_ms": self._next_straight_duration_ms(),
                "bank_direction": 1.0,
            }
            for agent_id in self.agent_ids
        }
        self.last_logged_second = -1

    async def handle_event(self, websocket: WebSocket, event: dict[str, Any]) -> None:
        event_type = event.get("type")

        if event_type == "simulation_ready" and not self.spawned:
            self.spawned = True
            start_a = event.get("suggestedSpawn") or {"lat": 0, "lng": 0, "alt": 100}
            self.spawn_altitude_m = float(start_a["alt"])
            self._assign_objective_targets(event.get("worldObjectives"))
            start_b = dict(start_a)
            print("[scenario] ready -> spawn dual fixed-wing explore")
            await send_command_batch(
                websocket,
                [
                    self._spawn_agent_command("fixed-wing-1", start_a),
                    self._spawn_agent_command("fixed-wing-2", start_b),
                ],
            )
            return

        if event_type == "agent_spawned" and event.get("agentId") in self.agent_ids:
            print(f"[scenario] spawned {event.get('agentId')}")
            return

        if event_type != "agent_tick":
            return

        snapshot = event.get("snapshot")
        if not snapshot:
            return
        world_time_ms = int(snapshot.get("worldTimeMs") or 0)
        agents = {agent.get("id"): agent for agent in (snapshot.get("agents") or [])}

        commands: list[dict[str, Any]] = []
        for index, agent_id in enumerate(self.agent_ids):
            agent = agents.get(agent_id)
            if not agent:
                continue
            controller = self.controllers[agent_id]
            if controller["center_point"] is None:
                position = agent["position"]
                target = self.objective_targets.get(agent_id)
                if target is not None:
                    controller["center_point"] = dict(target)
                elif index == 0:
                    controller["center_point"] = {
                        "x": float(position["x"]),
                        "y": 0.0,
                        "z": float(position["z"]),
                    }
                else:
                    first_center = self.controllers["fixed-wing-1"]["center_point"]
                    units_per_meter = float(position["y"]) / max(self.spawn_altitude_m, 1.0)
                    north_offset_units = 1000.0 * units_per_meter
                    base_x = float(first_center["x"]) if first_center else float(position["x"])
                    base_z = float(first_center["z"]) if first_center else float(position["z"])
                    controller["center_point"] = {
                        "x": base_x,
                        "y": 0.0,
                        "z": base_z - north_offset_units,
                    }
                commands.append({
                    "type": "set_debug_marker",
                    "markerId": f"poi-{agent_id}",
                    "position": controller["center_point"],
                    "color": 0xff3366 if index == 0 else 0x44dd88,
                    "radius": 2.2,
                })
                print(
                    f"[scenario] {agent_id} center fixed at "
                    f"({controller['center_point']['x']:.2f}, {controller['center_point']['z']:.2f})"
                )
            if controller["cruise_altitude_y"] is None:
                units_per_meter = float(agent["position"]["y"]) / max(self.spawn_altitude_m, 1.0)
                controller["cruise_altitude_y"] = float(agent["position"]["y"]) - 50.0 * units_per_meter
                print(f"[scenario] {agent_id} cruise altitude y={controller['cruise_altitude_y']:.2f}")
            if controller["state_started_at_ms"] is None:
                controller["state_started_at_ms"] = world_time_ms
                print(
                    f"[scenario] {agent_id} enter {controller['state']} "
                    f"for {controller['state_duration_ms'] / 1000:.1f}s"
                )
            if world_time_ms - controller["state_started_at_ms"] >= controller["state_duration_ms"]:
                self._advance_agent_state(agent_id, agent, world_time_ms)

            commands.extend(self._agent_commands(agent_id, agent))

        if commands:
            await send_command_batch(websocket, commands)

        tick_second = world_time_ms // 1000
        if tick_second != self.last_logged_second:
            self.last_logged_second = tick_second
            summary = []
            for agent_id in self.agent_ids:
                agent = agents.get(agent_id)
                if not agent:
                    continue
                controller = self.controllers[agent_id]
                position = agent["position"]
                orientation = agent["orientation"]
                summary.append(
                    f"{agent_id}:{controller['state']} "
                    f"pos=({float(position['x']):.1f},{float(position['z']):.1f}) "
                    f"hdg={float(orientation['headingDeg']):.1f} "
                    f"pitch={float(orientation['pitchDeg']):.1f} "
                    f"roll={float(orientation['rollDeg']):.1f}"
                )
            print("[scenario] " + " | ".join(summary))

    def _spawn_agent_command(self, agent_id: str, start: dict[str, float]) -> dict[str, Any]:
        return {
            "type": "spawn_agent",
            "agentId": agent_id,
            "modality": "fixed_wing",
            "start": start,
            "headingDeg": 0,
            "sensors": [
                {
                    "sensorId": f"{agent_id}-camera",
                    "type": "camera",
                    "mount": "sensor_mount",
                    "offset": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "orientation": {"panDeg": 0, "tiltDeg": -45},
                    "captureIntervalMs": 1000,
                    "config": {"fovDeg": 60, "aspect": 16 / 9},
                },
                {
                    "sensorId": f"{agent_id}-lidar",
                    "type": "lidar",
                    "mount": "sensor_mount",
                    "offset": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "orientation": {"panDeg": 0, "tiltDeg": -60},
                    "captureIntervalMs": 1000,
                    "config": {
                        "hFovDeg": 60,
                        "vFovDeg": 20,
                        "hDensity": 24,
                        "vDensity": 8,
                        "maxRangeM": 300,
                        "nearPlaneM": 0.05,
                    },
                },
            ],
        }

    def _agent_commands(self, agent_id: str, agent: dict[str, Any]) -> list[dict[str, Any]]:
        controller = self.controllers[agent_id]
        center_point = controller["center_point"]
        roll_deg = float(agent["orientation"]["rollDeg"])
        pitch_deg = float(agent["orientation"]["pitchDeg"])
        altitude_y = float(agent["position"]["y"])

        if controller["state"] == "straight":
            altitude_error = float(controller["cruise_altitude_y"] or altitude_y) - altitude_y
            aileron = clamp((-roll_deg) * 0.05, -1, 1)
            elevator = clamp(altitude_error * 0.08 + (-pitch_deg) * 0.04, -1, 1)
            rudder = 0.0
            throttle = 0.55
            pan_deg = 0.0
            tilt_deg = -40.0
        else:
            target_bank_deg = float(controller["bank_direction"]) * 45.0
            aileron = clamp((target_bank_deg - roll_deg) * 0.05, -1, 1)
            elevator = clamp(0.15 - pitch_deg * 0.03, -1, 1)
            rudder = clamp((0.0 - pitch_deg) * 0.06, -1, 1)
            throttle = 0.55
            pan_deg, tilt_deg = compute_camera_orientation(agent, center_point)

        return [
            {
                "type": "set_fixed_wing_controls",
                "agentId": agent_id,
                "aileron": aileron,
                "elevator": elevator,
                "rudder": rudder,
                "throttle": throttle,
            },
            {
                "type": "set_sensor_orientation",
                "agentId": agent_id,
                "sensorId": f"{agent_id}-camera",
                "panDeg": pan_deg,
                "tiltDeg": tilt_deg,
            },
            {
                "type": "set_sensor_orientation",
                "agentId": agent_id,
                "sensorId": f"{agent_id}-lidar",
                "panDeg": pan_deg,
                "tiltDeg": tilt_deg,
            },
        ]

    def _advance_agent_state(self, agent_id: str, agent: dict[str, Any], world_time_ms: int) -> None:
        controller = self.controllers[agent_id]
        if controller["state"] == "straight":
            controller["state"] = "banking"
            controller["bank_direction"] = self._bank_direction_toward_center(agent_id, agent)
            controller["state_duration_ms"] = self._next_bank_duration_ms()
            direction_label = "right" if float(controller["bank_direction"]) > 0 else "left"
            print(f"[scenario] {agent_id} enter banking {direction_label} for {controller['state_duration_ms'] / 1000:.1f}s")
        else:
            controller["state"] = "straight"
            controller["state_duration_ms"] = self._next_straight_duration_ms()
            print(f"[scenario] {agent_id} enter straight for {controller['state_duration_ms'] / 1000:.1f}s")
        controller["state_started_at_ms"] = world_time_ms

    def _bank_direction_toward_center(self, agent_id: str, agent: dict[str, Any]) -> float:
        center_point = self.controllers[agent_id]["center_point"]
        if center_point is None:
            return 1.0
        position = agent["position"]
        forward = agent["forward"]
        dx = float(center_point["x"]) - float(position["x"])
        dz = float(center_point["z"]) - float(position["z"])
        fx = float(forward["x"])
        fz = float(forward["z"])
        cross = fx * dz - fz * dx
        return -1.0 if cross > 0 else 1.0

    def _next_straight_duration_ms(self) -> int:
        return random.randint(3000, 8000)

    def _next_bank_duration_ms(self) -> int:
        return random.randint(3000, 10000)

    def _assign_objective_targets(self, objectives: dict[str, Any] | None) -> None:
        if not objectives:
            return
        connectivity_zones = objectives.get("connectivityZones") or []
        help_hazards = [
            hazard for hazard in (objectives.get("buildingHazards") or [])
            if hazard.get("type") == "help"
        ]
        if connectivity_zones:
            zone = random.choice(connectivity_zones)
            world_position = zone.get("worldPosition") or {}
            self.objective_targets["fixed-wing-1"] = {
                "x": float(world_position.get("x", 0.0)),
                "y": float(world_position.get("y", 0.0)),
                "z": float(world_position.get("z", 0.0)),
            }
            print(
                "[scenario] fixed-wing-1 objective -> connectivity zone "
                f"{zone.get('id')} at ({self.objective_targets['fixed-wing-1']['x']:.2f}, "
                f"{self.objective_targets['fixed-wing-1']['z']:.2f})"
            )
        if help_hazards:
            hazard = random.choice(help_hazards)
            world_position = hazard.get("worldPosition") or {}
            self.objective_targets["fixed-wing-2"] = {
                "x": float(world_position.get("x", 0.0)),
                "y": float(world_position.get("y", 0.0)),
                "z": float(world_position.get("z", 0.0)),
            }
            print(
                "[scenario] fixed-wing-2 objective -> help hazard "
                f"{hazard.get('id')} at ({self.objective_targets['fixed-wing-2']['x']:.2f}, "
                f"{self.objective_targets['fixed-wing-2']['z']:.2f})"
            )

    async def dispose(self) -> None:
        return


SCENARIOS["fixed_wing_dual_explore"] = FixedWingDualExploreScenario


class PyromaniacScenario(FixedWingDualExploreScenario):
    def _assign_objective_targets(self, objectives: dict[str, Any] | None) -> None:
        if not objectives:
            return

        command_center = (objectives.get("commandCenter") or {}).get("worldPosition") or {}
        cx = float(command_center.get("x", 0.0))
        cy = float(command_center.get("y", 0.0))
        cz = float(command_center.get("z", 0.0))

        fire_hazards = [
            hazard for hazard in (objectives.get("buildingHazards") or [])
            if hazard.get("type") == "fire"
        ]
        if not fire_hazards:
            print("[scenario] pyromaniac -> no fire hazards found")
            return

        ranked_hazards = sorted(
            fire_hazards,
            key=lambda hazard: _distance_sq(
                hazard.get("worldPosition") or {},
                {"x": cx, "y": cy, "z": cz},
            ),
        )
        for agent_id, hazard in zip(self.agent_ids, ranked_hazards[: len(self.agent_ids)]):
            world_position = hazard.get("worldPosition") or {}
            self.objective_targets[agent_id] = {
                "x": float(world_position.get("x", 0.0)),
                "y": float(world_position.get("y", 0.0)),
                "z": float(world_position.get("z", 0.0)),
            }
            print(
                f"[scenario] {agent_id} objective -> fire hazard {hazard.get('id')} "
                f"at ({self.objective_targets[agent_id]['x']:.2f}, "
                f"{self.objective_targets[agent_id]['z']:.2f})"
            )


class ScoringProbeScenario(Scenario):
    def __init__(self) -> None:
        self.started = False
        self.probe_task: asyncio.Task[None] | None = None

    async def handle_event(self, websocket: WebSocket, event: dict[str, Any]) -> None:
        if event.get("type") != "simulation_ready" or self.started:
            return
        self.started = True
        objectives = event.get("worldObjectives") or {}
        probes = self._build_probes(objectives)
        if not probes:
            print("[scenario] scoring_probe -> no hazards available for scoring probes")
            return
        print(f"[scenario] scoring_probe -> scheduling {len(probes)} scoring probes")
        self.probe_task = asyncio.create_task(self._run_probes(websocket, probes))

    async def dispose(self) -> None:
        if self.probe_task is None:
            return
        self.probe_task.cancel()
        try:
            await self.probe_task
        except asyncio.CancelledError:
            pass

    def _build_probes(self, objectives: dict[str, Any]) -> list[dict[str, Any]]:
        hazards = [
            hazard
            for hazard in [
                *((objectives.get("buildingHazards") or [])),
                *((objectives.get("roadHazards") or [])),
            ]
            if hazard.get("geoPosition")
        ]
        if not hazards:
            return []

        primary = hazards[0]
        primary_type = str(primary.get("type") or "unknown")
        correct_label = primary_type if primary_type in {"fire", "help", "debris"} else "unknown"
        incorrect_label = next(
            candidate for candidate in ("fire", "help", "debris")
            if candidate != correct_label
        )
        far_location = _offset_geo(primary.get("geoPosition") or {}, lat_offset_deg=0.01, lng_offset_deg=0.01)

        return [
            self._probe_case(
                "correct_hazard_unknown_label",
                primary,
                "unknown",
                "expect hazard_identified_unknown",
            ),
            self._probe_case(
                "correct_hazard_incorrect_label",
                primary,
                incorrect_label,
                "expect hazard_identified_wrong_type",
            ),
            self._probe_case(
                "correct_hazard_correct_label",
                primary,
                correct_label,
                "expect hazard_identified_correct",
            ),
            {
                "name": "incorrect_hazard_correct_label",
                "report": {
                    "type": "hazard_identified",
                    "agentId": "score-probe-agent",
                    "hazardType": correct_label,
                    "location": far_location,
                },
                "expectation": "expect hazard_identified_invalid",
            },
            {
                "name": "incorrect_hazard_incorrect_label",
                "report": {
                    "type": "hazard_identified",
                    "agentId": "score-probe-agent",
                    "hazardType": incorrect_label,
                    "location": far_location,
                },
                "expectation": "expect hazard_identified_invalid",
            },
        ]

    def _probe_case(
        self,
        name: str,
        hazard: dict[str, Any],
        hazard_type: str,
        expectation: str,
    ) -> dict[str, Any]:
        geo = hazard.get("geoPosition") or {}
        return {
            "name": name,
            "report": {
                "type": "hazard_identified",
                "agentId": "score-probe-agent",
                "hazardType": hazard_type,
                "location": {
                    "lat": float(geo.get("lat", 0.0)),
                    "lng": float(geo.get("lng", 0.0)),
                },
            },
            "expectation": expectation,
        }

    async def _run_probes(self, websocket: WebSocket, probes: list[dict[str, Any]]) -> None:
        try:
            for index, probe in enumerate(probes, start=1):
                print(
                    f"[scenario] scoring_probe {index}/{len(probes)} -> "
                    f"{probe['name']} ({probe['expectation']})"
                )
                await send_scoring_report(websocket, probe["report"])
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise


def _distance_sq(a: dict[str, Any], b: dict[str, Any]) -> float:
    dx = float(a.get("x", 0.0)) - float(b.get("x", 0.0))
    dy = float(a.get("y", 0.0)) - float(b.get("y", 0.0))
    dz = float(a.get("z", 0.0)) - float(b.get("z", 0.0))
    return dx * dx + dy * dy + dz * dz


SCENARIOS["pyromaniac"] = PyromaniacScenario
SCENARIOS["scoring_probe"] = ScoringProbeScenario


def _offset_geo(geo: dict[str, Any], *, lat_offset_deg: float, lng_offset_deg: float) -> dict[str, float]:
    return {
        "lat": float(geo.get("lat", 0.0)) + lat_offset_deg,
        "lng": float(geo.get("lng", 0.0)) + lng_offset_deg,
    }
