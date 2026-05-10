import math
from typing import Any

from fastapi import WebSocket

from .common import Scenario, send_command_batch

COMMAND_LINK_TARGET_M = 175.0
RELAY_LINK_TARGET_M = 135.0
ZONE_SERVICE_TARGET_M = 70.0
MIN_TARGET_DISTANCE_M = 5.0
QUADCOPTER_CRUISE_ALT_M = 200.0


class ConnectivityProbeScenario(Scenario):
    def __init__(self) -> None:
        self.started = False

    async def handle_event(self, websocket: WebSocket, event: dict[str, Any]) -> None:
        if event.get("type") != "simulation_ready" or self.started:
            return
        self.started = True

        objectives = event.get("worldObjectives") or {}
        command_center = objectives.get("commandCenter") or {}
        command_world = command_center.get("worldPosition") or {}
        command_geo = command_center.get("geoPosition") or {}
        connectivity_zones = objectives.get("connectivityZones") or []
        meters_per_unit = float(event.get("metersPerUnit") or 1.0)

        if not command_world or not command_geo or not connectivity_zones or meters_per_unit <= 0:
            print("[scenario] connectivity_probe -> missing command center or connectivity zones")
            return

        closest_zone = min(
            connectivity_zones,
            key=lambda zone: _distance_world_units(command_world, zone.get("worldPosition") or {}),
        )
        zone_world = closest_zone.get("worldPosition") or {}
        zone_geo = closest_zone.get("geoPosition") or {}
        zone_id = str(closest_zone.get("id") or "unknown-zone")
        distance_world_units = _distance_world_units(command_world, zone_world)
        distance_m = distance_world_units * meters_per_unit
        quad_count = _required_quadcopter_count(distance_m)
        cruise_alt_m = float((event.get("suggestedSpawn") or {}).get("alt") or QUADCOPTER_CRUISE_ALT_M)
        target_distances_m = _target_distances(distance_m, quad_count)

        print(
            "[scenario] connectivity_probe -> "
            f"zone={zone_id} distance={distance_m:.1f}m "
            f"distance_units={distance_world_units:.2f} mpu={meters_per_unit:.3f} quads={quad_count} "
            f"targets={[round(distance, 1) for distance in target_distances_m]}"
        )

        commands: list[dict[str, Any]] = []
        for index, target_distance_m in enumerate(target_distances_m, start=1):
            fraction = 0.0 if distance_m <= 1e-6 else min(1.0, max(0.0, target_distance_m / distance_m))
            target_world = _interpolate_world(command_world, zone_world, fraction)
            target_geo = _interpolate_geo(command_geo, zone_geo, fraction, cruise_alt_m)
            agent_id = f"connectivity-probe-{index}"

            commands.append(
                {
                    "type": "spawn_agent",
                    "agentId": agent_id,
                    "modality": "quadcopter",
                    "start": {
                        "lat": float(command_geo.get("lat", 0.0)),
                        "lng": float(command_geo.get("lng", 0.0)),
                        "alt": cruise_alt_m,
                    },
                    "waypoints": [target_geo],
                    "sensors": [],
                }
            )
            commands.append(
                {
                    "type": "set_debug_marker",
                    "markerId": f"connectivity-probe-target-{index}",
                    "position": target_world,
                    "color": 0x33ccff if index < quad_count else 0x00ff88,
                    "radius": 2.0,
                }
            )

        await send_command_batch(websocket, commands)


def _required_quadcopter_count(distance_m: float) -> int:
    remaining_m = max(0.0, distance_m - COMMAND_LINK_TARGET_M - ZONE_SERVICE_TARGET_M)
    return 1 + math.ceil(remaining_m / RELAY_LINK_TARGET_M)


def _target_distances(distance_m: float, quad_count: int) -> list[float]:
    if quad_count <= 1:
        return [max(MIN_TARGET_DISTANCE_M, min(COMMAND_LINK_TARGET_M, max(MIN_TARGET_DISTANCE_M, distance_m - ZONE_SERVICE_TARGET_M)))]

    distances = [COMMAND_LINK_TARGET_M + RELAY_LINK_TARGET_M * index for index in range(quad_count - 1)]
    distances.append(max(MIN_TARGET_DISTANCE_M, distance_m - ZONE_SERVICE_TARGET_M))
    return distances


def _distance_world_units(a: dict[str, Any], b: dict[str, Any]) -> float:
    dx = float(a.get("x", 0.0)) - float(b.get("x", 0.0))
    dy = float(a.get("y", 0.0)) - float(b.get("y", 0.0))
    dz = float(a.get("z", 0.0)) - float(b.get("z", 0.0))
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _interpolate_world(a: dict[str, Any], b: dict[str, Any], fraction: float) -> dict[str, float]:
    return {
        "x": _lerp(float(a.get("x", 0.0)), float(b.get("x", 0.0)), fraction),
        "y": _lerp(float(a.get("y", 0.0)), float(b.get("y", 0.0)), fraction),
        "z": _lerp(float(a.get("z", 0.0)), float(b.get("z", 0.0)), fraction),
    }


def _interpolate_geo(a: dict[str, Any], b: dict[str, Any], fraction: float, altitude_m: float) -> dict[str, float]:
    return {
        "lat": _lerp(float(a.get("lat", 0.0)), float(b.get("lat", 0.0)), fraction),
        "lng": _lerp(float(a.get("lng", 0.0)), float(b.get("lng", 0.0)), fraction),
        "alt": altitude_m,
    }


def _lerp(start: float, end: float, fraction: float) -> float:
    return start + (end - start) * fraction
