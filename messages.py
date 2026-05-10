from typing import Any, Literal, TypedDict


class SimulationEventEnvelope(TypedDict):
    kind: Literal["simulation_event"]
    event: dict[str, Any]


class SimulationCommandEnvelope(TypedDict):
    kind: Literal["simulation_command"]
    command: dict[str, Any]


class SimulationCommandBatchEnvelope(TypedDict):
    kind: Literal["simulation_command_batch"]
    commands: list[dict[str, Any]]


class ScoreUpdateEnvelope(TypedDict):
    kind: Literal["score_update"]
    update: dict[str, Any]
