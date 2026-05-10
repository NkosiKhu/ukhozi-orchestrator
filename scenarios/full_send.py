import math
from typing import Any

from fastapi import WebSocket

from .common import Scenario, clamp, compute_camera_orientation, distance_sq, send_command_batch

CONNECTIVITY_COMMAND_LINK_M = 180.0
CONNECTIVITY_SERVICE_RADIUS_M = 35.0
CONNECTIVITY_RELAY_LINK_M = 140.0
QUADCOPTER_CRUISE_ALT_M = 200.0
AGV_ARRIVAL_RADIUS_M = 10.0
AGV_SLOW_RADIUS_M = 25.0


class FullSendScenario(Scenario):
    def __init__(self) -> None:
        self.started = False
        self.air_agent_ids = ["full-send-air-1", "full-send-air-2"]
        self.relay_agent_ids: list[str] = []
        self.agv_agent_id = "full-send-agv-1"
        self.spawn_altitude_m = QUADCOPTER_CRUISE_ALT_M
        self.meters_per_unit = 1.0
        self.air_targets: dict[str, dict[str, float] | None] = {agent_id: None for agent_id in self.air_agent_ids}
        self.air_controllers: dict[str, dict[str, Any]] = {
            agent_id: {
                "center_point": None,
                "cruise_altitude_y": None,
                "state": "straight",
                "state_started_at_ms": None,
                "state_duration_ms": self._next_straight_duration_ms(),
                "bank_direction": 1.0,
            }
            for agent_id in self.air_agent_ids
        }
        self.agv_route_nodes: list[dict[str, float]] = []
        self.agv_route_edges: list[int] = []
        self.agv_target_index = 0
        self.agv_route_complete = False
        self.last_logged_second = -1

    async def handle_event(self, websocket: WebSocket, event: dict[str, Any]) -> None:
        event_type = event.get("type")

        if event_type == "simulation_ready" and not self.started:
            self.started = True
            await self._handle_simulation_ready(websocket, event)
            return

        if event_type == "agent_spawned":
            agent_id = str(event.get("agentId") or "")
            if agent_id in self.air_agent_ids or agent_id == self.agv_agent_id or agent_id in self.relay_agent_ids:
                print(f"[scenario] spawned {agent_id}")
            return

        if event_type != "agent_tick":
            return

        snapshot = event.get("snapshot") or {}
        world_time_ms = int(snapshot.get("worldTimeMs") or 0)
        agents = {str(agent.get("id")): agent for agent in (snapshot.get("agents") or [])}
        commands: list[dict[str, Any]] = []

        for index, agent_id in enumerate(self.air_agent_ids):
            agent = agents.get(agent_id)
            if not agent:
                continue
            commands.extend(self._air_agent_commands(agent_id, agent, world_time_ms, color=0xff6633 if index == 0 else 0x44dd88))

        agv_agent = agents.get(self.agv_agent_id)
        if agv_agent:
            commands.extend(self._agv_commands(agv_agent))

        if commands:
            await send_command_batch(websocket, commands)

        tick_second = world_time_ms // 1000
        if tick_second != self.last_logged_second:
            self.last_logged_second = tick_second
            self._log_summary(agents)

    async def _handle_simulation_ready(self, websocket: WebSocket, event: dict[str, Any]) -> None:
        objectives = event.get("worldObjectives") or {}
        road_graph = event.get("roadGraph") or {}
        suggested_spawn = event.get("suggestedSpawn") or {"lat": 0.0, "lng": 0.0, "alt": QUADCOPTER_CRUISE_ALT_M}
        self.spawn_altitude_m = float(suggested_spawn.get("alt", QUADCOPTER_CRUISE_ALT_M))
        self.meters_per_unit = float(event.get("metersPerUnit") or 1.0)

        command_center = objectives.get("commandCenter") or {}
        command_world = command_center.get("worldPosition") or {}
        command_geo = command_center.get("geoPosition") or suggested_spawn
        connectivity_zones = objectives.get("connectivityZones") or []
        building_hazards = objectives.get("buildingHazards") or []
        road_hazards = objectives.get("roadHazards") or []

        if not command_world or not command_geo or not connectivity_zones:
            print("[scenario] full_send -> missing command center or connectivity zones")
            return

        fire_hazards = [hazard for hazard in building_hazards if hazard.get("type") == "fire" and hazard.get("worldPosition")]
        help_hazards = [hazard for hazard in building_hazards if hazard.get("type") == "help" and hazard.get("worldPosition")]

        if fire_hazards:
            closest_fire = min(fire_hazards, key=lambda hazard: distance_sq(hazard.get("worldPosition") or {}, command_world))
            self.air_targets[self.air_agent_ids[0]] = _world_point(closest_fire.get("worldPosition") or {})
            print(
                "[scenario] full_send -> "
                f"{self.air_agent_ids[0]} circles closest fire {closest_fire.get('id')} "
                f"at ({self.air_targets[self.air_agent_ids[0]]['x']:.2f}, {self.air_targets[self.air_agent_ids[0]]['z']:.2f})"
            )
        if help_hazards:
            furthest_help = max(help_hazards, key=lambda hazard: distance_sq(hazard.get("worldPosition") or {}, command_world))
            self.air_targets[self.air_agent_ids[1]] = _world_point(furthest_help.get("worldPosition") or {})
            print(
                "[scenario] full_send -> "
                f"{self.air_agent_ids[1]} circles furthest help {furthest_help.get('id')} "
                f"at ({self.air_targets[self.air_agent_ids[1]]['x']:.2f}, {self.air_targets[self.air_agent_ids[1]]['z']:.2f})"
            )

        closest_zone = min(connectivity_zones, key=lambda zone: _distance_m(command_world, zone.get("worldPosition") or {}))
        zone_world = closest_zone.get("worldPosition") or {}
        zone_geo = closest_zone.get("geoPosition") or {}
        zone_distance_m = _distance_m(command_world, zone_world)
        relay_count = _required_quadcopter_count(zone_distance_m)
        relay_target_distances_m = _relay_target_distances(zone_distance_m, relay_count)
        print(
            "[scenario] full_send -> "
            f"closest connectivity zone={closest_zone.get('id')} distance={zone_distance_m:.1f}m "
            f"quads={relay_count}"
        )

        route_help = min(help_hazards, key=lambda hazard: distance_sq(hazard.get("worldPosition") or {}, command_world)) if help_hazards else None
        if route_help is not None:
            self.agv_route_nodes, self.agv_route_edges = _build_agv_route(
                road_graph=road_graph,
                command_center=command_center,
                help_hazard=route_help,
                rubble_hazards=road_hazards,
            )
            if self.agv_route_nodes:
                print(
                    "[scenario] full_send -> "
                    f"agv route to closest help {route_help.get('id')} "
                    f"nodes={len(self.agv_route_nodes)} edges={len(self.agv_route_edges)}"
                )
            else:
                print("[scenario] full_send -> failed to build AGV route")

        commands: list[dict[str, Any]] = []

        for agent_id in self.air_agent_ids:
            commands.append(self._spawn_air_agent_command(agent_id, suggested_spawn))
            target = self.air_targets.get(agent_id)
            if target is not None:
                commands.append({
                    "type": "set_debug_marker",
                    "markerId": f"poi-{agent_id}",
                    "position": target,
                    "color": 0xff6633 if agent_id == self.air_agent_ids[0] else 0x44dd88,
                    "radius": 2.2,
                })

        for index, target_distance_m in enumerate(relay_target_distances_m, start=1):
            fraction = 0.0 if zone_distance_m <= 1e-6 else min(1.0, max(0.0, target_distance_m / zone_distance_m))
            target_world = _interpolate_world(command_world, zone_world, fraction)
            target_geo = _interpolate_geo(command_geo, zone_geo, fraction, self.spawn_altitude_m)
            agent_id = f"full-send-relay-{index}"
            self.relay_agent_ids.append(agent_id)
            commands.append(
                {
                    "type": "spawn_agent",
                    "agentId": agent_id,
                    "modality": "quadcopter",
                    "start": {
                        "lat": float(command_geo.get("lat", 0.0)),
                        "lng": float(command_geo.get("lng", 0.0)),
                        "alt": self.spawn_altitude_m,
                    },
                    "waypoints": [target_geo],
                    "sensors": [],
                }
            )
            commands.append(
                {
                    "type": "set_debug_marker",
                    "markerId": f"relay-target-{index}",
                    "position": target_world,
                    "color": 0x33ccff if index < relay_count else 0x00ff88,
                    "radius": 2.0,
                }
            )

        commands.append(
            {
                "type": "spawn_agent",
                "agentId": self.agv_agent_id,
                "modality": "agv",
                "start": {
                    "lat": float(command_geo.get("lat", 0.0)),
                    "lng": float(command_geo.get("lng", 0.0)),
                    "alt": 0.0,
                },
                "headingDeg": 0.0,
                "sensors": [],
            }
        )

        for index, waypoint in enumerate(self.agv_route_nodes[:12]):
            commands.append(
                {
                    "type": "set_debug_marker",
                    "markerId": f"agv-route-{index}",
                    "position": waypoint,
                    "color": 0xffdd33 if index == 0 else 0xffaa33,
                    "radius": 1.4 if index == 0 else 1.0,
                }
            )

        await send_command_batch(websocket, commands)

    def _spawn_air_agent_command(self, agent_id: str, start: dict[str, float]) -> dict[str, Any]:
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

    def _air_agent_commands(
        self,
        agent_id: str,
        agent: dict[str, Any],
        world_time_ms: int,
        color: int,
    ) -> list[dict[str, Any]]:
        controller = self.air_controllers[agent_id]
        target = self.air_targets.get(agent_id)
        if target is None:
            return []

        if controller["center_point"] is None:
            controller["center_point"] = dict(target)
            print(f"[scenario] {agent_id} center fixed at ({target['x']:.2f}, {target['z']:.2f})")
        if controller["cruise_altitude_y"] is None:
            units_per_meter = float(agent["position"]["y"]) / max(self.spawn_altitude_m, 1.0)
            controller["cruise_altitude_y"] = float(agent["position"]["y"]) - 50.0 * units_per_meter
            print(f"[scenario] {agent_id} cruise altitude y={controller['cruise_altitude_y']:.2f}")
        if controller["state_started_at_ms"] is None:
            controller["state_started_at_ms"] = world_time_ms
            print(f"[scenario] {agent_id} enter {controller['state']} for {controller['state_duration_ms'] / 1000:.1f}s")
        if world_time_ms - controller["state_started_at_ms"] >= controller["state_duration_ms"]:
            self._advance_air_agent_state(agent_id, agent, world_time_ms)

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
            pan_deg, tilt_deg = compute_camera_orientation(agent, target)

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
            {
                "type": "set_debug_marker",
                "markerId": f"poi-{agent_id}",
                "position": target,
                "color": color,
                "radius": 2.2,
            },
        ]

    def _advance_air_agent_state(self, agent_id: str, agent: dict[str, Any], world_time_ms: int) -> None:
        controller = self.air_controllers[agent_id]
        if controller["state"] == "straight":
            controller["state"] = "banking"
            controller["bank_direction"] = self._bank_direction_toward_target(agent_id, agent)
            controller["state_duration_ms"] = self._next_bank_duration_ms()
            direction_label = "right" if float(controller["bank_direction"]) > 0 else "left"
            print(f"[scenario] {agent_id} enter banking {direction_label} for {controller['state_duration_ms'] / 1000:.1f}s")
        else:
            controller["state"] = "straight"
            controller["state_duration_ms"] = self._next_straight_duration_ms()
            print(f"[scenario] {agent_id} enter straight for {controller['state_duration_ms'] / 1000:.1f}s")
        controller["state_started_at_ms"] = world_time_ms

    def _bank_direction_toward_target(self, agent_id: str, agent: dict[str, Any]) -> float:
        center_point = self.air_controllers[agent_id]["center_point"]
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

    def _agv_commands(self, agent: dict[str, Any]) -> list[dict[str, Any]]:
        if self.agv_route_complete or not self.agv_route_nodes:
            return [{"type": "set_agv_controls", "agentId": self.agv_agent_id, "throttle": 0.0, "steering": 0.0, "brake": True}]

        threshold_units = AGV_ARRIVAL_RADIUS_M / max(self.meters_per_unit, 1e-6)
        while self.agv_target_index < len(self.agv_route_nodes):
            target = self.agv_route_nodes[self.agv_target_index]
            if _distance_units(agent.get("position") or {}, target) > threshold_units:
                break
            self.agv_target_index += 1

        if self.agv_target_index >= len(self.agv_route_nodes):
            self.agv_route_complete = True
            print("[scenario] full_send -> AGV route complete")
            return [{"type": "set_agv_controls", "agentId": self.agv_agent_id, "throttle": 0.0, "steering": 0.0, "brake": True}]

        target = self.agv_route_nodes[self.agv_target_index]
        position = agent.get("position") or {}
        heading_deg = float((agent.get("orientation") or {}).get("headingDeg", 0.0))
        dx = float(target.get("x", 0.0)) - float(position.get("x", 0.0))
        dz = float(target.get("z", 0.0)) - float(position.get("z", 0.0))
        target_heading_deg = math.degrees(math.atan2(dx, dz))
        heading_error_deg = _normalize_angle_deg(target_heading_deg - heading_deg)
        steering = clamp(heading_error_deg / 45.0, -1.0, 1.0)
        distance_units = math.hypot(dx, dz)
        slow_radius_units = AGV_SLOW_RADIUS_M / max(self.meters_per_unit, 1e-6)

        if abs(heading_error_deg) > 50.0:
            throttle = 0.0
            brake = False
        else:
            throttle = 0.65 if distance_units > slow_radius_units else 0.25
            brake = False

        return [
            {
                "type": "set_agv_controls",
                "agentId": self.agv_agent_id,
                "throttle": throttle,
                "steering": steering,
                "brake": brake,
            },
            {
                "type": "set_debug_marker",
                "markerId": "agv-target",
                "position": target,
                "color": 0xffee55,
                "radius": 1.6,
            },
        ]

    def _log_summary(self, agents: dict[str, dict[str, Any]]) -> None:
        summary: list[str] = []
        for agent_id in self.air_agent_ids:
            agent = agents.get(agent_id)
            if not agent:
                continue
            controller = self.air_controllers[agent_id]
            position = agent["position"]
            orientation = agent["orientation"]
            summary.append(
                f"{agent_id}:{controller['state']} "
                f"pos=({float(position['x']):.1f},{float(position['z']):.1f}) "
                f"hdg={float(orientation['headingDeg']):.1f}"
            )

        agv_agent = agents.get(self.agv_agent_id)
        if agv_agent:
            position = agv_agent["position"]
            orientation = agv_agent["orientation"]
            summary.append(
                f"{self.agv_agent_id}:route "
                f"idx={self.agv_target_index}/{len(self.agv_route_nodes)} "
                f"pos=({float(position['x']):.1f},{float(position['z']):.1f}) "
                f"hdg={float(orientation['headingDeg']):.1f}"
            )

        if self.relay_agent_ids:
            summary.append(f"relays={len([agent_id for agent_id in self.relay_agent_ids if agent_id in agents])}")

        if summary:
            print("[scenario] " + " | ".join(summary))

    def _next_straight_duration_ms(self) -> int:
        return 4000

    def _next_bank_duration_ms(self) -> int:
        return 6000


