from typing import Any, Literal, TypedDict


class SimulationEventEnvelope(TypedDict):
    kind: Literal["simulation_event"]
    event: dict[str, Any]


class SimulationCommandEnvelope(TypedDict):
    kind: Literal["simulation_command"]
    command: dict[str, Any]

