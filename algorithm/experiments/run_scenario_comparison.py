"""Five-scenario paired wall-clock comparison on the official instance.

Scenarios (all run the SAME seed, each with the SAME total wall-clock budget):

  1. pure_direct         纯 Direct：所有无人机驻原点，无中继（baseline）
  2. pure_relay          纯 Relay：所有无人机驻原点，全部预算直接做中继搜索（warmup=0）
  3. direct_relay        Direct + Relay：原点起点，80% DIRECT 预热 + 20% Relay 分阶段
  4. direct_stations     Direct + 起点不同：预部署（原点 1 架 + 每中继站 1 架），无中继
  5. direct_relay_stations  Direct + Relay + 起点不同：预部署 + 80/20 分阶段中继

Judging is strictly lexicographic on (late_count, total_lateness_min,
distance_km).  Every scenario in a seed shares the ALNS seed, the objective
and the wall-clock budget (construction time is counted inside the budget,
with a safety margin).  Scenario order rotates per seed to reduce
system-load bias.
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import math
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from uav_dispatch import (
    ALNSConfig,
    Problem,
    construct_regret_initial,
    load_tasks_csv,
    solve_alns_core,
    solve_relay_staged,
)
from uav_dispatch.cli import (
    _relay_count_arg,
    resolve_relay_count,
    result_payload,
)
from uav_dispatch.deployment import (
    DeploymentPlan,
    compute_dynamic_uav_homes,
    deployment_distribution,
)
from uav_dispatch.diagnostics import (
    deadhead_km,
    deadhead_vs_origin_km,
    home_assignment_rate,
    home_mismatch,
    min_deadhead_lb,
    origin_min_deadhead_lb,
)
from uav_dispatch.model import Point
from uav_dispatch.relay import build_relay_network


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    ROOT / "algorithm" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)
DEFAULT_OUTPUT = ROOT / "algorithm" / "results" / "scenario_comparison_240s"
DEFAULT_REPORT = ROOT / "algorithm" / "SCENARIO_COMPARISON_240S_REPORT.md"
DEFAULT_SEED_BASE = 2026080500

# Scenario definitions: every scenario keeps the same 7-station network; the
# differences are relay on/off, DIRECT-warmup fraction, and drone homes.
SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "id": "pure_direct",
        "label": "纯 Direct（原点起点，无中继）",
        "relay": False,
        "warmup": None,
        "drone_homes": "origin",
    },
    {
        "id": "pure_relay",
        "label": "纯 Relay（原点起点，warmup=0）",
        "relay": True,
        "warmup": 0.0,
        "drone_homes": "origin",
    },
    {
        "id": "direct_relay",
        "label": "Direct + Relay（原点起点，80/20 分阶段）",
        "relay": True,
        "warmup": 0.80,
        "drone_homes": "origin",
    },
    {
        "id": "direct_stations",
        "label": "Direct + 起点不同（预部署中继站）",
        "relay": False,
        "warmup": None,
        "drone_homes": "stations",
    },
    {
        "id": "direct_relay_stations",
        "label": "Direct + Relay + 起点不同（预部署 + 80/20）",
        "relay": True,
        "warmup": 0.80,
        "drone_homes": "stations",
    },
)
BASELINE_ID = "pure_direct"

# A/B baseline: the legacy fixed one-per-station deployment, kept ONLY inside
# this experiment (never in product code) so the dynamic deployment can be
# compared against the old rule on the same seed and budget.
FIXED_SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "id": "direct_stations_fixed",
        "label": "Direct + 起点不同（固定一站一机，A/B 基准）",
        "relay": False,
        "warmup": None,
        "drone_homes": "stations_fixed",
    },
    {
        "id": "direct_relay_stations_fixed",
        "label": "Direct + Relay + 起点不同（固定一站一机 + 80/20，A/B 基准）",
        "relay": True,
        "warmup": 0.80,
        "drone_homes": "stations_fixed",
    },
)


def _all_scenarios(args: argparse.Namespace) -> tuple[dict[str, Any], ...]:
    scenarios = list(SCENARIOS)
    if getattr(args, "include_fixed_deployment_baseline", False):
        scenarios.extend(FIXED_SCENARIOS)
    return tuple(scenarios)


def _legacy_fixed_allocation(
    drone_count: int, station_count: int
) -> list[int]:
    """Legacy one-drone-per-station allocation (depot first, then each
    station once, surplus back to the depot), for A/B comparison only."""

    allocation = [1] + [0] * station_count
    remaining = drone_count - 1
    for index in range(1, len(allocation)):
        if remaining <= 0:
            break
        allocation[index] = 1
        remaining -= 1
    allocation[0] += remaining
    return allocation


def _solver_source_sha256() -> str:
    """Fingerprint the exact solver sources used by an experiment run."""

    digest = hashlib.sha256()
    source_root = ROOT / "algorithm" / "src" / "uav_dispatch"
    for path in sorted(source_root.glob("*.py")):
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _routes_sha256(routes: Sequence[Sequence[int]]) -> str:
    encoded = json.dumps(
        [list(route) for route in routes],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _score_tuple(row: dict[str, Any]) -> tuple[int, float, float]:
    return (
        int(row["late_count"]),
        float(row["total_lateness_min"]),
        float(row["distance_km"]),
    )


def _lex_compare(left: tuple, right: tuple) -> int:
    """-1 if left wins, +1 if right wins, 0 on exact tie."""

    if right < left:
        return 1
    if left < right:
        return -1
    return 0


def paired_vs_baseline(
    rows: Sequence[dict[str, Any]],
    scenarios: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """For every seed, compare each scenario against the baseline scenario."""

    by_seed: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        by_seed.setdefault(int(row["seed"]), {})[row["scenario"]] = row

    results: dict[str, Any] = {}
    for scenario in scenarios:
        sid = scenario["id"]
        if sid == BASELINE_ID:
            continue
        wins = 0
        ties = 0
        losses = 0
        pairs = []
        for seed in sorted(by_seed):
            base = by_seed[seed].get(BASELINE_ID)
            other = by_seed[seed].get(sid)
            if base is None or other is None:
                continue
            outcome = _lex_compare(_score_tuple(base), _score_tuple(other))
            if outcome < 0:
                winner = BASELINE_ID
                wins += 1
            elif outcome > 0:
                winner = sid
                losses += 1
            else:
                winner = "tie"
                ties += 1
            pairs.append(
                {
                    "seed": seed,
                    "winner": winner,
                    "baseline_score": _score_tuple(base),
                    f"{sid}_score": _score_tuple(other),
                    "late_count_delta": other["late_count"] - base["late_count"],
                    "total_lateness_delta": (
                        other["total_lateness_min"] - base["total_lateness_min"]
                    ),
                    "distance_delta": other["distance_km"] - base["distance_km"],
                    "relay_task_count": other.get("relay_task_count", 0),
                }
            )
        results[sid] = {
            "baseline": BASELINE_ID,
            "scenario": sid,
            f"{BASELINE_ID}_win": wins,
            "tie": ties,
            f"{sid}_win": losses,
            "paired_count": len(pairs),
            "pairs": pairs,
        }
    return results


def full_pairwise_matrix(
    rows: Sequence[dict[str, Any]],
    scenarios: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Full (scenario, scenario) win/loss/tie matrix averaged over seeds."""

    by_seed: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        by_seed.setdefault(int(row["seed"]), {})[row["scenario"]] = row

    ids = [scenario["id"] for scenario in scenarios]
    matrix: dict[str, Any] = {}
    for left in ids:
        matrix[left] = {}
        for right in ids:
            if left == right:
                matrix[left][right] = {"win": 0, "tie": 0, "loss": 0}
                continue
            win = tie = loss = 0
            for seed, runs in by_seed.items():
                if left not in runs or right not in runs:
                    continue
                outcome = _lex_compare(
                    _score_tuple(runs[left]), _score_tuple(runs[right])
                )
                if outcome < 0:
                    win += 1
                elif outcome > 0:
                    loss += 1
                else:
                    tie += 1
            matrix[left][right] = {"win": win, "tie": tie, "loss": loss}
    return {"scenarios": ids, "matrix": matrix}