def _required_quadcopter_count(distance_m: float) -> int:
    remaining_m = max(0.0, distance_m - CONNECTIVITY_COMMAND_LINK_M - CONNECTIVITY_SERVICE_RADIUS_M)
    return 1 + math.ceil(remaining_m / CONNECTIVITY_RELAY_LINK_M)


def _relay_target_distances(distance_m: float, quad_count: int) -> list[float]:
    if quad_count <= 1:
        return [max(5.0, min(CONNECTIVITY_COMMAND_LINK_M, max(5.0, distance_m - CONNECTIVITY_SERVICE_RADIUS_M)))]

    distances = [CONNECTIVITY_COMMAND_LINK_M + CONNECTIVITY_RELAY_LINK_M * index for index in range(quad_count - 1)]
    distances.append(max(5.0, distance_m - CONNECTIVITY_SERVICE_RADIUS_M))
    return distances


def _build_agv_route(
    *,
    road_graph: dict[str, Any],
    command_center: dict[str, Any],
    help_hazard: dict[str, Any],
    rubble_hazards: list[dict[str, Any]],
) -> tuple[list[dict[str, float]], list[int]]:
    nodes = road_graph.get("nodes") or []
    edges = road_graph.get("edges") or []
    if not nodes or not edges:
        return [], []

    blocked_edge_ids = {int(hazard.get("edgeId")) for hazard in rubble_hazards if hazard.get("edgeId") is not None}
    start_edge_id = int(command_center.get("edgeId") or 0)
    target_position = help_hazard.get("worldPosition") or {}
    target_edge = _find_nearest_edge(edges, target_position, blocked_edge_ids)
    if target_edge is None:
        return [], []

    path_edge_ids = _find_shortest_path_edges(edges, nodes, start_edge_id, int(target_edge["id"]), blocked_edge_ids)
    if not path_edge_ids:
        return [], []

    route_nodes = _edge_path_to_node_waypoints(nodes, edges, start_edge_id, path_edge_ids)
    if not route_nodes:
        return [], path_edge_ids

    route_nodes.append(_world_point(target_position))
    return route_nodes, path_edge_ids


