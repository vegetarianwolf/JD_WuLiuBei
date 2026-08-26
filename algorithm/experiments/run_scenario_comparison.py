"""Run the final four-scenario comparison on the official 200-task data.

Protocol fixed by the final chat adjustment: each enabled mechanism receives
one 240 s wall-clock module.  Direct therefore receives 240 s, Direct + Relay
and Direct + Station receive 480 s, and Direct + Relay + Station receives
720 s.  The same three seeds are paired across all four scenarios, and initial
route construction time is included in each allowance.

The script writes fresh JSON/CSV artefacts and one JSON solution per run.  It
does not read or reuse historical result files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from uav_dispatch import (
    ALNSConfig,
    Problem,
    construct_regret_initial,
    load_tasks_csv,
    solve_alns,
    solve_relay_staged,
)
from uav_dispatch.cli import resolve_relay_count, result_payload
from uav_dispatch.deployment import (
    DeploymentPlan,
    compute_dynamic_uav_homes,
    deployment_distribution,
)
from uav_dispatch.diagnostics import deadhead_km, home_assignment_rate
from uav_dispatch.model import Point, Task
from uav_dispatch.relay import build_relay_network


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    ROOT / "algorithm" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)
DEFAULT_OUTPUT = ROOT / "algorithm" / "results" / "final_scenario_comparison"
DEFAULT_SEEDS = (2026081701, 2026081702, 2026081703)
FORMAL_PROTOCOL_CONFIG: dict[str, Any] = {
    "seeds": DEFAULT_SEEDS,
    "tasks": 200,
    "drones": 8,
    "max_tasks": 25,
    "base_seconds": 240.0,
    "bonus_seconds": 240.0,
    "safety_margin_seconds": 0.05,
    "max_iterations": 10_000_000,
    "candidate_limit": 48,
    "relay_count": "auto",
    "relay_location_seed": 42,
}

SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "id": "pure_direct",
        "label": "Direct（原点起点）",
        "relay": False,
        "warmup": 0.0,
        "drone_homes": "origin",
    },
    {
        "id": "direct_relay",
        "label": "Direct + Relay（原点起点）",
        "relay": True,
        "warmup": 0.50,
        "drone_homes": "origin",
    },
    {
        "id": "direct_stations",
        "label": "Direct + Station（需求驱动预部署）",
        "relay": False,
        "warmup": 0.0,
        "drone_homes": "stations",
    },
    {
        "id": "direct_relay_stations",
        "label": "Direct + Relay + Station",
        "relay": True,
        "warmup": 2.0 / 3.0,
        "drone_homes": "stations",
    },
)


def scenario_time_limit_seconds(
    scenario: dict[str, Any],
    *,
    base_seconds: float,
    relay_or_station_bonus_seconds: float,
    effective_seconds_override: float | None = None,
) -> float:
    """Return the effective wall-clock allowance for one scenario.

    The formal four-scenario comparison accumulates one bonus for each enabled
    extension (Relay and station predeployment).  A caller such as the 6.2
    sensitivity analysis may explicitly override that modular 6.1 policy with
    its own fixed total budget.
    """

    if not math.isfinite(base_seconds) or base_seconds <= 0:
        raise ValueError("基础墙钟预算必须是有限正数")
    if (
        not math.isfinite(relay_or_station_bonus_seconds)
        or relay_or_station_bonus_seconds < 0
    ):
        raise ValueError("Relay/Station 加时必须是有限非负数")
    if effective_seconds_override is not None:
        if (
            not math.isfinite(effective_seconds_override)
            or effective_seconds_override <= 0
        ):
            raise ValueError("覆盖墙钟预算必须是有限正数")
        return float(effective_seconds_override)
    extension_count = int(bool(scenario["relay"])) + int(
        scenario["drone_homes"] == "stations"
    )
    return base_seconds + extension_count * relay_or_station_bonus_seconds


def _scenario_stage_targets_seconds(
    scenario: dict[str, Any],
    effective_seconds: float,
) -> dict[str, float]:
    """Return the nominal Direct and Relay modules for audit manifests."""

    if not bool(scenario["relay"]):
        return {
            "direct_search": float(effective_seconds),
            "relay_search": 0.0,
        }
    warmup_fraction = float(scenario["warmup"])
    return {
        "direct_search": effective_seconds * warmup_fraction,
        "relay_search": effective_seconds * (1.0 - warmup_fraction),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _solver_source_sha256() -> str:
    digest = hashlib.sha256()
    source_root = ROOT / "algorithm" / "src" / "uav_dispatch"
    for path in sorted(source_root.glob("*.py")):
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _git_state() -> tuple[str | None, bool | None]:
    """Return the repository revision and dirty flag when Git is available."""

    try:
        commit = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    return commit or None, bool(status.strip())


def _environment_manifest() -> dict[str, Any]:
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "UAV_DISPATCH_LEGACY_CANDIDATE_EVAL": os.environ.get(
            "UAV_DISPATCH_LEGACY_CANDIDATE_EVAL"
        ),
    }


def _optional_sha256(path: Path) -> str | None:
    return _sha256(path) if path.is_file() else None


def build_scenario_problem(
    tasks: Sequence[Task],
    scenario: dict[str, Any],
    *,
    drones: int,
    max_tasks: int,
    relay_count: int | str = "auto",
    relay_candidates_per_task: int = 2,
    relay_detour_ratio: float = 2.0,
    relay_location_seed: int = 42,
    relay_location_method: str = "weighted_kmedoids",
) -> tuple[Problem, DeploymentPlan | None]:
    """Build one scenario while keeping station geometry comparable."""

    concrete_relay_count = resolve_relay_count(
        tasks,
        drones,
        relay_count,
        location_seed=relay_location_seed,
        location_method=relay_location_method,
    )
    network = build_relay_network(
        tasks,
        relay_count=concrete_relay_count,
        candidates_per_task=relay_candidates_per_task,
        detour_ratio=relay_detour_ratio,
        location_seed=relay_location_seed,
        location_method=relay_location_method,
    )
    plan: DeploymentPlan | None = None
    homes = None
    if scenario["drone_homes"] == "stations":
        plan = compute_dynamic_uav_homes(
            tasks,
            network.stations,
            drones,
            Point(0.0, 0.0),
            speed_km_per_min=0.9,
        )
        homes = plan.homes
    problem = Problem(
        tuple(tasks),
        drone_count=drones,
        max_tasks_per_drone=max_tasks,
        capacity=2,
        speed_km_per_min=0.9,
        depot=Point(0.0, 0.0),
        relay_stations=network.stations,
        leg_registry=network.leg_registry if scenario["relay"] else {},
        task_relay_candidates=(
            network.task_relay_candidates if scenario["relay"] else {}
        ),
        drone_homes=homes,
    )
    return problem, plan


def _config(
    scenario: dict[str, Any],
    *,
    seed: int,
    solve_seconds: float,
    max_iterations: int,
    candidate_limit: int | None,
    home_aware: bool,
) -> ALNSConfig:
    return ALNSConfig(
        max_iterations=max_iterations,
        time_limit_seconds=max(1e-6, solve_seconds),
        seed=seed,
        candidate_limit=candidate_limit,
        relay_direct_warmup_fraction=float(scenario["warmup"]),
        relay_candidates_per_task=2,
        relay_plan_beam=4,
        relay_leg_beam=5,
        relay_event_cap=60,
        relay_sample_every=3,
        relay_global_limit=3,
        relay_probe_fraction=0.25,
        relay_probe_min=1,
        relay_probe_max_tasks=2,
        relay_seed_task_limit=200,
        relay_refine_interval=10,
        relay_refine_task_limit=8,
        enable_home_seed=home_aware,
        enable_home_bias=home_aware,
    )


def run_scenario(
    tasks: Sequence[Task],
    scenario: dict[str, Any],
    *,
    seed: int,
    output_dir: Path,
    input_path: Path,
    drones: int,
    max_tasks: int,
    base_seconds: float,
    bonus_seconds: float,
    safety_margin_seconds: float,
    effective_seconds_override: float | None = None,
    max_iterations: int,
    candidate_limit: int | None,
    relay_count: int | str,
    relay_location_seed: int,
) -> dict[str, Any]:
    """Run one auditable scenario/seed cell of the paired design."""

    problem, plan = build_scenario_problem(
        tasks,
        scenario,
        drones=drones,
        max_tasks=max_tasks,
        relay_count=relay_count,
        relay_location_seed=relay_location_seed,
    )
    effective_seconds = scenario_time_limit_seconds(
        scenario,
        base_seconds=base_seconds,
        relay_or_station_bonus_seconds=bonus_seconds,
        effective_seconds_override=effective_seconds_override,
    )
    home_aware = scenario["drone_homes"] == "stations"
    wall_started = time.perf_counter()
    initial = construct_regret_initial(
        problem,
        candidate_limit=candidate_limit,
        home_seed=home_aware,
        home_bias=home_aware,
    )
    construction_seconds = time.perf_counter() - wall_started
    solve_seconds = max(
        1e-6,
        effective_seconds - construction_seconds - safety_margin_seconds,
    )
    config = _config(
        scenario,
        seed=seed,
        solve_seconds=solve_seconds,
        max_iterations=max_iterations,
        candidate_limit=candidate_limit,
        home_aware=home_aware,
    )
    solver = solve_relay_staged if scenario["relay"] else solve_alns
    result = solver(problem, config=config, initial_routes=initial.routes)
    wall_seconds = time.perf_counter() - wall_started
    if not result.evaluation.valid:
        raise RuntimeError(
            f"{scenario['id']} seed={seed} 产生非法解："
            f"{result.evaluation.violations}"
        )

    solution_dir = output_dir / "run_solutions"
    solution_dir.mkdir(parents=True, exist_ok=True)
    solution_path = solution_dir / f"{scenario['id']}__seed_{seed}.json"
    solution_path.write_text(
        json.dumps(
            result_payload(problem, result, source=str(input_path)),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    evaluation = result.evaluation
    relay = dict(result.metadata.get("relay", {}))
    relay_warmup_runtime_seconds = float(
        result.metadata.get("relay_warmup_runtime_seconds", 0.0)
    )
    relay_search_runtime_seconds = float(
        result.metadata.get("relay_search_runtime_seconds", 0.0)
    )
    homes = problem.drone_homes or (None,) * problem.drone_count
    row = {
        "scenario": scenario["id"],
        "scenario_label": scenario["label"],
        "seed": seed,
        "relay": bool(scenario["relay"]),
        "station_predeployment": home_aware,
        "effective_time_limit_seconds": effective_seconds,
        "construction_seconds": construction_seconds,
        "solver_runtime_seconds": result.runtime_seconds,
        "relay_warmup_runtime_seconds": relay_warmup_runtime_seconds,
        "relay_search_runtime_seconds": relay_search_runtime_seconds,
        "wall_seconds": wall_seconds,
        "iterations": result.iterations,
        "late_count": evaluation.score.late_count,
        "total_lateness_min": evaluation.score.total_lateness_min,
        "distance_km": evaluation.score.distance_km,
        "relay_task_count": relay.get("relay_task_count", 0),
        "cross_uav_handoff_count": relay.get("cross_uav_handoff_count", 0),
        "total_relay_waiting_min": relay.get("total_relay_waiting_time", 0.0),
        "station_count": len(problem.relay_stations),
        "deployment_distribution": deployment_distribution(homes),
        "deployment_scores": (
            {node.key: node.demand_score for node in plan.nodes}
            if plan is not None
            else {}
        ),
        "deadhead_km": deadhead_km(problem, result.routes),
        "home_assignment_rate": home_assignment_rate(problem, result.routes),
        "operator_statistics": {
            key: dict(value)
            for key, value in result.metadata.get(
                "operator_statistics", {}
            ).items()
        },
        "method": result.metadata.get("method"),
        "solution_file": solution_path.relative_to(output_dir).as_posix(),
        "solution_sha256": _sha256(solution_path),
    }
    print(
        f"[{scenario['id']:<23}] seed={seed} "
        f"budget={effective_seconds:.0f}s "
        f"score=({row['late_count']}, "
        f"{row['total_lateness_min']:.3f}, {row['distance_km']:.3f}) "
        f"relay={row['relay_task_count']} wall={wall_seconds:.1f}s",
        flush=True,
    )
    return row


def summarize(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        selected = [row for row in rows if row["scenario"] == scenario["id"]]
        if not selected:
            continue
        summaries.append(
            {
                "scenario": scenario["id"],
                "label": scenario["label"],
                "runs": len(selected),
                "mean_late_count": statistics.fmean(
                    row["late_count"] for row in selected
                ),
                "std_late_count": (
                    statistics.stdev(row["late_count"] for row in selected)
                    if len(selected) > 1
                    else 0.0
                ),
                "mean_total_lateness_min": statistics.fmean(
                    row["total_lateness_min"] for row in selected
                ),
                "std_total_lateness_min": (
                    statistics.stdev(
                        row["total_lateness_min"] for row in selected
                    )
                    if len(selected) > 1
                    else 0.0
                ),
                "mean_distance_km": statistics.fmean(
                    row["distance_km"] for row in selected
                ),
                "std_distance_km": (
                    statistics.stdev(row["distance_km"] for row in selected)
                    if len(selected) > 1
                    else 0.0
                ),
                "mean_relay_task_count": statistics.fmean(
                    row["relay_task_count"] for row in selected
                ),
                "mean_wall_seconds": statistics.fmean(
                    row["wall_seconds"] for row in selected
                ),
            }
        )
    return summaries


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    scalar_rows = [
        {
            key: value
            for key, value in row.items()
            if not isinstance(value, (dict, list, tuple))
        }
        for row in rows
    ]
    if not scalar_rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(scalar_rows[0]))
        writer.writeheader()
        writer.writerows(scalar_rows)


def _write_report(
    path: Path,
    rows: Sequence[dict[str, Any]],
    summaries: Sequence[dict[str, Any]],
    manifest: dict[str, Any],
) -> None:
    lines = [
        "# 正式四场景实验结果",
        "",
        "目标按（逾期任务数、总逾期时间、总航程）严格词典序比较。",
        "",
        "| 场景 | 预算/s | 运行数 | 逾期数（均值±标准差） | 总逾期/min | 航程/km | Relay任务 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    budget_by_id = {
        scenario["id"]: scenario_time_limit_seconds(
            scenario,
            base_seconds=manifest["base_seconds"],
            relay_or_station_bonus_seconds=manifest["bonus_seconds"],
        )
        for scenario in SCENARIOS
    }
    for item in summaries:
        lines.append(
            f"| {item['label']} | {budget_by_id[item['scenario']]:.0f} | "
            f"{item['runs']} | {item['mean_late_count']:.2f}±"
            f"{item['std_late_count']:.2f} | "
            f"{item['mean_total_lateness_min']:.2f}±"
            f"{item['std_total_lateness_min']:.2f} | "
            f"{item['mean_distance_km']:.2f}±"
            f"{item['std_distance_km']:.2f} | "
            f"{item['mean_relay_task_count']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## 逐次运行",
            "",
            "| 场景 | seed | 逾期数 | 总逾期/min | 航程/km | Relay任务 | Direct阶段/s | Relay阶段/s | 墙钟/s |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rows:
        direct_runtime = (
            row["relay_warmup_runtime_seconds"]
            if row["relay"]
            else row["solver_runtime_seconds"]
        )
        lines.append(
            f"| {row['scenario']} | {row['seed']} | {row['late_count']} | "
            f"{row['total_lateness_min']:.3f} | {row['distance_km']:.3f} | "
            f"{row['relay_task_count']} | {direct_runtime:.1f} | "
            f"{row['relay_search_runtime_seconds']:.1f} | "
            f"{row['wall_seconds']:.1f} |"
        )
    lines.extend(
        [
            "",
            f"- 输入 SHA-256：`{manifest['input_sha256']}`",
            f"- 求解器源码 SHA-256：`{manifest['solver_source_sha256']}`",
            f"- 生成时间（UTC）：{manifest['created_at_utc']}",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _parse_relay_count(value: str) -> int | str:
    if value.lower() == "auto":
        return "auto"
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("中继站数必须为正整数或 auto")
    return parsed


def validate_formal_protocol(args: argparse.Namespace) -> None:
    """Reject a non-final configuration unless the run is marked quick."""

    if args.quick:
        return
    mismatches: list[str] = []
    for name, expected in FORMAL_PROTOCOL_CONFIG.items():
        actual = getattr(args, name)
        if name == "seeds":
            actual = tuple(actual)
        if actual != expected:
            mismatches.append(f"{name}={actual!r}（应为 {expected!r}）")
    if not args.input.is_file():
        mismatches.append(f"input={args.input!s}（文件不存在）")
    elif _sha256(args.input) != _sha256(DEFAULT_INPUT):
        mismatches.append("input_sha256 与官方数据不一致")
    if mismatches:
        raise ValueError("正式四场景协议参数不可覆盖：" + "；".join(mismatches))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seeds", type=int, nargs="+", default=DEFAULT_SEEDS)
    parser.add_argument("--tasks", type=int, default=200)
    parser.add_argument("--drones", type=int, default=8)
    parser.add_argument("--max-tasks", type=int, default=25)
    parser.add_argument("--base-seconds", type=float, default=240.0)
    parser.add_argument(
        "--bonus-seconds",
        type=float,
        default=240.0,
        help="每启用一个扩展机制（Relay 或 Station）增加的墙钟秒数",
    )
    parser.add_argument("--safety-margin-seconds", type=float, default=0.05)
    parser.add_argument("--max-iterations", type=int, default=10_000_000)
    parser.add_argument("--candidate-limit", type=int, default=48)
    parser.add_argument("--relay-count", type=_parse_relay_count, default="auto")
    parser.add_argument("--relay-location-seed", type=int, default=42)
    execution_mode = parser.add_mutually_exclusive_group()
    execution_mode.add_argument(
        "--worker-seed",
        type=int,
        choices=DEFAULT_SEEDS,
        help="仅运行一颗正式种子的四场景并写入原子分片",
    )
    execution_mode.add_argument(
        "--finalize-shards",
        action="store_true",
        help="不求解；校验三颗正式种子分片并生成最终汇总",
    )
    execution_mode.add_argument(
        "--quick",
        action="store_true",
        help="仅用于冒烟：24任务、每格约1秒，不作为论文结果",
    )
    return parser


def build_manifest(
    args: argparse.Namespace,
    *,
    task_count: int,
) -> dict[str, Any]:
    """Build the versioned, self-auditing manifest for one formal run."""

    candidate_limit = (
        None if args.candidate_limit == 0 else args.candidate_limit
    )
    alns_by_scenario: dict[str, dict[str, Any]] = {}
    for scenario in SCENARIOS:
        nominal_budget = scenario_time_limit_seconds(
            scenario,
            base_seconds=args.base_seconds,
            relay_or_station_bonus_seconds=args.bonus_seconds,
        )
        config = asdict(
            _config(
                scenario,
                seed=args.seeds[0],
                solve_seconds=nominal_budget,
                max_iterations=args.max_iterations,
                candidate_limit=candidate_limit,
                home_aware=scenario["drone_homes"] == "stations",
            )
        )
        config.pop("seed")
        config.pop("time_limit_seconds")
        config["scenario_relay"] = bool(scenario["relay"])
        alns_by_scenario[scenario["id"]] = config

    git_commit, git_dirty = _git_state()
    return {
        "schema_version": 1,
        "protocol": "final_four_scenarios_three_paired_seeds",
        "formal": not args.quick,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "experiment_script_sha256": _sha256(Path(__file__)),
        "solver_source_sha256": _solver_source_sha256(),
        "pyproject_sha256": _optional_sha256(ROOT / "pyproject.toml"),
        "requirements_dev_sha256": _optional_sha256(
            ROOT / "requirements-dev.txt"
        ),
        "environment": _environment_manifest(),
        # Backward-compatible flat environment fields used by earlier tools.
        "python": platform.python_version(),
        "platform": platform.platform(),
        "input": str(args.input),
        "input_sha256": _sha256(args.input),
        "tasks": task_count,
        "drones": args.drones,
        "max_tasks_per_drone": args.max_tasks,
        "seeds": list(args.seeds),
        "base_seconds": args.base_seconds,
        "bonus_seconds": args.bonus_seconds,
        "budget_policy": "base plus one bonus per enabled extension",
        "scenario_budgets_seconds": _scenario_budgets_for_args(args),
        "scenario_stage_targets_seconds": (
            _scenario_stage_targets_for_args(args)
        ),
        "scenarios": list(SCENARIOS),
        "operator_count": 14,
        "solver_config": {
            "max_iterations": args.max_iterations,
            "candidate_limit": candidate_limit,
            "safety_margin_seconds": args.safety_margin_seconds,
            "capacity": 2,
            "speed_km_per_min": 0.9,
            "depot_km": [0.0, 0.0],
            "relay_count_requested": args.relay_count,
            "relay_candidates_per_task": 2,
            "relay_detour_ratio": 2.0,
            "relay_location_seed": args.relay_location_seed,
            "relay_location_method": "weighted_kmedoids",
            "time_limit_policy": (
                "effective budget - initial construction - safety margin; "
                "exact per-run limit is stored in solution metadata"
            ),
            "alns_by_scenario": alns_by_scenario,
        },
    }


def _scenario_budgets_for_args(
    args: argparse.Namespace,
) -> dict[str, float]:
    return {
        scenario["id"]: scenario_time_limit_seconds(
            scenario,
            base_seconds=args.base_seconds,
            relay_or_station_bonus_seconds=args.bonus_seconds,
        )
        for scenario in SCENARIOS
    }


def _scenario_stage_targets_for_args(
    args: argparse.Namespace,
) -> dict[str, dict[str, float]]:
    budgets = _scenario_budgets_for_args(args)
    return {
        scenario["id"]: _scenario_stage_targets_seconds(
            scenario,
            budgets[scenario["id"]],
        )
        for scenario in SCENARIOS
    }


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Atomically publish JSON so finalize never observes a partial shard."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _build_seed_shard_payload(
    args: argparse.Namespace,
    *,
    seed: int,
    rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Build one self-auditing seed shard for later strict finalization."""

    if seed not in DEFAULT_SEEDS:
        raise ValueError(f"分片种子必须属于正式种子：{DEFAULT_SEEDS}")
    return {
        "schema_version": 1,
        "protocol": "final_four_scenarios_seed_shard",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "row_count": len(rows),
        "input_sha256": _sha256(args.input),
        "experiment_script_sha256": _sha256(Path(__file__)),
        "solver_source_sha256": _solver_source_sha256(),
        "scenario_budgets_seconds": _scenario_budgets_for_args(args),
        "scenario_stage_targets_seconds": (
            _scenario_stage_targets_for_args(args)
        ),
        "rows": list(rows),
    }