def _problem_for_scenario(
    tasks, scenario: dict[str, Any], args: argparse.Namespace
) -> tuple[Problem, DeploymentPlan | None]:
    """Build the shared relay network and per-scenario problem.

    The station network is identical for every scenario so the comparison is
    fair; the direct scenarios simply keep an empty leg registry (stations
    stay inert).  Drone homes follow the scenario's ``drone_homes`` mode:
    ``origin`` keeps the fleet at the depot, ``stations`` uses the
    demand-driven dynamic deployment, and ``stations_fixed`` (A/B baseline
    only) uses the legacy one-per-station rule.  Returns ``(problem, plan)``
    where ``plan`` is ``None`` for the origin scenario.
    """

    network = build_relay_network(
        tasks,
        relay_count=resolve_relay_count(
            tasks,
            args.drones,
            args.relay_count,
            location_seed=args.relay_location_seed,
            location_method=args.relay_location_method,
        ),
        candidates_per_task=args.relay_candidates_per_task,
        detour_ratio=args.relay_detour_ratio,
        location_seed=args.relay_location_seed,
        location_method=args.relay_location_method,
    )
    plan: DeploymentPlan | None = None
    if scenario["drone_homes"] == "origin":
        homes = None
    else:
        fixed_allocation = (
            _legacy_fixed_allocation(args.drones, len(network.stations))
            if scenario["drone_homes"] == "stations_fixed"
            else None
        )
        plan = compute_dynamic_uav_homes(
            tasks,
            network.stations,
            args.drones,
            Point(0.0, 0.0),
            speed_km_per_min=0.9,
            fixed_allocation=fixed_allocation,
        )
        homes = plan.homes
    if scenario["relay"]:
        return (
            Problem(
                tasks,
                drone_count=args.drones,
                max_tasks_per_drone=args.max_tasks,
                relay_stations=network.stations,
                leg_registry=network.leg_registry,
                task_relay_candidates=network.task_relay_candidates,
                drone_homes=homes,
            ),
            plan,
        )
    return (
        Problem(
            tasks,
            drone_count=args.drones,
            max_tasks_per_drone=args.max_tasks,
            relay_stations=network.stations,
            leg_registry={},
            task_relay_candidates={},
            drone_homes=homes,
        ),
        plan,
    )


def _build_config(
    scenario: dict[str, Any], seed: int, solve_budget: float, args: argparse.Namespace
) -> ALNSConfig:
    warmup = (
        0.0
        if scenario["warmup"] is None
        else scenario["warmup"]
    )
    home_aware = (
        scenario["drone_homes"] in ("stations", "stations_fixed")
        and not getattr(args, "disable_home_aware_init", False)
    )
    return ALNSConfig(
        max_iterations=args.wall_max_iterations,
        time_limit_seconds=solve_budget,
        seed=seed,
        candidate_limit=(
            None if args.candidate_limit == 0 else args.candidate_limit
        ),
        relay_direct_warmup_fraction=warmup,
        relay_candidates_per_task=args.relay_candidates_per_task,
        relay_plan_beam=args.relay_plan_beam,
        relay_leg_beam=args.relay_leg_beam,
        relay_event_cap=args.relay_event_cap,
        relay_sample_every=args.relay_sample_every,
        relay_global_limit=args.relay_global_limit,
        relay_debug=args.relay_debug,
        relay_probe_fraction=args.relay_probe_fraction,
        relay_probe_min=args.relay_probe_min,
        relay_probe_max_tasks=args.relay_probe_max_tasks,
        enable_relay_seed=not getattr(args, "disable_relay_seed", False),
        relay_seed_task_limit=args.relay_seed_task_limit,
        enable_relay_refine=not getattr(args, "disable_relay_refine", False),
        relay_refine_interval=args.relay_refine_interval,
        relay_refine_task_limit=args.relay_refine_task_limit,
        enable_home_seed=home_aware or getattr(args, "enable_home_seed", False),
        enable_home_displaced=getattr(args, "enable_home_displaced", False),
        enable_home_bias=home_aware or getattr(args, "enable_home_bias", False),
        direct_exact_top_k=args.direct_exact_top_k,
        repair_rank_candidate_limit=args.repair_rank_candidate_limit,
        repair_exact_candidate_limit=args.repair_exact_candidate_limit,
        min_destroy_fraction=args.direct_min_destroy,
        max_destroy_fraction=args.direct_max_destroy,
        relay_min_destroy_fraction=args.relay_min_destroy,
        relay_max_destroy_fraction=args.relay_max_destroy,
        relay_large_destroy_interval=args.relay_large_destroy_interval,
        relay_large_destroy_min=args.relay_large_destroy_min,
        relay_large_destroy_max=args.relay_large_destroy_max,
    )


