import random
from typing import Any

from fastapi import WebSocket

from .common import Scenario, clamp, compute_camera_orientation, send_command, send_command_batch


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
                            "config": {"fovDeg": 60, "aspect": 16 / 9},
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
            self.center_point = {"x": float(position["x"]), "y": 0.0, "z": float(position["z"])}
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
                {"type": "set_fixed_wing_controls", "agentId": "fixed-wing-1", "aileron": aileron, "elevator": elevator, "rudder": rudder, "throttle": throttle},
                {"type": "set_sensor_orientation", "agentId": "fixed-wing-1", "sensorId": "primary-camera-1", "panDeg": pan_deg, "tiltDeg": tilt_deg},
                {"type": "set_sensor_orientation", "agentId": "fixed-wing-1", "sensorId": "primary-lidar-1", "panDeg": pan_deg, "tiltDeg": tilt_deg},
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
