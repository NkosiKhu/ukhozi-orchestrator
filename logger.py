import asyncio
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SimulationEventLogger:
    def __init__(
        self,
        scenario_name: str,
        db_path: str | None = None,
        *,
        batch_size: int = 128,
        flush_interval_s: float = 0.5,
    ) -> None:
        default_path = Path(__file__).with_name("sim_logs.sqlite3")
        self.db_path = str(default_path if db_path is None else Path(db_path))
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()
        self.session_id = self._create_session(scenario_name)
        self.meters_per_unit = 1.0
        self.fire_hazards: list[dict[str, Any]] = []
        self.latest_agents: dict[str, dict[str, Any]] = {}
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_s
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.worker_task is None:
            self.worker_task = asyncio.create_task(self._run_worker())

    async def close(self) -> None:
        await self.queue.put(None)
        if self.worker_task is not None:
            await self.worker_task
        self.connection.close()

    def handle_event(self, event: dict[str, Any]) -> None:
        self.queue.put_nowait(event)

    def handle_score_update(self, update: dict[str, Any]) -> None:
        event = update.get("event") or {}
        self.connection.execute(
            """
            INSERT INTO score_updates (
              session_id, world_time_ms, reason, points_delta, total_points,
              hazard_id, zone_id, agent_id, received_at_utc, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.session_id,
                int(event["worldTimeMs"]) if event.get("worldTimeMs") is not None else None,
                str(event.get("reason") or "unknown"),
                float(event.get("pointsDelta") or 0.0),
                float(update.get("totalPoints") or 0.0),
                event.get("hazardId"),
                event.get("zoneId"),
                event.get("agentId"),
                _utc_now_iso(),
                json.dumps(update),
            ),
        )
        self.connection.commit()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scenario_name TEXT NOT NULL,
              started_at_utc TEXT NOT NULL,
              meters_per_unit REAL,
              objectives_json TEXT
            );

            CREATE TABLE IF NOT EXISTS raw_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id INTEGER NOT NULL,
              event_type TEXT NOT NULL,
              world_time_ms INTEGER,
              received_at_utc TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id)
            );

            CREATE TABLE IF NOT EXISTS agent_ticks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id INTEGER NOT NULL,
              world_time_ms INTEGER NOT NULL,
              agent_id TEXT NOT NULL,
              modality TEXT,
              position_x REAL,
              position_y REAL,
              position_z REAL,
              forward_x REAL,
              forward_y REAL,
              forward_z REAL,
              heading_deg REAL,
              pitch_deg REAL,
              roll_deg REAL,
              speed REAL,
              is_banking INTEGER NOT NULL,
              bank_angle_deg REAL NOT NULL,
              nearest_fire_id TEXT,
              nearest_fire_distance_m REAL,
              near_fire INTEGER NOT NULL,
              banking_near_fire INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id)
            );

            CREATE TABLE IF NOT EXISTS sensor_captures (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id INTEGER NOT NULL,
              world_time_ms INTEGER,
              agent_id TEXT,
              sensor_id TEXT,
              sensor_type TEXT,
              sensor_pan_deg REAL,
              sensor_tilt_deg REAL,
              heading_deg REAL,
              pitch_deg REAL,
              roll_deg REAL,
              is_banking INTEGER NOT NULL,
              bank_angle_deg REAL NOT NULL,
              nearest_fire_id TEXT,
              nearest_fire_distance_m REAL,
              near_fire INTEGER NOT NULL,
              banking_near_fire INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id)
            );

            CREATE TABLE IF NOT EXISTS score_updates (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id INTEGER NOT NULL,
              world_time_ms INTEGER,
              reason TEXT NOT NULL,
              points_delta REAL NOT NULL,
              total_points REAL NOT NULL,
              hazard_id TEXT,
              zone_id TEXT,
              agent_id TEXT,
              received_at_utc TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_agent_ticks_time ON agent_ticks(session_id, world_time_ms);
            CREATE INDEX IF NOT EXISTS idx_agent_ticks_fire ON agent_ticks(session_id, near_fire, banking_near_fire);
            CREATE INDEX IF NOT EXISTS idx_sensor_captures_time ON sensor_captures(session_id, world_time_ms);
            CREATE INDEX IF NOT EXISTS idx_sensor_captures_fire ON sensor_captures(session_id, near_fire, banking_near_fire);
            CREATE INDEX IF NOT EXISTS idx_score_updates_time ON score_updates(session_id, world_time_ms);
            CREATE INDEX IF NOT EXISTS idx_score_updates_reason ON score_updates(session_id, reason);
            """
        )
        self.connection.commit()

    def _create_session(self, scenario_name: str) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO sessions (scenario_name, started_at_utc)
            VALUES (?, ?)
            """,
            (scenario_name, _utc_now_iso()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    async def _run_worker(self) -> None:
        pending: list[dict[str, Any]] = []
        while True:
            item = await self.queue.get()
            if item is None:
                break
            pending.append(item)
            deadline = asyncio.get_running_loop().time() + self.flush_interval_s
            while len(pending) < self.batch_size:
                timeout = deadline - asyncio.get_running_loop().time()
                if timeout <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout)
                except asyncio.TimeoutError:
                    break
                if item is None:
                    await self._flush_batch(pending)
                    return
                pending.append(item)
            await self._flush_batch(pending)
            pending = []
        if pending:
            await self._flush_batch(pending)

    async def _flush_batch(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        self.connection.execute("BEGIN")
        try:
            for event in events:
                self._process_event(event)
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _process_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        self._insert_raw_event(event_type or "unknown", event)
        if event_type == "simulation_ready":
            self._handle_simulation_ready(event)
        elif event_type == "agent_tick":
            self._handle_agent_tick(event)
        elif event_type == "sensor_capture":
            self._handle_sensor_capture(event)

    def _insert_raw_event(self, event_type: str, event: dict[str, Any]) -> None:
        world_time_ms = event.get("worldTimeMs")
        if event_type == "agent_tick":
            snapshot = event.get("snapshot") or {}
            world_time_ms = snapshot.get("worldTimeMs")
        self.connection.execute(
            """
            INSERT INTO raw_events (session_id, event_type, world_time_ms, received_at_utc, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                self.session_id,
                event_type,
                int(world_time_ms) if world_time_ms is not None else None,
                _utc_now_iso(),
                json.dumps(event),
            ),
        )

    def _handle_simulation_ready(self, event: dict[str, Any]) -> None:
        objectives = event.get("worldObjectives") or {}
        self.meters_per_unit = float(event.get("metersPerUnit") or 1.0)
        self.fire_hazards = [
            hazard for hazard in (objectives.get("buildingHazards") or [])
            if hazard.get("type") == "fire"
        ]
        self.connection.execute(
            """
            UPDATE sessions
            SET meters_per_unit = ?, objectives_json = ?
            WHERE id = ?
            """,
            (self.meters_per_unit, json.dumps(objectives), self.session_id),
        )

    def _handle_agent_tick(self, event: dict[str, Any]) -> None:
        snapshot = event.get("snapshot") or {}
        world_time_ms = int(snapshot.get("worldTimeMs") or 0)
        rows: list[tuple[Any, ...]] = []
        for agent in snapshot.get("agents") or []:
            derived = self._derive_agent_tags(agent)
            agent_id = str(agent.get("id") or "")
            self.latest_agents[agent_id] = {
                "heading_deg": derived["heading_deg"],
                "pitch_deg": derived["pitch_deg"],
                "roll_deg": derived["roll_deg"],
                "is_banking": derived["is_banking"],
                "bank_angle_deg": derived["bank_angle_deg"],
                "nearest_fire_id": derived["nearest_fire_id"],
                "nearest_fire_distance_m": derived["nearest_fire_distance_m"],
                "near_fire": derived["near_fire"],
                "banking_near_fire": derived["banking_near_fire"],
            }
            rows.append(
                (
                    self.session_id,
                    world_time_ms,
                    agent_id,
                    agent.get("modality"),
                    derived["position_x"],
                    derived["position_y"],
                    derived["position_z"],
                    derived["forward_x"],
                    derived["forward_y"],
                    derived["forward_z"],
                    derived["heading_deg"],
                    derived["pitch_deg"],
                    derived["roll_deg"],
                    derived["speed"],
                    1 if derived["is_banking"] else 0,
                    derived["bank_angle_deg"],
                    derived["nearest_fire_id"],
                    derived["nearest_fire_distance_m"],
                    1 if derived["near_fire"] else 0,
                    1 if derived["banking_near_fire"] else 0,
                    json.dumps(agent),
                )
            )
        if rows:
            self.connection.executemany(
                """
                INSERT INTO agent_ticks (
                  session_id, world_time_ms, agent_id, modality,
                  position_x, position_y, position_z,
                  forward_x, forward_y, forward_z,
                  heading_deg, pitch_deg, roll_deg, speed,
                  is_banking, bank_angle_deg,
                  nearest_fire_id, nearest_fire_distance_m,
                  near_fire, banking_near_fire, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _handle_sensor_capture(self, event: dict[str, Any]) -> None:
        agent_id = str(event.get("agentId") or "")
        latest = self.latest_agents.get(agent_id, {})
        orientation = event.get("orientation") or {}
        self.connection.execute(
            """
            INSERT INTO sensor_captures (
              session_id, world_time_ms, agent_id, sensor_id, sensor_type,
              sensor_pan_deg, sensor_tilt_deg,
              heading_deg, pitch_deg, roll_deg,
              is_banking, bank_angle_deg,
              nearest_fire_id, nearest_fire_distance_m,
              near_fire, banking_near_fire, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.session_id,
                int(event.get("worldTimeMs") or 0),
                agent_id,
                event.get("sensorId"),
                event.get("sensorType"),
                float(orientation.get("panDeg", 0.0)),
                float(orientation.get("tiltDeg", 0.0)),
                float(latest.get("heading_deg", 0.0)),
                float(latest.get("pitch_deg", 0.0)),
                float(latest.get("roll_deg", 0.0)),
                1 if bool(latest.get("is_banking")) else 0,
                float(latest.get("bank_angle_deg", 0.0)),
                latest.get("nearest_fire_id"),
                latest.get("nearest_fire_distance_m"),
                1 if bool(latest.get("near_fire")) else 0,
                1 if bool(latest.get("banking_near_fire")) else 0,
                json.dumps(event.get("payload") or {}),
            ),
        )

    def _derive_agent_tags(self, agent: dict[str, Any]) -> dict[str, Any]:
        position = agent.get("position") or {}
        forward = agent.get("forward") or {}
        orientation = agent.get("orientation") or {}
        roll_deg = float(orientation.get("rollDeg", 0.0))
        nearest_fire_id = None
        nearest_fire_distance_m = None
        near_fire = False

        for hazard in self.fire_hazards:
            world_position = hazard.get("worldPosition") or {}
            dx = float(world_position.get("x", 0.0)) - float(position.get("x", 0.0))
            dy = float(world_position.get("y", 0.0)) - float(position.get("y", 0.0))
            dz = float(world_position.get("z", 0.0)) - float(position.get("z", 0.0))
            distance_m = math.sqrt(dx * dx + dy * dy + dz * dz) * self.meters_per_unit
            if nearest_fire_distance_m is None or distance_m < nearest_fire_distance_m:
                nearest_fire_distance_m = distance_m
                nearest_fire_id = hazard.get("id")
                radius_m = float(hazard.get("radiusM") or 0.0)
                near_fire = distance_m <= max(radius_m, 30.0)

        is_banking = abs(roll_deg) >= 12.0
        return {
            "position_x": float(position.get("x", 0.0)),
            "position_y": float(position.get("y", 0.0)),
            "position_z": float(position.get("z", 0.0)),
            "forward_x": float(forward.get("x", 0.0)),
            "forward_y": float(forward.get("y", 0.0)),
            "forward_z": float(forward.get("z", 0.0)),
            "heading_deg": float(orientation.get("headingDeg", 0.0)),
            "pitch_deg": float(orientation.get("pitchDeg", 0.0)),
            "roll_deg": roll_deg,
            "speed": float(agent.get("speed", 0.0)),
            "is_banking": is_banking,
            "bank_angle_deg": abs(roll_deg),
            "nearest_fire_id": nearest_fire_id,
            "nearest_fire_distance_m": nearest_fire_distance_m,
            "near_fire": near_fire,
            "banking_near_fire": is_banking and near_fire,
        }