def _find_nearest_edge(
    edges: list[dict[str, Any]],
    position: dict[str, Any],
    blocked_edge_ids: set[int],
) -> dict[str, Any] | None:
    nearest: dict[str, Any] | None = None
    nearest_distance_sq = math.inf
    px = float(position.get("x", 0.0))
    pz = float(position.get("z", 0.0))
    for edge in edges:
        edge_id = int(edge.get("id", -1))
        if edge_id in blocked_edge_ids:
            continue
        start = edge.get("start") or {}
        end = edge.get("end") or {}
        candidate = _nearest_point_on_segment(
            float(start.get("x", 0.0)),
            float(start.get("z", 0.0)),
            float(end.get("x", 0.0)),
            float(end.get("z", 0.0)),
            px,
            pz,
        )
        dx = candidate["x"] - px
        dz = candidate["z"] - pz
        candidate_distance_sq = dx * dx + dz * dz
        if candidate_distance_sq >= nearest_distance_sq:
            continue
        nearest_distance_sq = candidate_distance_sq
        nearest = edge
    return nearest


def _find_shortest_path_edges(
    edges: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    start_edge_id: int,
    target_edge_id: int,
    blocked_edge_ids: set[int],
) -> list[int] | None:
    if start_edge_id in blocked_edge_ids or target_edge_id in blocked_edge_ids:
        return None

    edge_by_id = {int(edge["id"]): edge for edge in edges}
    node_edges: dict[int, list[int]] = {int(node["id"]): [int(edge_id) for edge_id in (node.get("edges") or [])] for node in nodes}
    start_edge = edge_by_id.get(start_edge_id)
    target_edge = edge_by_id.get(target_edge_id)
    if start_edge is None or target_edge is None:
        return None

    candidate_paths = [
        _find_shortest_path_nodes(edges, node_edges, int(start_edge["startNodeId"]), int(target_edge["startNodeId"]), blocked_edge_ids),
        _find_shortest_path_nodes(edges, node_edges, int(start_edge["startNodeId"]), int(target_edge["endNodeId"]), blocked_edge_ids),
        _find_shortest_path_nodes(edges, node_edges, int(start_edge["endNodeId"]), int(target_edge["startNodeId"]), blocked_edge_ids),
        _find_shortest_path_nodes(edges, node_edges, int(start_edge["endNodeId"]), int(target_edge["endNodeId"]), blocked_edge_ids),
    ]
    candidate_paths = [candidate for candidate in candidate_paths if candidate is not None]
    if not candidate_paths:
        return None

    best_path = candidate_paths[0]
    best_length = _path_length(edge_by_id, best_path)
    for candidate in candidate_paths[1:]:
        candidate_length = _path_length(edge_by_id, candidate)
        if candidate_length < best_length:
            best_path = candidate
            best_length = candidate_length
    return best_path


