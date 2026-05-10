from .common import Scenario, summarize_payload
from .connectivity_probe import ConnectivityProbeScenario
from .full_send import FullSendScenario
from .fixed_wing_dual import FixedWingDualExploreScenario, PyromaniacScenario
from .fixed_wing_explore import FixedWingExploreScenario
from .scoring_probe import ScoringProbeScenario

SCENARIOS: dict[str, type[Scenario]] = {
    "connectivity_probe": ConnectivityProbeScenario,
    "fixed_wing_circle_track": FixedWingExploreScenario,
    "fixed_wing_explore": FixedWingExploreScenario,
    "fixed_wing_dual_explore": FixedWingDualExploreScenario,
    "full_send": FullSendScenario,
    "pyromaniac": PyromaniacScenario,
    "scoring_probe": ScoringProbeScenario,
}

__all__ = [
    "SCENARIOS",
    "Scenario",
    "summarize_payload",
    "ConnectivityProbeScenario",
    "FullSendScenario",
    "FixedWingExploreScenario",
    "FixedWingDualExploreScenario",
    "PyromaniacScenario",
    "ScoringProbeScenario",
]
