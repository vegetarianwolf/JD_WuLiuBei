"""容量二、开放式物流无人机取送货调度算法。"""

from .io import load_tasks_csv
from .exact import solve_exact
from .alns import (
    ALNSConfig,
    construct_edd_adjacent,
    construct_greedy_initial,
    construct_nearest_adjacent,
    construct_regret_initial,
    destroy_solution,
    solve_alns,
    solve_alns_core,
)
from .model import Point, Problem, Score, SolutionEvaluation, Task
from .interaction_graph import (
    PairInteraction,
    TaskInteractionGraph,
    build_interaction_graph,
    clear_interaction_graph_cache,
    evaluate_task_pair,
    rank_interaction_tasks,
)
from .hypergraph_destroy import ConflictHyperedge, hypergraph_destroy
from .location_allocation import (
    LocationAllocationResult,
    solve_location_allocation,
)
from .pair_repair import (
    PairInsertionOption,
    enumerate_pair_orders,
    pair_insertion_options,
    pair_regret_repair,
)
from .propagation_eval import (
    InsertionPropagationEvaluation,
    RoutePropagationEvaluation,
    evaluate_insertion_propagation,
    evaluate_route_propagation,
)
from .relay_model import (
    NUM_RELAY_STATIONS,
    RELAY_ENDPOINT_EPS,
    RELAY_HANDLING_TIME_MIN,
    RelaySolution,
    RelayStation,
    RelayTransfer,
    relay_solution_from_routes,
)
from .relay_search import (
    RelaySearchConfig,
    RelaySolverResult,
    multi_mode_regret2_constructor,
    relay_route_adjustment,
    solve_relay,
)
from .relay_validation import (
    RelaySolutionEvaluation,
    RelayTransferTiming,
    evaluate_relay_solution,
    relay_gap_completion_times,
)
from .search import InsertionResult, SolverResult, insert_task_best
from .swap_model import SwapEvent, SwapSolution, swap_solution_from_routes
from .swap_search import (
    SwapConfig,
    SwapSolverResult,
    build_swap_candidate,
    pickup_owner,
    solve_swap,
    solve_swap_stage2,
)
from .swap_validation import (
    SwapEventTiming,
    SwapSolutionEvaluation,
    evaluate_swap_solution,
    gap_completion_times,
)
from .validation import evaluate_solution

__all__ = [
    "Point",
    "ALNSConfig",
    "ConflictHyperedge",
    "Problem",
    "Score",
    "SolutionEvaluation",
    "SolverResult",
    "InsertionResult",
    "InsertionPropagationEvaluation",
    "LocationAllocationResult",
    "NUM_RELAY_STATIONS",
    "PairInteraction",
    "PairInsertionOption",
    "RELAY_ENDPOINT_EPS",
    "RELAY_HANDLING_TIME_MIN",
    "RelaySearchConfig",
    "RelaySolution",
    "RelaySolutionEvaluation",
    "RelaySolverResult",
    "RelayStation",
    "RelayTransfer",
    "RelayTransferTiming",
    "RoutePropagationEvaluation",
    "Task",
    "TaskInteractionGraph",
    "SwapConfig",
    "SwapEvent",
    "SwapEventTiming",
    "SwapSolution",
    "SwapSolutionEvaluation",
    "SwapSolverResult",
    "build_interaction_graph",
    "build_swap_candidate",
    "clear_interaction_graph_cache",
    "construct_regret_initial",
    "construct_edd_adjacent",
    "evaluate_relay_solution",
    "multi_mode_regret2_constructor",
    "relay_gap_completion_times",
    "relay_route_adjustment",
    "relay_solution_from_routes",
    "solve_location_allocation",
    "solve_relay",
    "construct_greedy_initial",
    "construct_nearest_adjacent",
    "destroy_solution",
    "evaluate_insertion_propagation",
    "evaluate_route_propagation",
    "evaluate_solution",
    "evaluate_task_pair",
    "evaluate_swap_solution",
    "enumerate_pair_orders",
    "gap_completion_times",
    "hypergraph_destroy",
    "insert_task_best",
    "load_tasks_csv",
    "pair_insertion_options",
    "pair_regret_repair",
    "pickup_owner",
    "rank_interaction_tasks",
    "solve_exact",
    "solve_alns",
    "solve_alns_core",
    "solve_swap",
    "solve_swap_stage2",
    "swap_solution_from_routes",
]