def _find_shortest_path_nodes(
    edges: list[dict[str, Any]],
    node_edges: dict[int, list[int]],
    start_node_id: int,
    target_node_id: int,
    blocked_edge_ids: set[int],
) -> list[int] | None:
    edge_by_id = {int(edge["id"]): edge for edge in edges}
    distances = {node_id: math.inf for node_id in node_edges}
    previous_node = {node_id: -1 for node_id in node_edges}
    previous_edge = {node_id: -1 for node_id in node_edges}
    visited: set[int] = set()
    distances[start_node_id] = 0.0

    while True:
        current_node_id = -1
        current_distance = math.inf
        for node_id, distance in distances.items():
            if node_id in visited:
                continue
            if distance >= current_distance:
                continue
            current_distance = distance
            current_node_id = node_id

        if current_node_id == -1 or current_node_id == target_node_id:
            break
        visited.add(current_node_id)

        for edge_id in node_edges.get(current_node_id, []):
            if edge_id in blocked_edge_ids:
                continue
            edge = edge_by_id[edge_id]
            next_node_id = int(edge["endNodeId"]) if int(edge["startNodeId"]) == current_node_id else int(edge["startNodeId"])
            if next_node_id in visited:
                continue
            candidate_distance = distances[current_node_id] + float(edge.get("length", 0.0))
            if candidate_distance >= distances.get(next_node_id, math.inf):
                continue
            distances[next_node_id] = candidate_distance
            previous_node[next_node_id] = current_node_id
            previous_edge[next_node_id] = edge_id

    if not math.isfinite(distances.get(target_node_id, math.inf)):
        return None

    path: list[int] = []
    current_node_id = target_node_id
    while current_node_id != start_node_id:
        edge_id = previous_edge.get(current_node_id, -1)
        if edge_id == -1:
            return None
        path.append(edge_id)
        current_node_id = previous_node[current_node_id]

    path.reverse()
    return path


