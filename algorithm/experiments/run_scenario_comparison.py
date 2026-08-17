"""Run the final four-scenario comparison on the official 200-task data.

Protocol fixed by the final chat adjustment: the Direct baseline receives
240 s; every scenario using relay service or station predeployment receives
one additional 60 s allowance; the same three seeds are paired across all
four scenarios; and construction time is included in each allowance.

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
    "bonus_seconds": 60.0,
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
        "warmup": 0.80,
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
        "warmup": 0.80,
        "drone_homes": "stations",
    },
)


def scenario_time_limit_seconds(
    scenario: dict[str, Any],
    *,
    base_seconds: float,
    relay_or_station_bonus_seconds: float,
) -> float:
    """Return the final protocol's effective wall-clock allowance."""

    if not math.isfinite(base_seconds) or base_seconds <= 0:
        raise ValueError("基础墙钟预算必须是有限正数")
    if (
        not math.isfinite(relay_or_station_bonus_seconds)
        or relay_or_station_bonus_seconds < 0
    ):
        raise ValueError("Relay/Station 加时必须是有限非负数")
    uses_extension = bool(scenario["relay"]) or (
        scenario["drone_homes"] == "stations"
    )
    return base_seconds + (
        relay_or_station_bonus_seconds if uses_extension else 0.0
    )


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
            "| 场景 | seed | 逾期数 | 总逾期/min | 航程/km | Relay任务 | 墙钟/s |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row['scenario']} | {row['seed']} | {row['late_count']} | "
            f"{row['total_lateness_min']:.3f} | {row['distance_km']:.3f} | "
            f"{row['relay_task_count']} | {row['wall_seconds']:.1f} |"
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
    parser.add_argument("--bonus-seconds", type=float, default=60.0)
    parser.add_argument("--safety-margin-seconds", type=float, default=0.05)
    parser.add_argument("--max-iterations", type=int, default=10_000_000)
    parser.add_argument("--candidate-limit", type=int, default=48)
    parser.add_argument("--relay-count", type=_parse_relay_count, default="auto")
    parser.add_argument("--relay-location-seed", type=int, default=42)
    parser.add_argument(
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
        "scenario_budgets_seconds": {
            scenario["id"]: scenario_time_limit_seconds(
                scenario,
                base_seconds=args.base_seconds,
                relay_or_station_bonus_seconds=args.bonus_seconds,
            )
            for scenario in SCENARIOS
        },
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    validate_formal_protocol(args)
    if len(set(args.seeds)) != len(args.seeds):
        raise ValueError("种子不得重复")
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

    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for seed_index, seed in enumerate(args.seeds):
        offset = seed_index % len(SCENARIOS)
        ordered = SCENARIOS[offset:] + SCENARIOS[:offset]
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

    summaries = summarize(rows)
    manifest = build_manifest(args, task_count=len(tasks))
    (args.output_dir / "runs.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(args.output_dir / "runs.csv", rows)
    _write_csv(args.output_dir / "summary.csv", summaries)
    _write_report(args.output_dir / "REPORT.md", rows, summaries, manifest)
    print(f"结果已写入 {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
