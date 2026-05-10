import asyncio
from typing import Any

from fastapi import WebSocket

from .common import Scenario, offset_geo, send_scoring_report


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
            for hazard in [*((objectives.get("buildingHazards") or [])), *((objectives.get("roadHazards") or []))]
            if hazard.get("geoPosition")
        ]
        if not hazards:
            return []

        primary = hazards[0]
        primary_type = str(primary.get("type") or "unknown")
        correct_label = primary_type if primary_type in {"fire", "help", "debris"} else "unknown"
        incorrect_label = next(candidate for candidate in ("fire", "help", "debris") if candidate != correct_label)
        far_location = offset_geo(primary.get("geoPosition") or {}, lat_offset_deg=0.01, lng_offset_deg=0.01)

        return [
            self._probe_case("correct_hazard_unknown_label", primary, "unknown", "expect hazard_identified_unknown"),
            self._probe_case("correct_hazard_incorrect_label", primary, incorrect_label, "expect hazard_identified_wrong_type"),
            self._probe_case("correct_hazard_correct_label", primary, correct_label, "expect hazard_identified_correct"),
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

    def _probe_case(self, name: str, hazard: dict[str, Any], hazard_type: str, expectation: str) -> dict[str, Any]:
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
                print(f"[scenario] scoring_probe {index}/{len(probes)} -> {probe['name']} ({probe['expectation']})")
                await send_scoring_report(websocket, probe["report"])
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