def _edge_path_to_node_waypoints(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    start_edge_id: int,
    path_edge_ids: list[int],
) -> list[dict[str, float]]:
    node_by_id = {int(node["id"]): node for node in nodes}
    edge_by_id = {int(edge["id"]): edge for edge in edges}
    start_edge = edge_by_id.get(start_edge_id)
    if start_edge is None or not path_edge_ids:
        return []

    first_edge = edge_by_id[path_edge_ids[0]]
    candidate_start_nodes = [int(start_edge["startNodeId"]), int(start_edge["endNodeId"])]
    current_node_id = next(
        (node_id for node_id in candidate_start_nodes if node_id in (int(first_edge["startNodeId"]), int(first_edge["endNodeId"]))),
        candidate_start_nodes[0],
    )

    route_nodes: list[dict[str, float]] = []
    for edge_id in path_edge_ids:
        edge = edge_by_id[edge_id]
        next_node_id = int(edge["endNodeId"]) if int(edge["startNodeId"]) == current_node_id else int(edge["startNodeId"])
        next_node = node_by_id.get(next_node_id)
        if next_node is not None:
            route_nodes.append({"x": float(next_node["x"]), "y": 0.0, "z": float(next_node["z"])})
        current_node_id = next_node_id
    return route_nodes


def _path_length(edge_by_id: dict[int, dict[str, Any]], path_edge_ids: list[int]) -> float:
    return sum(float(edge_by_id[edge_id].get("length", 0.0)) for edge_id in path_edge_ids)