def _finite_row_number(
    row: dict[str, Any],
    field: str,
    *,
    cell: tuple[int, str],
) -> float:
    try:
        value = float(row[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"分片单元格 {cell} 缺少有效字段 {field}") from exc
    if not math.isfinite(value):
        raise ValueError(f"分片单元格 {cell} 的 {field} 不是有限数")
    return value


def _validate_relay_phase_runtime(
    row: dict[str, Any],
    scenario: dict[str, Any],
    *,
    budget: float,
    safety_margin_seconds: float,
    cell: tuple[int, str],
) -> None:
    """Validate that staged Relay runtime follows the formal module split."""

    if not bool(scenario["relay"]):
        return
    construction = _finite_row_number(
        row,
        "construction_seconds",
        cell=cell,
    )
    solver_runtime = _finite_row_number(
        row,
        "solver_runtime_seconds",
        cell=cell,
    )
    warmup_runtime = _finite_row_number(
        row,
        "relay_warmup_runtime_seconds",
        cell=cell,
    )
    relay_runtime = _finite_row_number(
        row,
        "relay_search_runtime_seconds",
        cell=cell,
    )
    if min(construction, solver_runtime, warmup_runtime, relay_runtime) < 0:
        raise ValueError(f"分片单元格 {cell} 的阶段时长不能为负")

    expected_solver_runtime = max(
        1e-6,
        budget - construction - safety_margin_seconds,
    )
    total_tolerance = max(2.0, 0.01 * budget)
    if not math.isclose(
        solver_runtime,
        expected_solver_runtime,
        rel_tol=0.0,
        abs_tol=total_tolerance,
    ):
        raise ValueError(
            f"分片单元格 {cell} 的阶段时长总额与正式预算不一致"
        )
    if not math.isclose(
        warmup_runtime + relay_runtime,
        solver_runtime,
        rel_tol=0.0,
        abs_tol=total_tolerance,
    ):
        raise ValueError(f"分片单元格 {cell} 的阶段时长之和不一致")

    warmup_fraction = float(scenario["warmup"])
    expected_warmup = expected_solver_runtime * warmup_fraction
    expected_relay = expected_solver_runtime * (1.0 - warmup_fraction)
    warmup_tolerance = max(2.0, 0.01 * expected_warmup)
    relay_tolerance = max(2.0, 0.01 * expected_relay)
    if not math.isclose(
        warmup_runtime,
        expected_warmup,
        rel_tol=0.0,
        abs_tol=warmup_tolerance,
    ) or not math.isclose(
        relay_runtime,
        expected_relay,
        rel_tol=0.0,
        abs_tol=relay_tolerance,
    ):
        raise ValueError(
            f"分片单元格 {cell} 的阶段时长未按正式比例分配"
        )


def _load_and_validate_seed_shards(
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    """Load exactly the three formal seed shards and validate all 12 cells."""

    expected_budgets = _scenario_budgets_for_args(args)
    expected_stage_targets = _scenario_stage_targets_for_args(args)
    expected_input_sha256 = _sha256(args.input)
    expected_script_sha256 = _sha256(Path(__file__))
    expected_solver_sha256 = _solver_source_sha256()
    scenarios_by_id = {scenario["id"]: scenario for scenario in SCENARIOS}
    rows_by_cell: dict[tuple[int, str], dict[str, Any]] = {}

    for seed in DEFAULT_SEEDS:
        shard_path = args.output_dir / "shards" / f"seed_{seed}.json"
        if not shard_path.is_file():
            raise ValueError(f"缺少正式种子分片：{shard_path}")
        try:
            payload = json.loads(shard_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"正式种子分片无法读取：{shard_path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"正式种子分片必须是 JSON 对象：{shard_path}")
        expected_header = {
            "schema_version": 1,
            "protocol": "final_four_scenarios_seed_shard",
            "seed": seed,
            "input_sha256": expected_input_sha256,
            "experiment_script_sha256": expected_script_sha256,
            "solver_source_sha256": expected_solver_sha256,
            "scenario_budgets_seconds": expected_budgets,
            "scenario_stage_targets_seconds": expected_stage_targets,
        }
        for field, expected in expected_header.items():
            if payload.get(field) != expected:
                raise ValueError(
                    f"正式种子分片 {shard_path.name} 的 {field} 不一致"
                )
        shard_rows = payload.get("rows")
        if (
            not isinstance(shard_rows, list)
            or len(shard_rows) != len(SCENARIOS)
            or payload.get("row_count") != len(SCENARIOS)
        ):
            raise ValueError(f"正式种子分片 {shard_path.name} 必须包含4个单元格")

        for row in shard_rows:
            if not isinstance(row, dict):
                raise ValueError(f"正式种子分片 {shard_path.name} 含非法行")
            scenario_id = row.get("scenario")
            row_seed = row.get("seed")
            cell = (row_seed, scenario_id)
            if row_seed != seed or scenario_id not in scenarios_by_id:
                raise ValueError(f"正式种子分片含非法单元格：{cell}")
            typed_cell = (seed, str(scenario_id))
            if typed_cell in rows_by_cell:
                raise ValueError(f"正式种子分片含重复单元格：{typed_cell}")
            budget = _finite_row_number(
                row,
                "effective_time_limit_seconds",
                cell=typed_cell,
            )
            expected_budget = expected_budgets[str(scenario_id)]
            if not math.isclose(
                budget,
                expected_budget,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise ValueError(
                    f"分片单元格 {typed_cell} 的预算应为 {expected_budget:g}s"
                )

            expected_solution_file = (
                f"run_solutions/{scenario_id}__seed_{seed}.json"
            )
            if row.get("solution_file") != expected_solution_file:
                raise ValueError(f"分片单元格 {typed_cell} 的解文件名不一致")
            solution_path = args.output_dir / expected_solution_file
            if not solution_path.is_file():
                raise ValueError(f"分片单元格 {typed_cell} 缺少解文件")
            if row.get("solution_sha256") != _sha256(solution_path):
                raise ValueError(f"分片单元格 {typed_cell} 的解文件哈希不一致")

            _validate_relay_phase_runtime(
                row,
                scenarios_by_id[str(scenario_id)],
                budget=expected_budget,
                safety_margin_seconds=args.safety_margin_seconds,
                cell=typed_cell,
            )
            rows_by_cell[typed_cell] = row

    expected_cells = {
        (seed, scenario["id"])
        for seed in DEFAULT_SEEDS
        for scenario in SCENARIOS
    }
    if set(rows_by_cell) != expected_cells or len(rows_by_cell) != 12:
        raise ValueError("正式种子分片未形成完整的12个单元格")

    ordered_rows: list[dict[str, Any]] = []
    for seed_index, seed in enumerate(DEFAULT_SEEDS):
        offset = seed_index % len(SCENARIOS)
        ordered = SCENARIOS[offset:] + SCENARIOS[:offset]
        ordered_rows.extend(
            rows_by_cell[(seed, scenario["id"])] for scenario in ordered
        )
    return ordered_rows


def _write_final_outputs(
    args: argparse.Namespace,
    rows: Sequence[dict[str, Any]],
    *,
    task_count: int,
) -> None:
    summaries = summarize(rows)
    manifest = build_manifest(args, task_count=task_count)
    _write_json_atomic(args.output_dir / "runs.json", rows)
    _write_json_atomic(args.output_dir / "summary.json", summaries)
    _write_json_atomic(args.output_dir / "manifest.json", manifest)
    _write_csv(args.output_dir / "runs.csv", rows)
    _write_csv(args.output_dir / "summary.csv", summaries)
    _write_report(args.output_dir / "REPORT.md", rows, summaries, manifest)


def _run_seed(
    tasks: Sequence[Task],
    args: argparse.Namespace,
    *,
    seed: int,
    seed_index: int,
) -> list[dict[str, Any]]:
    offset = seed_index % len(SCENARIOS)
    ordered = SCENARIOS[offset:] + SCENARIOS[:offset]
    rows: list[dict[str, Any]] = []
    for scenario in ordered:
        rows.append(
            run_scenario(
                tasks,
                scenario,
                seed=seed,
                output_dir=args.output_dir,
                input_path=args.input,
                drones=args.drones,
                max_tasks=args.max_tasks,
                base_seconds=args.base_seconds,
                bonus_seconds=args.bonus_seconds,
                safety_margin_seconds=args.safety_margin_seconds,
                max_iterations=args.max_iterations,
                candidate_limit=(
                    None if args.candidate_limit == 0 else args.candidate_limit
                ),
                relay_count=args.relay_count,
                relay_location_seed=args.relay_location_seed,
            )
        )
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    validate_formal_protocol(args)
    if len(set(args.seeds)) != len(args.seeds):
        raise ValueError("种子不得重复")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.finalize_shards:
        rows = _load_and_validate_seed_shards(args)
        _write_final_outputs(args, rows, task_count=args.tasks)
        print(f"分片汇总已写入 {args.output_dir}", flush=True)
        return 0

    if args.quick:
        args.tasks = min(args.tasks, 24)
        args.base_seconds = min(args.base_seconds, 1.0)
        args.bonus_seconds = min(args.bonus_seconds, 0.5)
        args.max_iterations = min(args.max_iterations, 1_000_000)
    tasks = load_tasks_csv(args.input)[: args.tasks]
    if len(tasks) != args.tasks:
        raise ValueError(f"输入仅有 {len(tasks)} 条任务，无法读取 {args.tasks} 条")
    if args.drones * args.max_tasks < len(tasks):
        raise ValueError("无人机数×单机任务上限不足以覆盖全部任务")

    if args.worker_seed is not None:
        seed_index = DEFAULT_SEEDS.index(args.worker_seed)
        rows = _run_seed(
            tasks,
            args,
            seed=args.worker_seed,
            seed_index=seed_index,
        )
        shard = _build_seed_shard_payload(
            args,
            seed=args.worker_seed,
            rows=rows,
        )
        shard_path = (
            args.output_dir / "shards" / f"seed_{args.worker_seed}.json"
        )
        _write_json_atomic(shard_path, shard)
        print(f"种子分片已写入 {shard_path}", flush=True)
        return 0

    rows: list[dict[str, Any]] = []
    for seed_index, seed in enumerate(args.seeds):
        rows.extend(
            _run_seed(
                tasks,
                args,
                seed=seed,
                seed_index=seed_index,
            )
        )

    _write_final_outputs(args, rows, task_count=len(tasks))
    print(f"结果已写入 {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
