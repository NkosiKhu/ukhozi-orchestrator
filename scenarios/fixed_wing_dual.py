import random
from typing import Any

from fastapi import WebSocket

from .common import Scenario, clamp, compute_camera_orientation, distance_sq, send_command_batch


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
                    controller["center_point"] = {"x": float(position["x"]), "y": 0.0, "z": float(position["z"])}
                else:
                    first_center = self.controllers["fixed-wing-1"]["center_point"]
                    units_per_meter = float(position["y"]) / max(self.spawn_altitude_m, 1.0)
                    north_offset_units = 1000.0 * units_per_meter
                    base_x = float(first_center["x"]) if first_center else float(position["x"])
                    base_z = float(first_center["z"]) if first_center else float(position["z"])
                    controller["center_point"] = {"x": base_x, "y": 0.0, "z": base_z - north_offset_units}
                commands.append({
                    "type": "set_debug_marker",
                    "markerId": f"poi-{agent_id}",
                    "position": controller["center_point"],
                    "color": 0xff3366 if index == 0 else 0x44dd88,
                    "radius": 2.2,
                })
                print(f"[scenario] {agent_id} center fixed at ({controller['center_point']['x']:.2f}, {controller['center_point']['z']:.2f})")
            if controller["cruise_altitude_y"] is None:
                units_per_meter = float(agent["position"]["y"]) / max(self.spawn_altitude_m, 1.0)
                controller["cruise_altitude_y"] = float(agent["position"]["y"]) - 50.0 * units_per_meter
                print(f"[scenario] {agent_id} cruise altitude y={controller['cruise_altitude_y']:.2f}")
            if controller["state_started_at_ms"] is None:
                controller["state_started_at_ms"] = world_time_ms
                print(f"[scenario] {agent_id} enter {controller['state']} for {controller['state_duration_ms'] / 1000:.1f}s")
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
            {"type": "set_fixed_wing_controls", "agentId": agent_id, "aileron": aileron, "elevator": elevator, "rudder": rudder, "throttle": throttle},
            {"type": "set_sensor_orientation", "agentId": agent_id, "sensorId": f"{agent_id}-camera", "panDeg": pan_deg, "tiltDeg": tilt_deg},
            {"type": "set_sensor_orientation", "agentId": agent_id, "sensorId": f"{agent_id}-lidar", "panDeg": pan_deg, "tiltDeg": tilt_deg},
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
        help_hazards = [hazard for hazard in (objectives.get("buildingHazards") or []) if hazard.get("type") == "help"]
        if connectivity_zones:
            zone = random.choice(connectivity_zones)
            world_position = zone.get("worldPosition") or {}
            self.objective_targets["fixed-wing-1"] = {"x": float(world_position.get("x", 0.0)), "y": float(world_position.get("y", 0.0)), "z": float(world_position.get("z", 0.0))}
            print("[scenario] fixed-wing-1 objective -> connectivity zone " f"{zone.get('id')} at ({self.objective_targets['fixed-wing-1']['x']:.2f}, {self.objective_targets['fixed-wing-1']['z']:.2f})")
        if help_hazards:
            hazard = random.choice(help_hazards)
            world_position = hazard.get("worldPosition") or {}
            self.objective_targets["fixed-wing-2"] = {"x": float(world_position.get("x", 0.0)), "y": float(world_position.get("y", 0.0)), "z": float(world_position.get("z", 0.0))}
            print("[scenario] fixed-wing-2 objective -> help hazard " f"{hazard.get('id')} at ({self.objective_targets['fixed-wing-2']['x']:.2f}, {self.objective_targets['fixed-wing-2']['z']:.2f})")


class PyromaniacScenario(FixedWingDualExploreScenario):
    def _assign_objective_targets(self, objectives: dict[str, Any] | None) -> None:
        if not objectives:
            return
        command_center = (objectives.get("commandCenter") or {}).get("worldPosition") or {}
        cx = float(command_center.get("x", 0.0))
        cy = float(command_center.get("y", 0.0))
        cz = float(command_center.get("z", 0.0))

        fire_hazards = [hazard for hazard in (objectives.get("buildingHazards") or []) if hazard.get("type") == "fire"]
        if not fire_hazards:
            print("[scenario] pyromaniac -> no fire hazards found")
            return

        ranked_hazards = sorted(
            fire_hazards,
            key=lambda hazard: distance_sq(hazard.get("worldPosition") or {}, {"x": cx, "y": cy, "z": cz}),
        )
        for agent_id, hazard in zip(self.agent_ids, ranked_hazards[: len(self.agent_ids)]):
            world_position = hazard.get("worldPosition") or {}
            self.objective_targets[agent_id] = {"x": float(world_position.get("x", 0.0)), "y": float(world_position.get("y", 0.0)), "z": float(world_position.get("z", 0.0))}
            print(
                f"[scenario] {agent_id} objective -> fire hazard {hazard.get('id')} "
                f"at ({self.objective_targets[agent_id]['x']:.2f}, {self.objective_targets[agent_id]['z']:.2f})"
            )