def _distance_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    dx = float(a.get("x", 0.0)) - float(b.get("x", 0.0))
    dy = float(a.get("y", 0.0)) - float(b.get("y", 0.0))
    dz = float(a.get("z", 0.0)) - float(b.get("z", 0.0))
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _distance_units(a: dict[str, Any], b: dict[str, Any]) -> float:
    dx = float(a.get("x", 0.0)) - float(b.get("x", 0.0))
    dz = float(a.get("z", 0.0)) - float(b.get("z", 0.0))
    return math.hypot(dx, dz)


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


def _nearest_point_on_segment(ax: float, az: float, bx: float, bz: float, px: float, pz: float) -> dict[str, float]:
    delta_x = bx - ax
    delta_z = bz - az
    length_sq = delta_x * delta_x + delta_z * delta_z
    if length_sq <= 1e-8:
        return {"x": ax, "z": az}
    t = max(0.0, min(1.0, ((px - ax) * delta_x + (pz - az) * delta_z) / length_sq))
    return {
        "x": ax + delta_x * t,
        "z": az + delta_z * t,
    }


def _normalize_angle_deg(value: float) -> float:
    normalized = (value + 180.0) % 360.0 - 180.0
    return normalized if normalized != -180.0 else 180.0


def _world_point(position: dict[str, Any]) -> dict[str, float]:
    return {
        "x": float(position.get("x", 0.0)),
        "y": float(position.get("y", 0.0)),
        "z": float(position.get("z", 0.0)),
    }


def _lerp(start: float, end: float, fraction: float) -> float:
    return start + (end - start) * fraction