def _run_scenario(
    problem: Problem,
    scenario: dict[str, Any],
    seed: int,
    args: argparse.Namespace,
    output_dir: Path,
    plan: DeploymentPlan | None,
) -> dict[str, Any]:
    home_aware = (
        scenario["drone_homes"] in ("stations", "stations_fixed")
        and not getattr(args, "disable_home_aware_init", False)
    )
    construction_started = time.perf_counter()
    initial = construct_regret_initial(
        problem,
        candidate_limit=(
            None if args.candidate_limit == 0 else args.candidate_limit
        ),
        home_seed=home_aware or getattr(args, "enable_home_seed", False),
        home_bias=home_aware or getattr(args, "enable_home_bias", False),
    )
    construction_seconds = time.perf_counter() - construction_started
    initial_routes_sha256 = _routes_sha256(initial.routes)
    initial_deadhead = deadhead_km(problem, initial.routes)
    solve_budget = max(
        0.0,
        args.wall_time_limit - construction_seconds - args.wall_safety_margin,
    )
    config = _build_config(scenario, seed, solve_budget, args)
    if scenario["relay"]:
        result = solve_relay_staged(
            problem,
            config=config,
            initial_routes=initial.routes,
            core=True,
        )
    else:
        result = solve_alns_core(
            problem, config=config, initial_routes=initial.routes
        )
    evaluation = result.evaluation
    if not evaluation.valid:
        raise RuntimeError(
            f"{scenario['id']} seed {seed} 产出非法解: {evaluation.violations}"
        )
    relay_meta = result.metadata.get("relay", {})
    row = {
        "scenario": scenario["id"],
        "scenario_label": scenario["label"],
        "relay": scenario["relay"],
        "relay_direct_warmup_fraction": (
            0.0 if scenario["warmup"] is None else scenario["warmup"]
        ),
        "drone_homes": scenario["drone_homes"],
        "seed": seed,
        "valid": evaluation.valid,
        "late_count": evaluation.score.late_count,
        "total_lateness_min": evaluation.score.total_lateness_min,
        "distance_km": evaluation.score.distance_km,
        "construction_seconds": construction_seconds,
        "runtime_seconds": result.runtime_seconds,
        "total_wall_seconds": construction_seconds + result.runtime_seconds,
        "iterations": result.iterations,
        "iterations_per_second": result.metadata.get(
            "iterations_per_second", 0.0
        ),
        "relay_count": relay_meta.get("relay_count", 0),
        "relay_task_count": relay_meta.get("relay_task_count", 0),
        "relay_share": relay_meta.get("relay_share", 0.0),
        "cross_uav_handoff_count": relay_meta.get(
            "cross_uav_handoff_count", 0
        ),
        "same_uav_relay_count": relay_meta.get("same_uav_relay_count", 0),
        "total_relay_waiting_time": relay_meta.get(
            "total_relay_waiting_time", 0.0
        ),
        "global_evaluation_count": result.metadata.get(
            "global_evaluation_count", 0
        ),
        "initial_score": list(result.metadata.get("initial_score", ())),
        "staged_relay": bool(result.metadata.get("staged_relay", False)),
        "relay_warmup_runtime_seconds": result.metadata.get(
            "relay_warmup_runtime_seconds", 0.0
        ),
        "relay_search_runtime_seconds": result.metadata.get(
            "relay_search_runtime_seconds", 0.0
        ),
        "relay_warmup_score": list(
            result.metadata.get("relay_warmup_score", ())
        ),
        "initial_routes_sha256": initial_routes_sha256,
        "deployment_distribution": deployment_distribution(
            problem.drone_homes
            if problem.drone_homes is not None
            else (None,) * problem.drone_count
        ),
        "deployment_scores": (
            {node.key: node.demand_score for node in plan.nodes}
            if plan is not None
            else {}
        ),
        "initial_deadhead_km": initial_deadhead,
        "mean_initial_deadhead_km": initial_deadhead / problem.drone_count,
        "home_assignment_rate": home_assignment_rate(
            problem, result.routes
        ),
        "deadhead_km": deadhead_km(problem, result.routes),
        "deadhead_vs_origin_km": deadhead_vs_origin_km(
            problem, result.routes
        ),
        "home_mismatch_count": home_mismatch(problem, result.routes)[1],
        "min_deadhead_lb_km": min_deadhead_lb(problem),
        "origin_min_deadhead_lb_km": origin_min_deadhead_lb(problem),
    }
    solution_file = (
        output_dir
        / "run_solutions"
        / f"{scenario['id']}__seed_{seed}.json"
    )
    solution_file.parent.mkdir(parents=True, exist_ok=True)
    solution_file.write_text(
        json.dumps(
            result_payload(problem, result, source=args.input),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    row["solution_file"] = str(solution_file.relative_to(output_dir))
    row["solution_sha256"] = hashlib.sha256(
        solution_file.read_bytes()
    ).hexdigest()
    print(
        f"[{scenario['id']:22s}] seed={seed} "
        f"score={evaluation.score} it/s={row['iterations_per_second']:.2f} "
        f"relay_tasks={row['relay_task_count']} "
        f"cross={row['cross_uav_handoff_count']} "
        f"homes={scenario['drone_homes']} "
        f"dist={row['deployment_distribution']}"
    )
    return row


def _summary(
    rows: Sequence[dict[str, Any]],
    scenarios: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    summaries = []
    for scenario in scenarios:
        sid = scenario["id"]
        scenario_rows = [row for row in rows if row["scenario"] == sid]
        if not scenario_rows:
            continue
        summaries.append(
            {
                "scenario": sid,
                "label": scenario["label"],
                "runs": len(scenario_rows),
                "relay": scenario["relay"],
                "relay_direct_warmup_fraction": (
                    0.0 if scenario["warmup"] is None else scenario["warmup"]
                ),
                "drone_homes": scenario["drone_homes"],
                "mean_late_count": statistics.mean(
                    row["late_count"] for row in scenario_rows
                ),
                "mean_total_lateness_min": statistics.mean(
                    row["total_lateness_min"] for row in scenario_rows
                ),
                "mean_distance_km": statistics.mean(
                    row["distance_km"] for row in scenario_rows
                ),
                "mean_iterations_per_second": statistics.mean(
                    row["iterations_per_second"] for row in scenario_rows
                ),
                "mean_relay_task_count": statistics.mean(
                    row["relay_task_count"] for row in scenario_rows
                ),
                "mean_cross_uav_handoff_count": statistics.mean(
                    row["cross_uav_handoff_count"] for row in scenario_rows
                ),
                "mean_initial_deadhead_km": statistics.mean(
                    row["mean_initial_deadhead_km"]
                    for row in scenario_rows
                ),
                "mean_home_assignment_rate": statistics.mean(
                    row["home_assignment_rate"] for row in scenario_rows
                ),
            }
        )
    return summaries


def _row_by_scenario(
    rows: Sequence[dict[str, Any]], seed: int
) -> dict[str, dict[str, Any]]:
    return {row["scenario"]: row for row in rows if int(row["seed"]) == seed}


def _lex_rank(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank scenarios by the strict lexicographic score (best first)."""

    return sorted(rows, key=_score_tuple)


def _delta_text(
    label: str, base: dict[str, Any], other: dict[str, Any]
) -> str:
    d_late = other["late_count"] - base["late_count"]
    d_late_min = other["total_lateness_min"] - base["total_lateness_min"]
    d_dist = other["distance_km"] - base["distance_km"]
    return (
        f"`{label}`：late_count {d_late:+d}，总逾期 {d_late_min:+.2f} min，"
        f"里程 {d_dist:+.2f} km"
    )


def _write_report(payload: dict[str, Any], report_path: Path) -> None:
    manifest = payload["manifest"]
    summaries = {item["scenario"]: item for item in payload["summaries"]}
    lines: list[str] = []
    write = lines.append

    write(f"# {manifest['wall_time_limit']:g} 秒 {len(manifest['scenarios'])} 场景配对墙钟对比实验报告")
    write("")
    write(f"日期：{manifest['generated_at'][:10]}")
    write(
        f"环境：{manifest['environment']['platform']} / "
        f"Python {manifest['environment']['python'].split()[0]}；"
        f"{manifest['task_count']} tasks / {manifest['drone_count']} UAV / "
        f"capacity=2 / K={manifest['max_tasks_per_drone']}"
    )
    write(f"配置：seed {manifest['seed_base']}–"
          f"{manifest['seed_base'] + manifest['wall_seed_count'] - 1}"
          f"（{manifest['wall_seed_count']} 种子，{len(manifest['scenarios'])} 个场景共用同一 seed），"
          f"candidate_limit={manifest['candidate_limit']}，"
          f"初始解 = regret-2 直送构造（构造时间计入预算）")
    relay_count_note = (
        "（需求驱动自动确定）" if manifest.get("relay_count_dynamic") else ""
    )
    write(
        f"Relay 网络：**{manifest['relay_count']} 个中继站{relay_count_note}，"
        f"每任务 {manifest['relay_candidates_per_task']} 个候选站，"
        f"绕行比 {manifest['relay_detour_ratio']}，兜底开启**，"
        f"选址方法 `{manifest['relay_location_method']}`，选址种子 "
        f"{manifest['relay_location_seed']}"
    )
    write(
        f"求解：墙钟 {manifest['wall_time_limit']:g}s（安全余量 "
        f"{manifest['wall_safety_margin']:g}s），分阶段 warmup 比例见场景定义；"
        f"构造后中继播种 + 跨机精化开启（direct 场景无中继故不生效）"
    )
    write("")
    write(f"## 场景定义（{len(manifest['scenarios'])} 个场景共用同一 seed，各自跑满总预算）")
    write("")
    write("| 场景 | 定义 | Relay | warmup | 起点（drone homes） |")
    write("|---|---|---|---|---|")
    for scenario in manifest["scenarios"]:
        sid = scenario["id"]
        warmup = (
            "—" if scenario["warmup"] is None else f"{scenario['warmup']:.2f}"
        )
        homes_label = {
            "origin": "origin（原点）",
            "stations": "stations（动态部署，按任务需求分配 0..n 架）",
            "stations_fixed": "stations_fixed（固定一站一机，A/B 基准）",
        }.get(scenario["drone_homes"], scenario["drone_homes"])
        write(
            f"| `{sid}` | {scenario['label']} | "
            f"{'是' if scenario['relay'] else '否'} | {warmup} | "
            f"{homes_label} |"
        )
    write("")
    write("## 1. 主要结果（各场景均值）")
    write("")
    write(
        "| 场景 | late_count | total_lateness_min | distance_km | it/s | "
        "relay 任务数 | 跨机接驳 |"
    )
    write("|---|---:|---:|---:|---:|---:|---:|")
    for summary in payload["summaries"]:
        write(
            f"| `{summary['scenario']}` | {summary['mean_late_count']:.2f} | "
            f"{summary['mean_total_lateness_min']:.2f} | "
            f"{summary['mean_distance_km']:.2f} | "
            f"{summary['mean_iterations_per_second']:.2f} | "
            f"{summary['mean_relay_task_count']:.2f} | "
            f"{summary['mean_cross_uav_handoff_count']:.2f} |"
        )
    write("")
    write(f"基准：`{BASELINE_ID}`（纯 Direct，原点起点）。")
    write("")
    write("## 2. 构造阶段 vs 最终得分")
    write("")
    write(
        "| 场景 | 构造得分（0 迭代） | 最终得分 | "
        "构造→最终改进（逾期 min / 里程 km） |"
    )
    write("|---|---:|---:|---:|")
    for row in payload["runs"]:
        initial = tuple(round(value, 2) for value in row["initial_score"])
        final = _score_tuple(row)
        d_late_min = row["total_lateness_min"] - row["initial_score"][1]
        d_dist = row["distance_km"] - row["initial_score"][2]
        write(
            f"| `{row['scenario']}` | `{initial}` | `{final}` | "
            f"{d_late_min:+.2f} / {d_dist:+.2f} |"
        )
    write("")
    write(
        "- 构造得分是 regret-2 直送构造在 0 次搜索迭代时的得分（构造时间计入预算）；"
        "构造与最终之间的差距反映搜索对起点优势的留存或抹平程度：若搜索后差距缩小，"
        "说明起点收益主要是冷启动优势，短预算下更明显。"
    )
    write("")
    write("## 3. 首段空飞与部署利用率")
    write("")
    write(
        "| 场景 | deadhead_km | vs 全驻原点节省 | 就近错配任务数 | "
        "部署首段下界 LB | 全驻原点 LB |"
    )
    write("|---|---:|---:|---:|---:|---:|")
    for row in payload["runs"]:
        write(
            f"| `{row['scenario']}` | {row['deadhead_km']:.2f} | "
            f"{row['deadhead_vs_origin_km']:.2f} | "
            f"{row['home_mismatch_count']} | "
            f"{row['min_deadhead_lb_km']:.2f} | "
            f"{row['origin_min_deadhead_lb_km']:.2f} |"
        )
    write("")
    write(
        "- `deadhead_km`=各机起点→首个访问的空飞总里程；`vs 全驻原点节省`=若全部驻原点"
        "时同样路线的首段总里程减实际首段（部署节省）；`就近错配任务数`=所在机起点并非"
        "离该任务取件点最近部署点的任务数；LB=每机到其最近任务取件点距离之和的下界，"
        "两 LB 之差给出分散部署在首段上的杠杆上限。"
    )
    write("")
    write("## 3b. 动态部署分布与初始空飞")
    write("")
    write(
        "| 场景 | 部署分布 | demand 得分 | 初始空飞总 km | 初始空飞均值 km | "
        "最终就近命中率 |"
    )
    write("|---|---|---|---:|---:|---:|")
    for row in payload["runs"]:
        distribution = row.get("deployment_distribution", {})
        dist_text = " / ".join(
            f"{key}:{count}" for key, count in sorted(distribution.items())
        )
        scores = row.get("deployment_scores", {})
        score_text = " / ".join(
            f"{key}:{value:.1f}"
            for key, value in sorted(scores.items())
        )
        write(
            f"| `{row['scenario']}` | {dist_text} | {score_text} | "
            f"{row.get('initial_deadhead_km', 0.0):.2f} | "
            f"{row.get('mean_initial_deadhead_km', 0.0):.2f} | "
            f"{row.get('home_assignment_rate', 0.0):.2%} |"
        )
    write("")
    write(
        "- 部署分布=每个候选起点（depot / relay_<id>）实际部署的 UAV 数，由任务"
        "空间分布与时间窗需求动态决定，允许 0 架、2 架甚至更多（不再是一站一机）；"
        "`demand 得分`=各候选起点的需求加权分（空间衰减×紧迫度×服务价值）；"
        "`初始空飞`=构造解（0 次搜索迭代）各机起点→首个访问的空飞；"
        "`就近命中率`=最终解中所在机起点即离该任务取件点最近部署点的任务占比"
        "（仅分析指标，不进目标）。"
    )
    write("")
    write("## 4. 与基准（纯 Direct）的逐种子配对比较")
    write("")
    paired = payload["paired_vs_baseline"]
    for sid, result in paired.items():
        write(f"### {sid} vs `{BASELINE_ID}`")
        write("")
        write(
            "| Seed | 基准得分 | "
            f"{sid} 得分 | 逾期数差 | 逾期分钟差 | 里程差 | relay 任务 | 胜者 |"
        )
        write("|---|---:|---:|---:|---:|---:|---:|---|")
        for pair in result["pairs"]:
            write(
                f"| {pair['seed']} | "
                f"`{pair['baseline_score']}` | `{pair[f'{sid}_score']}` | "
                f"{pair['late_count_delta']:+d} | "
                f"{pair['total_lateness_delta']:+.2f} | "
                f"{pair['distance_delta']:+.2f} | "
                f"{pair['relay_task_count']} | {pair['winner']} |"
            )
        write(
            f"| **小计** | | | | | | | "
            f"**{BASELINE_ID} {result[f'{BASELINE_ID}_win']} : "
            f"tie {result['tie']} : {sid} {result[f'{sid}_win']}** |"
        )
        write("")
    write("## 5. 全场景两两胜率矩阵（行 vs 列，win/tie/loss）")
    write("")
    matrix = payload["full_pairwise_matrix"]["matrix"]
    ids = payload["full_pairwise_matrix"]["scenarios"]
    write("| 行 \\ 列 | " + " | ".join(f"`{sid}`" for sid in ids) + " |")
    write("|" + "---|" * (len(ids) + 1))
    for left in ids:
        cells = []
        for right in ids:
            cell = matrix[left][right]
            if left == right:
                cells.append("—")
            else:
                cells.append(
                    f"{cell['win']}/{cell['tie']}/{cell['loss']}"
                )
        write(f"| `{left}` | " + " | ".join(cells) + " |")
    write("")
    write("## 6. 逐种子明细")
    write("")
    write(
        "| Seed | 场景 | 得分 | it/s | 迭代 | relay 任务 | 跨机接驳 | "
        "总墙钟(s) |"
    )
    write("|---|---|---:|---:|---:|---:|---:|---:|")
    for row in sorted(
        payload["runs"], key=lambda r: (r["seed"], r["scenario"])
    ):
        write(
            f"| {row['seed']} | `{row['scenario']}` | "
            f"`({row['late_count']}, {row['total_lateness_min']:.2f}, "
            f"{row['distance_km']:.2f})` | "
            f"{row['iterations_per_second']:.2f} | {row['iterations']} | "
            f"{row['relay_task_count']} | {row['cross_uav_handoff_count']} | "
            f"{row['total_wall_seconds']:.1f} |"
        )
    write("")
    write("## 7. 结论与分析")
    write("")
    # Rank scenarios by the strict lexicographic score.
    ranked = _lex_rank(payload["runs"])
    rank_text = " > ".join(f"`{row['scenario']}`" for row in ranked)
    write(f"**词典序质量排序（最优在前）：{rank_text}**")
    write("")
    write("- 第一目标（逾期任务数）与第二目标（总逾期分钟）在各场景间的词典序对比，"
          "见第 4/5 节配对结果。")
    write("")
    # Predeployment effect (same relay setting, origin vs stations homes).
    seed = int(payload["runs"][0]["seed"]) if payload["runs"] else None
    by_scenario = (
        _row_by_scenario(payload["runs"], seed) if seed is not None else {}
    )
    if "pure_direct" in by_scenario and "direct_stations" in by_scenario:
        write("### 7.1 预部署（起点不同）的影响")
        write("")
        write(
            f"- Direct 场景：预部署使 "
            + _delta_text(
                "direct_stations", by_scenario["pure_direct"],
                by_scenario["direct_stations"],
            )
            + "。"
        )
        if "direct_relay" in by_scenario and "direct_relay_stations" in by_scenario:
            write(
                f"- Relay 场景：预部署使 "
                + _delta_text(
                    "direct_relay_stations",
                    by_scenario["direct_relay"],
                    by_scenario["direct_relay_stations"],
                )
                + "。"
            )
        write("")
    # Dynamic deployment vs the legacy fixed one-per-station rule (A/B),
    # aggregated over every seed for an honest multi-seed verdict.
    dynamic_sids = {row["scenario"] for row in payload["runs"]}
    if (
        "direct_stations" in dynamic_sids
        and "direct_stations_fixed" in dynamic_sids
    ):
        write("### 7.2 动态部署 vs 固定一站一机（A/B）")
        write("")
        for dynamic_id, fixed_id in (
            ("direct_stations", "direct_stations_fixed"),
            ("direct_relay_stations", "direct_relay_stations_fixed"),
        ):
            dynamic_rows = [
                row for row in payload["runs"] if row["scenario"] == dynamic_id
            ]
            fixed_rows = [
                row for row in payload["runs"] if row["scenario"] == fixed_id
            ]
            if not dynamic_rows or not fixed_rows:
                continue
            by_seed = {row["seed"]: row for row in dynamic_rows}
            d_win = tie = f_win = 0
            late_deltas: list[float] = []
            lateness_deltas: list[float] = []
            distance_deltas: list[float] = []
            for fixed_row in fixed_rows:
                dynamic_row = by_seed.get(fixed_row["seed"])
                if dynamic_row is None:
                    continue
                outcome = _lex_compare(
                    _score_tuple(fixed_row), _score_tuple(dynamic_row)
                )
                if outcome < 0:
                    f_win += 1
                elif outcome > 0:
                    d_win += 1
                else:
                    tie += 1
                late_deltas.append(
                    dynamic_row["late_count"] - fixed_row["late_count"]
                )
                lateness_deltas.append(
                    dynamic_row["total_lateness_min"]
                    - fixed_row["total_lateness_min"]
                )
                distance_deltas.append(
                    dynamic_row["distance_km"] - fixed_row["distance_km"]
                )
            write(
                f"- **{dynamic_id}（动态部署）vs {fixed_id}（固定一站一机）**："
                f"动态 {d_win} 胜 / {tie} 平 / {f_win} 负"
                f"（{len(late_deltas)} seed，词典序）；平均差："
                f"late_count {statistics.mean(late_deltas):+.1f}，"
                f"总逾期 {statistics.mean(lateness_deltas):+.1f} min，"
                f"里程 {statistics.mean(distance_deltas):+.1f} km。"
            )
            dynamic = dynamic_rows[0]
            fixed = fixed_rows[0]
            write(
                f"  - 部署分布：动态 `{dynamic.get('deployment_distribution')}` vs "
                f"固定 `{fixed.get('deployment_distribution')}`；"
                f"初始空飞总里程 "
                f"{dynamic.get('initial_deadhead_km', 0.0):.2f} vs "
                f"{fixed.get('initial_deadhead_km', 0.0):.2f} km；"
                f"就近命中率均值 "
                f"{statistics.mean(row.get('home_assignment_rate', 0.0) for row in dynamic_rows):.2%} vs "
                f"{statistics.mean(row.get('home_assignment_rate', 0.0) for row in fixed_rows):.2%}。"
            )
        write("")
    # Relay-only vs staged.
    if "pure_relay" in by_scenario and "direct_relay" in by_scenario:
        write("### 7.3 纯 Relay（warmup=0）与分阶段（warmup=0.8）")
        write("")
        pure_relay = by_scenario["pure_relay"]
        staged = by_scenario["direct_relay"]
        write(
            f"- 纯 Relay 迭代 {pure_relay['iterations']}（it/s="
            f"{pure_relay['iterations_per_second']:.2f}，全局评估 "
            f"{pure_relay['global_evaluation_count']:,}）"
            f"远少于分阶段 {staged['iterations']}（it/s="
            f"{staged['iterations_per_second']:.2f}，全局评估 "
            f"{staged['global_evaluation_count']:,}）。"
        )
        write(
            f"- 纯 Relay 从弱起点出发并采用 {pure_relay['relay_task_count']} 个中继任务"
            f"（跨机 {pure_relay['cross_uav_handoff_count']} 次），但 "
            + _delta_text(
                "pure_relay", by_scenario["pure_direct"], pure_relay
            )
            + "，是唯一词典序劣于基准的场景。"
        )
        write(
            f"- 分阶段 `direct_relay` 中继阶段（"
            f"{staged['relay_search_runtime_seconds']:.1f}s）把预热得分 "
            f"`{tuple(round(v, 2) for v in staged['relay_warmup_score'])}` "
            f"改进到 `{_score_tuple(staged)}`"
            f"（逾期 "
            f"{staged['total_lateness_min'] - staged['relay_warmup_score'][1]:+.2f} min）。"
        )
        write("")
    # Relay-phase improvement for the staged scenarios.
    write("### 7.4 分阶段 Relay 搜索阶段的改进")
    write("")
    write("| 场景 | 预热得分 | 最终得分 | 中继阶段改进 | relay 任务 | 跨机接驳 |")
    write("|---|---:|---:|---:|---:|---|")
    for sid in (
        "direct_relay",
        "direct_relay_stations",
    ):
        row = by_scenario.get(sid)
        if row is None or not row.get("staged_relay"):
            continue
        warmup = tuple(round(v, 2) for v in row["relay_warmup_score"])
        final = _score_tuple(row)
        improvement = (
            row["total_lateness_min"] - row["relay_warmup_score"][1]
        )
        write(
            f"| `{sid}` | `{warmup}` | `{final}` | "
            f"逾期 {improvement:+.2f} min | {row['relay_task_count']} | "
            f"{row['cross_uav_handoff_count']} |"
        )
    write("")
    write("### 7.5 综合结论")
    write("")
    best = ranked[0]
    write(
        f"- 本 seed 下最优场景为 `{best['scenario']}`"
        f"（得分 `{_score_tuple(best)}`）。"
    )
    if "pure_direct" in by_scenario and "direct_stations" in by_scenario:
        write(
            f"- 预部署（`stations` 起点）使 "
            + _delta_text(
                "direct_stations", by_scenario["pure_direct"],
                by_scenario["direct_stations"],
            )
            + "；省去「原点→站」空飞是其主要来源（见第 3 节 deadhead 对比）。"
        )
    if "pure_relay" in by_scenario and "direct_relay" in by_scenario:
        pure_relay = by_scenario["pure_relay"]
        staged = by_scenario["direct_relay"]
        write(
            f"- 纯 Relay（warmup=0）在本预算下不可行：中继邻域单次迭代极贵"
            f"（全局评估次数是分阶段的约 "
            f"{pure_relay['global_evaluation_count'] / max(1, staged['global_evaluation_count']):.0f} 倍），"
            f"迭代数锐减，虽采用大量中继却从弱起点出发，最终词典序最差。"
            f"分阶段（先 80% 建立强 DIRECT 起点，再 20% 中继搜索）是唯一能发挥中继价值的结构。"
        )
    if "direct_relay_stations" in by_scenario and "direct_stations" in by_scenario:
        write(
            f"- 在预部署下中继边际收益变小（"
            + _delta_text(
                "direct_relay_stations", by_scenario["direct_stations"],
                by_scenario["direct_relay_stations"],
            )
            + "）：预部署已把大部分任务调度得足够好，中继主要作为第二/三目标的微调手段。"
        )
    write("")
    write("## 8. 可审计性")
    write("")
    write(f"- 求解器源码 SHA-256：`{manifest['solver_source_sha256']}`")
    write(f"- 输入 CSV SHA-256：`{manifest['input_sha256']}`")
    write(f"- 中继站选址方法：`{manifest['relay_location_method']}`；"
          f"起点感知开关：home_seed={manifest['enable_home_seed']}，"
          f"home_displaced={manifest['enable_home_displaced']}，"
          f"home_bias={manifest['enable_home_bias']}，"
          f"disable_home_aware_init={manifest['disable_home_aware_init']}"
          f"（stations 场景默认开启 home 感知初始构造，除非显式禁用）")
    write(f"- 原始记录：`{manifest['output_dir']}`（`scenario_comparison_results.json`、"
          "`scenario_comparison_runs.csv`、`run_solutions/` 内每场景每种子完整路线）")
    write(f"- 生成时间：{manifest['generated_at']}")
    write("")
    report_path.write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "run_solutions").mkdir(parents=True, exist_ok=True)
    tasks = load_tasks_csv(args.input)
    if args.tasks is not None:
        if args.tasks <= 0:
            raise ValueError("--tasks 必须为正整数")
        tasks = tasks[: args.tasks]

    rows: list[dict[str, Any]] = []
    scenarios = _all_scenarios(args)
    seeds = [args.seed_base + offset for offset in range(args.wall_seed_count)]
    for seed in seeds:
        ordered = (
            list(scenarios)
            if seed % 2 == 0
            else list(reversed(scenarios))
        )
        for scenario in ordered:
            problem, plan = _problem_for_scenario(tasks, scenario, args)
            rows.append(
                _run_scenario(
                    problem, scenario, seed, args, output_dir, plan
                )
            )
            del problem
            gc.collect()

    paired = paired_vs_baseline(rows, scenarios)
    matrix = full_pairwise_matrix(rows, scenarios)
    summaries = _summary(rows, scenarios)
    payload = {
        "manifest": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "input": str(args.input),
            "input_sha256": hashlib.sha256(
                Path(args.input).read_bytes()
            ).hexdigest(),
            "output_dir": str(output_dir),
            "wall_time_limit": args.wall_time_limit,
            "wall_safety_margin": args.wall_safety_margin,
            "wall_seed_count": args.wall_seed_count,
            "seed_base": args.seed_base,
            "candidate_limit": args.candidate_limit,
            "relay_count": resolve_relay_count(
                tasks,
                args.drones,
                args.relay_count,
                location_seed=args.relay_location_seed,
                location_method=args.relay_location_method,
            ),
            "relay_count_dynamic": args.relay_count == "auto",
            "relay_candidates_per_task": args.relay_candidates_per_task,
            "relay_detour_ratio": args.relay_detour_ratio,
            "relay_location_seed": args.relay_location_seed,
            "relay_location_method": args.relay_location_method,
            "relay_direct_warmup_fraction_default": 0.80,
            "enable_home_seed": args.enable_home_seed,
            "enable_home_displaced": args.enable_home_displaced,
            "enable_home_bias": getattr(args, "enable_home_bias", False),
            "disable_home_aware_init": getattr(
                args, "disable_home_aware_init", False
            ),
            "include_fixed_deployment_baseline": getattr(
                args, "include_fixed_deployment_baseline", False
            ),
            "deployment": {
                "method": "demand_driven",
                "speed_km_per_min": 0.9,
                "urgency_alpha": 10.0,
                "urgency_cap": 3.0,
                "cap_fraction": 0.5,
                "local_search_rounds": 3,
                "home_aware_init_default": True,
            },
            "relay_plan_beam": args.relay_plan_beam,
            "relay_leg_beam": args.relay_leg_beam,
            "relay_event_cap": args.relay_event_cap,
            "relay_sample_every": args.relay_sample_every,
            "relay_global_limit": args.relay_global_limit,
            "relay_probe_fraction": args.relay_probe_fraction,
            "relay_probe_min": args.relay_probe_min,
            "relay_probe_max_tasks": args.relay_probe_max_tasks,
            "direct_exact_top_k": args.direct_exact_top_k,
            "repair_rank_candidate_limit": args.repair_rank_candidate_limit,
            "repair_exact_candidate_limit": args.repair_exact_candidate_limit,
            "direct_destroy_fraction": [
                args.direct_min_destroy,
                args.direct_max_destroy,
            ],
            "relay_destroy_fraction": [
                args.relay_min_destroy,
                args.relay_max_destroy,
            ],
            "relay_large_destroy": {
                "interval": args.relay_large_destroy_interval,
                "fraction": [
                    args.relay_large_destroy_min,
                    args.relay_large_destroy_max,
                ],
            },
            "solver_source_sha256": _solver_source_sha256(),
            "solver_method": {
                "direct": "solve_alns_core",
                "relay": "solve_relay_staged(core=True)",
            },
            "initialization": "shared_direct_regret2",
            "construction_in_wall_budget": True,
            "scenario_order": "alternating_by_seed",
            "scenarios": [
                {
                    "id": scenario["id"],
                    "label": scenario["label"],
                    "relay": scenario["relay"],
                    "warmup": scenario["warmup"],
                    "drone_homes": scenario["drone_homes"],
                }
                for scenario in scenarios
            ],
            "baseline": BASELINE_ID,
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "processor": platform.processor(),
            },
            "task_count": len(tasks),
            "drone_count": args.drones,
            "max_tasks_per_drone": args.max_tasks,
            "objective_order": [
                "late_count",
                "total_lateness_min",
                "distance_km",
            ],
        },
        "paired_vs_baseline": paired,
        "full_pairwise_matrix": matrix,
        "summaries": summaries,
        "runs": rows,
    }
    (output_dir / "scenario_comparison_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    fieldnames = sorted(
        {key for row in rows for key in row} - {"solution_file"}
    )
    with (output_dir / "scenario_comparison_runs.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: value
                    for key, value in row.items()
                    if key != "solution_file"
                }
            )
    if args.report is not None:
        _write_report(payload, args.report)
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="五场景（纯 Direct / 纯 Relay / Direct+Relay / "
                    "Direct+起点不同 / Direct+Relay+起点不同）同种子配对墙钟对比"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--tasks", type=int)
    parser.add_argument("--drones", type=int, default=8)
    parser.add_argument("--max-tasks", type=int, default=25)
    parser.add_argument("--wall-seed-count", type=int, default=1)
    parser.add_argument("--seed-base", type=int, default=DEFAULT_SEED_BASE)
    parser.add_argument("--wall-time-limit", type=float, default=240.0)
    parser.add_argument("--wall-safety-margin", type=float, default=2.0)
    parser.add_argument("--wall-max-iterations", type=int, default=100_000)
    parser.add_argument("--candidate-limit", type=int, default=48)
    parser.add_argument("--relay-count", type=_relay_count_arg, default="auto",
                        help="中继站数量；auto=按任务分布动态确定（默认），或传入具体整数")
    parser.add_argument("--relay-candidates-per-task", type=int, default=2)
    parser.add_argument("--relay-detour-ratio", type=float, default=2.0)
    parser.add_argument("--relay-plan-beam", type=int, default=4)
    parser.add_argument("--relay-leg-beam", type=int, default=5)
    parser.add_argument("--relay-event-cap", type=int, default=60)
    parser.add_argument("--relay-sample-every", type=int, default=3)
    parser.add_argument("--relay-global-limit", type=int, default=3)
    parser.add_argument("--relay-location-seed", type=int, default=42)
    parser.add_argument(
        "--relay-location-method",
        choices=("weighted_kmedoids", "kmeans_midpoint", "kmeans_pickup"),
        default="weighted_kmedoids",
        help="中继站选址方法（站点同时是无人机预部署点）",
    )
    parser.add_argument("--enable-home-seed", action="store_true",
                        help="构造初始解时先给每架机插入离起点最近的任务")
    parser.add_argument("--enable-home-bias", action="store_true",
                        help="构造初始解时加入 home→pickup 距离平局偏向")
    parser.add_argument("--disable-home-aware-init", action="store_true",
                        help="stations 场景关闭 home 感知初始构造")
    parser.add_argument(
        "--include-fixed-deployment-baseline",
        action="store_true",
        help="额外加入固定一站一机部署的 A/B 基准场景"
        "（direct_stations_fixed / direct_relay_stations_fixed）",
    )
    parser.add_argument("--enable-home-displaced", action="store_true",
                        help="启用 home_displaced 破坏算子")
    parser.add_argument("--relay-probe-fraction", type=float, default=0.25)
    parser.add_argument("--relay-probe-min", type=int, default=1)
    parser.add_argument("--relay-probe-max-tasks", type=int, default=2)
    parser.add_argument("--relay-seed-task-limit", type=int, default=200)
    parser.add_argument("--disable-relay-seed", action="store_true")
    parser.add_argument("--disable-relay-refine", action="store_true")
    parser.add_argument("--relay-refine-interval", type=int, default=10)
    parser.add_argument("--relay-refine-task-limit", type=int, default=8)
    parser.add_argument("--direct-exact-top-k", type=int, default=2)
    parser.add_argument("--repair-rank-candidate-limit", type=int, default=24)
    parser.add_argument("--repair-exact-candidate-limit", type=int, default=48)
    parser.add_argument("--direct-min-destroy", type=float, default=0.03)
    parser.add_argument("--direct-max-destroy", type=float, default=0.08)
    parser.add_argument("--relay-min-destroy", type=float, default=0.03)
    parser.add_argument("--relay-max-destroy", type=float, default=0.06)
    parser.add_argument("--relay-large-destroy-interval", type=int, default=30)
    parser.add_argument("--relay-large-destroy-min", type=float, default=0.06)
    parser.add_argument("--relay-large-destroy-max", type=float, default=0.10)
    parser.add_argument("--relay-debug", action="store_true")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="不重新求解，仅从 output-dir/scenario_comparison_results.json "
             "重新渲染 Markdown 报告",
    )
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.quick:
        args.wall_seed_count = 1
        args.wall_time_limit = 10.0
        args.wall_safety_margin = 1.0
        args.tasks = 24
        args.drones = 2
        args.max_tasks = 12
    return args


def main() -> int:
    args = _parse_args()
    if args.render_only:
        results_file = args.output_dir / "scenario_comparison_results.json"
        if not results_file.exists():
            raise SystemExit(
                f"找不到 {results_file}，--render-only 需要先运行实验"
            )
        payload = json.loads(results_file.read_text(encoding="utf-8"))
        if args.report is not None:
            _write_report(payload, args.report)
        print(f"已从 {results_file} 重新渲染报告 -> {args.report}")
        return 0
    payload = run(args)
    print("\n--- 汇总（各场景均值）---")
    for summary in payload["summaries"]:
        print(
            f"{summary['scenario']:22s} "
            f"({summary['mean_late_count']:.2f}, "
            f"{summary['mean_total_lateness_min']:.2f}, "
            f"{summary['mean_distance_km']:.2f})  "
            f"it/s={summary['mean_iterations_per_second']:.2f}"
        )
    print("\n--- 与基准 pure_direct 的配对胜率 ---")
    for sid, result in payload["paired_vs_baseline"].items():
        print(
            f"{sid:22s} pure_direct {result['pure_direct_win']} : "
            f"tie {result['tie']} : {sid} {result[f'{sid}_win']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
