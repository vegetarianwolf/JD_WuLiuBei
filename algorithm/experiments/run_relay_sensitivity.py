"""Run a paired synthetic-campus hub-count x handoff-time sensitivity study."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from uav_dispatch.relay import Hub, RelayProblem, TaskCountSemantics
from uav_dispatch.relay_alns import RelayALNSConfig, solve_relay_alns

try:
    from .generate_relay_scenarios import (
        SCENARIO_CODES,
        SyntheticScenario,
        generate_scenario,
    )
except ImportError:  # pragma: no cover - direct script execution
    from generate_relay_scenarios import (  # type: ignore[no-redef]
        SCENARIO_CODES,
        SyntheticScenario,
        generate_scenario,
    )


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "algorithm" / "results" / "relay_handoff" / "sensitivity"

RUN_FIELDS = (
    "scenario",
    "task_count",
    "seed",
    "method",
    "hub_count",
    "handoff_service_min",
    "hub_ids",
    "drone_count",
    "max_tasks_per_drone",
    "valid",
    "compliant",
    "on_time_count",
    "on_time_rate",
    "distance_km",
    "relay_count",
    "runtime_seconds",
    "iterations",
    "budget_seconds",
    "scenario_sha256",
)

SUMMARY_FIELDS = (
    "scenario",
    "task_count",
    "method",
    "hub_count",
    "handoff_service_min",
    "run_count",
    "valid_run_count",
    "on_time_count_mean",
    "on_time_count_min",
    "on_time_count_max",
    "on_time_rate_mean",
    "distance_km_mean",
    "distance_km_min",
    "distance_km_max",
    "runtime_seconds_mean",
    "relay_count_mean",
)

HEATMAP_FIELDS = (
    "scenario",
    "task_count",
    "hub_count",
    "handoff_service_min",
    "paired_seed_count",
    "valid_pair_count",
    "baseline_on_time_count_mean",
    "relay_on_time_count_mean",
    "delta_on_time_count_mean",
    "baseline_distance_km_mean",
    "relay_distance_km_mean",
    "delta_distance_km_mean",
    "lexicographic_wins",
    "lexicographic_ties",
    "lexicographic_losses",
    "relay_count_mean",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        type=str.upper,
        choices=SCENARIO_CODES,
        default=list(SCENARIO_CODES),
    )
    parser.add_argument("--tasks", nargs="+", type=int)
    parser.add_argument("--seeds", nargs="+", type=int)
    parser.add_argument("--hub-counts", nargs="+", type=int)
    parser.add_argument("--handoff-times", nargs="+", type=float)
    parser.add_argument("--max-tasks-per-drone", type=int, default=25)
    parser.add_argument("--candidate-limit", type=int)
    parser.add_argument("--task-sample-size", type=int)
    parser.add_argument("--base-search-fraction", type=float)
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--time-limit", type=float)
    parser.add_argument("--wall-safety-margin", type=float)
    parser.add_argument("--require-relay-iterations", action="store_true")
    parser.add_argument("--rerun-reason")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after-runs", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = _parser().parse_args(argv)
    defaults = (
        {
            "tasks": [8],
            "seeds": [2026081000],
            "hub_counts": [1, 2],
            "handoff_times": [0.0, 0.5],
            "time_limit": 0.15,
            "wall_safety_margin": 0.01,
            "candidate_limit": 4,
            "task_sample_size": 8,
            "base_search_fraction": 0.65,
            "max_iterations": 64,
        }
        if args.quick
        else {
            "tasks": [25, 50, 100, 200],
            "seeds": list(range(2026081000, 2026081010)),
            "hub_counts": [1, 2, 4, 8],
            "handoff_times": [0.0, 0.5, 1.0, 2.0],
            "time_limit": 30.0,
            "wall_safety_margin": 1.0,
            "candidate_limit": 4,
            "task_sample_size": 8,
            "base_search_fraction": 0.65,
            "max_iterations": 10_000,
        }
    )
    for name, value in defaults.items():
        if getattr(args, name) is None:
            setattr(args, name, value)
    return args


def _validate_args(args: argparse.Namespace) -> None:
    named_sequences = {
        "scenarios": args.scenarios,
        "tasks": args.tasks,
        "seeds": args.seeds,
        "hub-counts": args.hub_counts,
        "handoff-times": args.handoff_times,
    }
    for name, values in named_sequences.items():
        if not values or len(values) != len(set(values)):
            raise ValueError(f"{name} 不能为空且不能重复")
    if min(args.tasks) <= 0 or args.max_tasks_per_drone <= 0:
        raise ValueError("任务数和单机任务上限必须为正整数")
    if min(args.candidate_limit, args.task_sample_size) <= 0:
        raise ValueError("候选上限和任务样本数必须为正整数")
    if args.max_iterations < 0:
        raise ValueError("最大迭代数不能为负")
    if args.stop_after_runs is not None and args.stop_after_runs <= 0:
        raise ValueError("checkpoint stop 数必须为正整数")
    if not 0 < args.base_search_fraction < 1:
        raise ValueError("基础搜索时间占比必须位于 (0, 1)")
    if min(args.hub_counts) <= 0 or max(args.hub_counts) > 8:
        raise ValueError("hub-counts 必须位于 1..8")
    if min(args.handoff_times) < 0:
        raise ValueError("交接操作时长不能为负")
    if args.time_limit <= 0:
        raise ValueError("时间预算必须为正数")
    if not 0 <= args.wall_safety_margin < args.time_limit:
        raise ValueError("墙钟安全余量必须非负且小于时间预算")


def _scenario_payload(scenario: SyntheticScenario) -> dict[str, Any]:
    return {
        "scenario": scenario.code,
        "seed": scenario.seed,
        "distance_metric": "euclidean_coordinate_km_unclipped",
        "depot": [scenario.depot.x, scenario.depot.y],
        "tasks": [
            {
                "task_id": task.id,
                "pickup_x": task.pickup.x,
                "pickup_y": task.pickup.y,
                "delivery_x": task.delivery.x,
                "delivery_y": task.delivery.y,
                "deadline_min": task.deadline_min,
            }
            for task in scenario.tasks
        ],
        "candidate_hubs": [
            {"id": hub.id, "x": hub.point.x, "y": hub.point.y}
            for hub in scenario.candidate_hubs
        ],
    }


def _sha256_json(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_csv(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _persist_scenario(
    root: Path, scenario: SyntheticScenario, payload: dict[str, Any]
) -> None:
    directory = (
        root
        / scenario.code
        / f"n{len(scenario.tasks)}"
        / f"seed_{scenario.seed}"
    )
    _write_json(directory / "scenario.json", payload)
    _write_csv(
        directory / "tasks.csv",
        payload["tasks"],
        (
            "task_id",
            "pickup_x",
            "pickup_y",
            "delivery_x",
            "delivery_y",
            "deadline_min",
        ),
    )


def _select_hubs(scenario: SyntheticScenario, count: int) -> tuple[Hub, ...]:
    """Select a nested, spatially spread candidate prefix."""

    candidates = scenario.candidate_hubs
    if count > len(candidates):
        raise ValueError("请求的 hub 数超过场景候选点数")
    centroid_x = statistics.fmean(hub.point.x for hub in candidates)
    centroid_y = statistics.fmean(hub.point.y for hub in candidates)
    first = min(
        range(len(candidates)),
        key=lambda index: (
            math.hypot(
                candidates[index].point.x - centroid_x,
                candidates[index].point.y - centroid_y,
            ),
            index,
        ),
    )
    ordered = [first]
    remaining = set(range(len(candidates))) - {first}
    while remaining:
        next_index = max(
            remaining,
            key=lambda index: (
                min(
                    candidates[index].point.distance_to(candidates[chosen].point)
                    for chosen in ordered
                ),
                -index,
            ),
        )
        ordered.append(next_index)
        remaining.remove(next_index)
    return tuple(candidates[index] for index in ordered[:count])


def _solver_config(args: argparse.Namespace, seed: int) -> RelayALNSConfig:
    return RelayALNSConfig(
        max_iterations=args.max_iterations,
        time_limit_seconds=args.time_limit - args.wall_safety_margin,
        seed=seed,
        candidate_limit=args.candidate_limit,
        task_sample_size=args.task_sample_size,
        base_search_fraction=args.base_search_fraction,
    )


def _run_one(
    *,
    scenario: SyntheticScenario,
    scenario_sha256: str,
    args: argparse.Namespace,
    seed: int,
    hub_count: int,
    handoff_service_min: float,
) -> dict[str, Any]:
    problem = scenario.to_problem(max_tasks_per_drone=args.max_tasks_per_drone)
    baseline = hub_count == 0
    selected_hubs = () if baseline else _select_hubs(scenario, hub_count)
    relay_problem = RelayProblem(
        problem,
        hubs=selected_hubs,
        handoff_service_min=0.0 if baseline else handoff_service_min,
        task_count_semantics=TaskCountSemantics.PRIMARY_OWNER,
        max_handoffs_per_task=0 if baseline else 1,
    )
    result = solve_relay_alns(
        relay_problem,
        config=_solver_config(args, seed),
    )
    evaluation = result.evaluation
    return {
        "scenario": scenario.code,
        "task_count": len(scenario.tasks),
        "seed": seed,
        "method": "baseline" if baseline else "static-relay",
        "hub_count": hub_count,
        "handoff_service_min": 0.0 if baseline else handoff_service_min,
        "hub_ids": ";".join(hub.id for hub in selected_hubs),
        "drone_count": problem.drone_count,
        "max_tasks_per_drone": problem.max_tasks_per_drone,
        "valid": evaluation.valid,
        "compliant": result.runtime_seconds <= args.time_limit,
        "on_time_count": len(problem.tasks) - evaluation.score.late_count,
        "on_time_rate": evaluation.on_time_rate,
        "distance_km": evaluation.score.distance_km,
        "relay_count": evaluation.relay_count,
        "runtime_seconds": result.runtime_seconds,
        "iterations": result.iterations,
        "budget_seconds": args.time_limit,
        "scenario_sha256": scenario_sha256,
    }


def _summarize(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[
        tuple[str, int, str, int, float], list[Mapping[str, Any]]
    ] = defaultdict(list)
    for row in rows:
        key = (
            str(row["scenario"]),
            int(row["task_count"]),
            str(row["method"]),
            int(row["hub_count"]),
            float(row["handoff_service_min"]),
        )
        grouped[key].append(row)
    output: list[dict[str, Any]] = []
    for key, group in sorted(grouped.items()):
        on_time = [float(row["on_time_count"]) for row in group]
        rates = [float(row["on_time_rate"]) for row in group]
        distances = [float(row["distance_km"]) for row in group]
        runtimes = [float(row["runtime_seconds"]) for row in group]
        relays = [float(row["relay_count"]) for row in group]
        output.append(
            {
                "scenario": key[0],
                "task_count": key[1],
                "method": key[2],
                "hub_count": key[3],
                "handoff_service_min": key[4],
                "run_count": len(group),
                "valid_run_count": sum(bool(row["valid"]) for row in group),
                "on_time_count_mean": statistics.fmean(on_time),
                "on_time_count_min": min(on_time),
                "on_time_count_max": max(on_time),
                "on_time_rate_mean": statistics.fmean(rates),
                "distance_km_mean": statistics.fmean(distances),
                "distance_km_min": min(distances),
                "distance_km_max": max(distances),
                "runtime_seconds_mean": statistics.fmean(runtimes),
                "relay_count_mean": statistics.fmean(relays),
            }
        )
    return output


def _competition_key(row: Mapping[str, Any]) -> tuple[float, float]:
    return (-float(row["on_time_count"]), float(row["distance_km"]))


def _heatmap_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    baselines = {
        (str(row["scenario"]), int(row["task_count"]), int(row["seed"])): row
        for row in rows
        if row["method"] == "baseline"
    }
    grouped: dict[
        tuple[str, int, int, float],
        list[tuple[Mapping[str, Any], Mapping[str, Any]]],
    ] = defaultdict(list)
    for row in rows:
        if row["method"] != "static-relay":
            continue
        baseline = baselines[
            (str(row["scenario"]), int(row["task_count"]), int(row["seed"]))
        ]
        grouped[
            (
                str(row["scenario"]),
                int(row["task_count"]),
                int(row["hub_count"]),
                float(row["handoff_service_min"]),
            )
        ].append((baseline, row))

    output: list[dict[str, Any]] = []
    for key, pairs in sorted(grouped.items()):
        baseline_on_time = [float(left["on_time_count"]) for left, _ in pairs]
        relay_on_time = [float(right["on_time_count"]) for _, right in pairs]
        baseline_distance = [float(left["distance_km"]) for left, _ in pairs]
        relay_distance = [float(right["distance_km"]) for _, right in pairs]
        outcomes: list[int] = []
        for left, right in pairs:
            if _competition_key(right) < _competition_key(left):
                outcomes.append(-1)
            elif _competition_key(right) > _competition_key(left):
                outcomes.append(1)
            else:
                outcomes.append(0)
        output.append(
            {
                "scenario": key[0],
                "task_count": key[1],
                "hub_count": key[2],
                "handoff_service_min": key[3],
                "paired_seed_count": len(pairs),
                "valid_pair_count": sum(
                    bool(left["valid"]) and bool(right["valid"])
                    for left, right in pairs
                ),
                "baseline_on_time_count_mean": statistics.fmean(baseline_on_time),
                "relay_on_time_count_mean": statistics.fmean(relay_on_time),
                "delta_on_time_count_mean": statistics.fmean(
                    right - left
                    for left, right in zip(
                        baseline_on_time, relay_on_time, strict=True
                    )
                ),
                "baseline_distance_km_mean": statistics.fmean(baseline_distance),
                "relay_distance_km_mean": statistics.fmean(relay_distance),
                "delta_distance_km_mean": statistics.fmean(
                    right - left
                    for left, right in zip(
                        baseline_distance, relay_distance, strict=True
                    )
                ),
                "lexicographic_wins": outcomes.count(-1),
                "lexicographic_ties": outcomes.count(0),
                "lexicographic_losses": outcomes.count(1),
                "relay_count_mean": statistics.fmean(
                    float(right["relay_count"]) for _, right in pairs
                ),
            }
        )
    return output


def _colour(value: float, scale: float) -> str:
    if scale <= 0 or abs(value) <= 1e-12:
        return "#f7f7f7"
    intensity = min(1.0, abs(value) / scale)
    pale = round(247 - 92 * intensity)
    if value > 0:
        return f"rgb({pale},220,{pale})"
    return f"rgb(232,{pale},{pale})"


def _render_heatmap(rows: Sequence[Mapping[str, Any]], path: Path) -> None:
    panels = sorted({(str(row["scenario"]), int(row["task_count"])) for row in rows})
    hubs = sorted({int(row["hub_count"]) for row in rows})
    times = sorted({float(row["handoff_service_min"]) for row in rows})
    lookup = {
        (
            str(row["scenario"]),
            int(row["task_count"]),
            int(row["hub_count"]),
            float(row["handoff_service_min"]),
        ): float(row["delta_on_time_count_mean"])
        for row in rows
    }
    scale = max((abs(value) for value in lookup.values()), default=0.0)
    columns = min(2, max(1, len(panels)))
    panel_width = 420
    cell = 54
    panel_height = 125 + cell * len(times)
    rows_of_panels = math.ceil(len(panels) / columns)
    width = 40 + columns * panel_width
    height = 75 + rows_of_panels * panel_height + 55
    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}">'
        ),
        '<rect width="100%" height="100%" fill="white"/>',
        (
            '<style>text{font-family:Arial,sans-serif;fill:#222}'
            '.title{font-size:18px;font-weight:700}'
            '.panel{font-size:15px;font-weight:700}'
            '.axis{font-size:12px}.cell{font-size:12px;font-weight:700}'
            "</style>"
        ),
        (
            '<text class="title" x="20" y="28">'
            "Mean delta on-time count: relay - paired baseline</text>"
        ),
        (
            '<text class="axis" x="20" y="50">Green is better; red is '
            "worse. Distance is retained in the machine-readable table as "
            "the second objective.</text>"
        ),
    ]
    for panel_index, (scenario, task_count) in enumerate(panels):
        column = panel_index % columns
        panel_row = panel_index // columns
        x0 = 35 + column * panel_width
        y0 = 75 + panel_row * panel_height
        parts.append(
            f'<text class="panel" x="{x0}" y="{y0}">'
            f"{html.escape(scenario)} | N={task_count}</text>"
        )
        grid_x = x0 + 105
        grid_y = y0 + 35
        for x_index, hub_count in enumerate(hubs):
            x = grid_x + x_index * cell
            parts.append(
                '<text class="axis" text-anchor="middle" '
                f'x="{x + cell / 2}" y="{grid_y - 8}">{hub_count}</text>'
            )
        parts.append(
            '<text class="axis" text-anchor="middle" '
            f'x="{grid_x + len(hubs) * cell / 2}" y="{grid_y - 24}">'
            "hub count</text>"
        )
        for y_index, handoff_time in enumerate(times):
            y = grid_y + y_index * cell
            parts.append(
                '<text class="axis" text-anchor="end" '
                f'x="{grid_x - 8}" y="{y + cell / 2 + 4}">'
                f"{handoff_time:g}</text>"
            )
            for x_index, hub_count in enumerate(hubs):
                x = grid_x + x_index * cell
                value = lookup[(scenario, task_count, hub_count, handoff_time)]
                parts.append(
                    f'<rect x="{x}" y="{y}" width="{cell}" '
                    f'height="{cell}" fill="{_colour(value, scale)}" '
                    'stroke="#aaa"/>'
                )
                parts.append(
                    '<text class="cell" text-anchor="middle" '
                    f'x="{x + cell / 2}" y="{y + cell / 2 + 4}">'
                    f"{value:+.2f}</text>"
                )
        parts.append(
            '<text class="axis" transform="translate('
            f"{x0 + 15},{grid_y + len(times) * cell / 2}) rotate(-90)\" "
            'text-anchor="middle">handoff time (min)</text>'
        )
    parts.append("</svg>\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(parts), encoding="utf-8")
    temporary.replace(path)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checkpoint_config(args: argparse.Namespace) -> dict[str, Any]:
    source_files = {
        "runner": Path(__file__).resolve(),
        "generator": Path(generate_scenario.__code__.co_filename).resolve(),
        "relay": ROOT / "algorithm" / "src" / "uav_dispatch" / "relay.py",
        "relay_alns": (
            ROOT / "algorithm" / "src" / "uav_dispatch" / "relay_alns.py"
        ),
        "relay_search": (
            ROOT / "algorithm" / "src" / "uav_dispatch" / "relay_search.py"
        ),
        "relay_validation": (
            ROOT
            / "algorithm"
            / "src"
            / "uav_dispatch"
            / "relay_validation.py"
        ),
    }
    return {
        "experiment": "synthetic-campus-relay-sensitivity",
        "experiment_stage": "development_sensitivity",
        "scenarios": list(args.scenarios),
        "task_counts": list(args.tasks),
        "seeds": list(args.seeds),
        "hub_counts": list(args.hub_counts),
        "handoff_service_minutes": list(args.handoff_times),
        "max_tasks_per_drone": args.max_tasks_per_drone,
        "time_limit_seconds": args.time_limit,
        "wall_safety_margin_seconds": args.wall_safety_margin,
        "max_iterations": args.max_iterations,
        "candidate_limit": args.candidate_limit,
        "task_sample_size": args.task_sample_size,
        "base_search_fraction": args.base_search_fraction,
        "require_relay_iterations": bool(args.require_relay_iterations),
        "task_count_semantics": TaskCountSemantics.PRIMARY_OWNER.value,
        "source_sha256": {
            name: _file_sha256(path) for name, path in source_files.items()
        },
    }


def _run_key(
    code: str,
    task_count: int,
    seed: int,
    hub_count: int,
    handoff_time: float,
) -> str:
    return (
        f"{code}|N={task_count}|seed={seed}|H={hub_count}|"
        f"tau={handoff_time:g}"
    )


def _checkpoint_rows(
    output_dir: Path,
    args: argparse.Namespace,
) -> tuple[Path, str, dict[str, dict[str, Any]]]:
    checkpoint = output_dir / ".sensitivity_checkpoint"
    manifest_path = checkpoint / "manifest.json"
    runs_dir = checkpoint / "runs"
    config = _checkpoint_config(args)
    signature = _sha256_json(config)
    loaded: dict[str, dict[str, Any]] = {}
    if checkpoint.exists():
        if not args.resume:
            raise RuntimeError(
                "checkpoint 已存在；请使用 --resume 续跑或更换输出目录"
            )
        if not manifest_path.is_file():
            raise RuntimeError("checkpoint 缺少 manifest.json")
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
        if stored.get("configuration_sha256") != signature:
            raise RuntimeError(
                "checkpoint configuration signature mismatch: "
                f"stored={stored.get('configuration_sha256')} current={signature}"
            )
        for path in sorted(runs_dir.glob("run_*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("configuration_sha256") != signature:
                raise RuntimeError(f"checkpoint run 签名不一致: {path.name}")
            run_key = str(payload.get("run_key", ""))
            row = payload.get("row")
            if not run_key or not isinstance(row, dict) or run_key in loaded:
                raise RuntimeError(f"checkpoint run 内容非法: {path.name}")
            loaded[run_key] = row
    else:
        runs_dir.mkdir(parents=True, exist_ok=False)
        _write_json(
            manifest_path,
            {
                "configuration_sha256": signature,
                "configuration": config,
                "checkpoint_policy": {
                    "on_failure": "retain_for_matching_signature_resume",
                    "on_success": "delete_after_consolidated_publication",
                    "write_granularity": "one_atomic_json_file_per_run",
                },
            },
        )
    return checkpoint, signature, loaded


def _resume_acceptable(
    row: Mapping[str, Any], args: argparse.Namespace
) -> bool:
    if not bool(row.get("valid")) or not bool(row.get("compliant")):
        return False
    return not (
        args.require_relay_iterations
        and row.get("method") == "static-relay"
        and int(row.get("iterations", 0)) == 0
    )


def _cleanup_checkpoint(checkpoint: Path) -> None:
    runs_dir = checkpoint / "runs"
    for path in runs_dir.glob("run_*.json"):
        path.unlink()
    for path in runs_dir.glob("*.tmp"):
        path.unlink()
    runs_dir.rmdir()
    (checkpoint / "manifest.json").unlink()
    manifest_tmp = checkpoint / "manifest.json.tmp"
    if manifest_tmp.exists():
        manifest_tmp.unlink()
    checkpoint.rmdir()


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Execute a complete paired matrix, then materialize all tables and SVG."""

    _validate_args(args)
    output_dir: Path = args.output_dir
    checkpoint, configuration_sha256, saved_rows = _checkpoint_rows(
        output_dir, args
    )
    rows: list[dict[str, Any]] = []
    resumed_run_count = 0
    new_run_count = 0
    matrix_size = sum(
        len(args.seeds) * (1 + len(args.hub_counts) * len(args.handoff_times))
        for _ in args.scenarios
        for _ in args.tasks
    )
    for code in args.scenarios:
        for task_count in args.tasks:
            for seed in args.seeds:
                scenario = generate_scenario(code, task_count=task_count, seed=seed)
                scenario_payload = _scenario_payload(scenario)
                scenario_sha256 = _sha256_json(scenario_payload)
                _persist_scenario(output_dir / "scenarios", scenario, scenario_payload)
                configurations: Iterable[tuple[int, float]] = (
                    [(0, 0.0)]
                    + [
                        (hub_count, handoff_time)
                        for hub_count in args.hub_counts
                        for handoff_time in args.handoff_times
                    ]
                )
                for hub_count, handoff_time in configurations:
                    index = len(rows) + 1
                    run_key = _run_key(
                        code, task_count, seed, hub_count, handoff_time
                    )
                    saved = saved_rows.get(run_key)
                    if saved is not None and _resume_acceptable(saved, args):
                        row = saved
                        resumed_run_count += 1
                        action = "resume"
                    else:
                        row = _run_one(
                            scenario=scenario,
                            scenario_sha256=scenario_sha256,
                            args=args,
                            seed=seed,
                            hub_count=hub_count,
                            handoff_service_min=handoff_time,
                        )
                        _write_json(
                            checkpoint / "runs" / f"run_{index:04d}.json",
                            {
                                "configuration_sha256": configuration_sha256,
                                "run_key": run_key,
                                "row": row,
                            },
                        )
                        new_run_count += 1
                        action = "run"
                    rows.append(row)
                    print(
                        f"[{action} {len(rows)}/{matrix_size}] {code} "
                        f"N={task_count} seed={seed} "
                        f"H={hub_count} tau={handoff_time:g} on_time={row['on_time_count']} "
                        f"distance={row['distance_km']:.3f} relay={row['relay_count']}",
                        flush=True,
                    )
                    if (
                        args.stop_after_runs is not None
                        and new_run_count >= args.stop_after_runs
                    ):
                        raise RuntimeError("checkpoint stop requested")

    summaries = _summarize(rows)
    heatmap = _heatmap_rows(rows)
    relay_rows = [row for row in rows if row["method"] == "static-relay"]
    zero_iteration_rows = [
        row for row in relay_rows if int(row["iterations"]) == 0
    ]
    zero_iteration_relay_runs = len(zero_iteration_rows)
    if args.require_relay_iterations and zero_iteration_relay_runs:
        rendered = ", ".join(
            _run_key(
                str(row["scenario"]),
                int(row["task_count"]),
                int(row["seed"]),
                int(row["hub_count"]),
                float(row["handoff_service_min"]),
            )
            for row in zero_iteration_rows[:8]
        )
        raise RuntimeError(
            "relay iteration quality gate failed: "
            f"{zero_iteration_relay_runs}/{len(relay_rows)} runs stopped at 0; "
            f"keys={rendered}"
        )
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": "synthetic-campus-relay-sensitivity",
        "experiment_stage": "development_sensitivity",
        "official_240s_comparable": False,
        "scenarios": list(args.scenarios),
        "task_counts": list(args.tasks),
        "seeds": list(args.seeds),
        "hub_counts": list(args.hub_counts),
        "handoff_service_minutes": list(args.handoff_times),
        "distance_metric": "euclidean_coordinate_km_unclipped",
        "objective_order": ["on_time_count", "distance_km"],
        "task_count_semantics": TaskCountSemantics.PRIMARY_OWNER.value,
        "semantics_extension": True,
        "max_handoffs_per_task": 1,
        "fleet_rule": "max(2, ceil(task_count / max_tasks_per_drone))",
        "max_tasks_per_drone": args.max_tasks_per_drone,
        "capacity": 2,
        "speed_km_per_min": 0.9,
        "budget_seconds": args.time_limit,
        "effective_search_time_limit_seconds": (
            args.time_limit - args.wall_safety_margin
        ),
        "wall_safety_margin_seconds": args.wall_safety_margin,
        "quick": bool(args.quick),
        "run_count": len(rows),
        "baseline_run_count": sum(row["method"] == "baseline" for row in rows),
        "relay_run_count": sum(row["method"] == "static-relay" for row in rows),
        "valid_run_count": sum(bool(row["valid"]) for row in rows),
        "compliant_run_count": sum(bool(row["compliant"]) for row in rows),
        "relay_solver": "adaptive-operator-weighted-relay-alns-with-sa",
        "relay_solver_config": {
            "max_iterations": args.max_iterations,
            "candidate_limit": args.candidate_limit,
            "task_sample_size": args.task_sample_size,
            "base_search_fraction": args.base_search_fraction,
        },
        "quality_gate": {
            "all_runs_valid": all(bool(row["valid"]) for row in rows),
            "all_runs_runtime_compliant": all(
                bool(row["compliant"]) for row in rows
            ),
            "relay_iterations_required": bool(
                args.require_relay_iterations
            ),
            "zero_iteration_relay_runs": zero_iteration_relay_runs,
            "relay_iteration_gate_passed": (
                zero_iteration_relay_runs == 0
            ),
        },
        "rerun_reason": args.rerun_reason,
        "checkpoint": {
            "configuration_sha256": configuration_sha256,
            "resumed_run_count": resumed_run_count,
            "new_run_count": new_run_count,
            "partial_on_failure": "retained_for_matching_signature_resume",
            "cleanup": "deleted_after_successful_consolidation",
        },
    }
    payload = {
        "manifest": manifest,
        "runs": rows,
        "summaries": summaries,
        "heatmap": heatmap,
    }
    _write_csv(output_dir / "sensitivity_runs.csv", rows, RUN_FIELDS)
    _write_csv(output_dir / "sensitivity_summary.csv", summaries, SUMMARY_FIELDS)
    _write_csv(output_dir / "sensitivity_heatmap.csv", heatmap, HEATMAP_FIELDS)
    _write_json(output_dir / "sensitivity_results.json", payload)
    _render_heatmap(heatmap, output_dir / "handoff_time_x_hub_count.svg")
    _cleanup_checkpoint(checkpoint)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    run(_parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
