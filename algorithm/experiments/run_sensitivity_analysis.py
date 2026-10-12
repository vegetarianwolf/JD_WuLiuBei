"""Four-strategy one-factor sensitivity with a fixed 300 s per solve.

Fleet size and deadlines apply to all four strategies. Station count applies
only to strategies using relay stations or station predeployment. Identical
baseline cells reuse one freshly generated solve within the same invocation.
Checkpoints can resume only under the same source/configuration fingerprints.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from uav_dispatch import Task, load_tasks_csv

from algorithm.experiments.run_scenario_comparison import (
    DEFAULT_INPUT,
    SCENARIOS,
    _config as scenario_alns_config,
    _environment_manifest,
    _git_state,
    _optional_sha256,
    _sha256,
    _solver_source_sha256,
    run_scenario,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "algorithm" / "results" / "final_sensitivity"
DEFAULT_SEED = 2026081701
FORMAL_PROTOCOL_CONFIG: dict[str, int | float] = {
    "seed": DEFAULT_SEED,
    "tasks": 200,
    "base_seconds": 240.0,
    "bonus_seconds": 60.0,
    "safety_margin_seconds": 0.05,
    "max_iterations": 10_000_000,
    "candidate_limit": 48,
}

SENSITIVITY_LEVELS: dict[str, tuple[int | float, ...]] = {
    "drone_count": (8, 9, 10, 12, 16),
    "deadline_multiplier": (0.8, 0.9, 1.0, 1.1, 1.2),
    "station_count": (2, 3, 4, 5, 6),
}
SENSITIVITY_RELAY_WARMUP_FRACTION = 0.80
SENSITIVITY_SCENARIOS = tuple(
    {
        **scenario,
        "warmup": SENSITIVITY_RELAY_WARMUP_FRACTION if scenario["relay"] else 0.0,
    }
    for scenario in SCENARIOS
)
PARAMETER_APPLICABILITY = {
    scenario["id"]: tuple(
        parameter
        for parameter in SENSITIVITY_LEVELS
        if parameter != "station_count" or scenario["id"] != "pure_direct"
    )
    for scenario in SENSITIVITY_SCENARIOS
}


def scale_deadlines(
    tasks: Sequence[Task], multiplier: float
) -> tuple[Task, ...]:
    """Return the same requests with uniformly scaled latest-delivery times."""

    if multiplier <= 0:
        raise ValueError("截止期倍率必须为正数")
    return tuple(
        Task(
            task.id,
            task.pickup,
            task.delivery,
            task.deadline_min * multiplier,
        )
        for task in tasks
    )


def _levels(quick: bool) -> dict[str, tuple[int | float, ...]]:
    if not quick:
        return SENSITIVITY_LEVELS
    return {
        name: (values[0], values[len(values) // 2], values[-1])
        for name, values in SENSITIVITY_LEVELS.items()
    }


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    scalar = [
        {
            key: value
            for key, value in row.items()
            if not isinstance(value, (dict, list, tuple))
        }
        for row in rows
    ]
    if not scalar:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(scalar[0]))
        writer.writeheader()
        writer.writerows(scalar)


def _write_report(
    path: Path, rows: Sequence[dict[str, Any]], *, formal: bool, budget_seconds: float,
) -> None:
    lines = [
        "# 四策略灵敏度分析" + ("" if formal else "（快速检查，非正式结果）"),
        "",
        "四种策略采用同一随机种子和（逾期任务数、总逾期时间、总航程）三级词典序目标。"
        f"本次每次独立求解均为 {budget_seconds:g} s 总预算；两种 Relay 策略的阶段占比均为 80% / 20%。",
        "每个配置仅采用一个随机种子（n=1），属于探索性比较，不提供置信区间或统计显著性结论。",
        "纯 Direct 不使用站点，因此站点数量分析不适用。相同策略的基准点在不同参数轴中"
        "复用同一个本次生成的解；这些重复展示不构成独立重复试验。",
        "",
        "| 策略 | 参数 | 水平 | 无人机 | 截止期倍率 | 站点 | 逾期数 | 总逾期/min | 航程/km | Relay任务 | 复用来源 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['scenario_label']} | {row['parameter']} | {row['level']} | "
            f"{row['drone_count']} | {row['deadline_multiplier']} | "
            f"{row['requested_station_count']} | {row['late_count']} | "
            f"{row['total_lateness_min']:.3f} | {row['distance_km']:.3f} | "
            f"{row['relay_task_count']} | {row['reused_from'] or '—'} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--tasks", type=int, default=200)
    parser.add_argument("--base-seconds", type=float, default=240.0)
    parser.add_argument("--bonus-seconds", type=float, default=60.0)
    parser.add_argument("--safety-margin-seconds", type=float, default=0.05)
    parser.add_argument("--max-iterations", type=int, default=10_000_000)
    parser.add_argument("--candidate-limit", type=int, default=48)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume source/configuration-matched checkpoints after verifying solution hashes",
    )
    return parser


def validate_formal_protocol(args: argparse.Namespace) -> None:
    """Reject a non-final configuration unless the run is marked quick."""

    if args.quick:
        return
    mismatches = [
        f"{name}={getattr(args, name)!r}（应为 {expected!r}）"
        for name, expected in FORMAL_PROTOCOL_CONFIG.items()
        if getattr(args, name) != expected
    ]
    if not args.input.is_file():
        mismatches.append(f"input={args.input!s}（文件不存在）")
    elif hashlib.sha256(args.input.read_bytes()).digest() != hashlib.sha256(
        DEFAULT_INPUT.read_bytes()
    ).digest():
        mismatches.append("input_sha256 与官方数据不一致")
    if mismatches:
        raise ValueError("正式敏感性协议参数不可覆盖：" + "；".join(mismatches))


def build_manifest(
    args: argparse.Namespace,
    *,
    task_count: int,
) -> dict[str, Any]:
    """Build the versioned, self-auditing sensitivity manifest."""

    candidate_limit = (
        None if args.candidate_limit == 0 else args.candidate_limit
    )
    effective_seconds = args.base_seconds + args.bonus_seconds
    alns_by_scenario = {}
    for scenario in SENSITIVITY_SCENARIOS:
        config = asdict(
            scenario_alns_config(
                scenario,
                seed=args.seed,
                solve_seconds=effective_seconds,
                max_iterations=args.max_iterations,
                candidate_limit=candidate_limit,
                home_aware=scenario["drone_homes"] == "stations",
            )
        )
        config.pop("seed")
        config.pop("time_limit_seconds")
        config["scenario_relay"] = bool(scenario["relay"])
        alns_by_scenario[scenario["id"]] = config
    cells = build_cells(args)
    git_commit, git_dirty = _git_state()
    environment = _environment_manifest()
    scenario_script = (
        ROOT / "algorithm" / "experiments" / "run_scenario_comparison.py"
    )
    return {
        "schema_version": 2,
        "protocol": "four_strategy_one_factor_sensitivity",
        "formal": not args.quick,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "experiment_script_sha256": _sha256(Path(__file__)),
        "scenario_script_sha256": _sha256(scenario_script),
        "solver_source_sha256": _solver_source_sha256(),
        "pyproject_sha256": _optional_sha256(ROOT / "pyproject.toml"),
        "requirements_dev_sha256": _optional_sha256(
            ROOT / "requirements-dev.txt"
        ),
        "environment": environment,
        "python": environment["python_version"],
        "platform": environment["platform"],
        "input": str(args.input),
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "tasks": task_count,
        "seed": args.seed,
        "base_seconds": args.base_seconds,
        "bonus_seconds": args.bonus_seconds,
        "effective_seconds_per_run": effective_seconds,
        "budget_policy": "fixed_total_override",
        "levels": _levels(args.quick),
        "scenarios": list(SENSITIVITY_SCENARIOS),
        "parameter_applicability": PARAMETER_APPLICABILITY,
        "not_applicable": {"pure_direct": {"station_count": "No relay or station predeployment"}},
        "workers": args.workers,
        "planned_rows": len(cells),
        "planned_unique_solves": len({cell["run_id"] for cell in cells}),
        "reuse_policy": "identical baseline within scenario and this run only; no historical results",
        "scenario_stage_targets_seconds": {
            scenario["id"]: {
                "direct_search": effective_seconds * (scenario["warmup"] if scenario["relay"] else 1.0),
                "relay_search": effective_seconds * (1.0 - scenario["warmup"]) if scenario["relay"] else 0.0,
            }
            for scenario in SENSITIVITY_SCENARIOS
        },
        "solver_config": {
            "max_iterations": args.max_iterations,
            "candidate_limit": candidate_limit,
            "safety_margin_seconds": args.safety_margin_seconds,
            "max_tasks_per_drone": 25,
            "capacity": 2,
            "speed_km_per_min": 0.9,
            "depot_km": [0.0, 0.0],
            "relay_candidates_per_task": 2,
            "relay_detour_ratio": 2.0,
            "relay_location_seed": 42,
            "relay_location_method": "weighted_kmedoids",
            "baseline": {
                "drone_count": 8,
                "deadline_multiplier": 1.0,
                "station_count": 4,
            },
            "time_limit_policy": (
                f"fixed {effective_seconds:g} s total override - initial "
                "construction - safety margin; "
                "exact per-run limit is stored in solution metadata"
            ),
            "input_transform": (
                "official task prefix; deadline_min multiplied by each "
                "runs[].deadline_multiplier"
            ),
            "alns_by_scenario": alns_by_scenario,
        },
    }


def build_cells(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Enumerate every applicable cell and its canonical within-run solve."""

    cells = []
    sources: dict[str, dict[str, Any]] = {}
    for scenario in SENSITIVITY_SCENARIOS:
        for parameter in PARAMETER_APPLICABILITY[scenario["id"]]:
            for level in _levels(args.quick)[parameter]:
                drones = int(level) if parameter == "drone_count" else 8
                deadline = float(level) if parameter == "deadline_multiplier" else 1.0
                stations = int(level) if parameter == "station_count" else 4
                if scenario["id"] == "pure_direct":
                    stations = 0
                cell_id = f"{scenario['id']}__{parameter}__{level}"
                run_id = (
                    f"{scenario['id']}__seed_{args.seed}__drones_{drones}"
                    f"__deadline_{deadline:g}__stations_{stations}"
                )
                cell = {
                    "cell_id": cell_id,
                    "run_id": run_id,
                    "scenario": scenario["id"],
                    "parameter": parameter,
                    "level": level,
                    "drone_count": drones,
                    "deadline_multiplier": deadline,
                    "requested_station_count": stations,
                    "cell_output_dir": f"{scenario['id']}/{parameter}__{level}",
                    "reused_from": None,
                }
                if run_id in sources:
                    cell["cell_output_dir"] = sources[run_id]["cell_output_dir"]
                    cell["reused_from"] = sources[run_id]["cell_id"]
                else:
                    sources[run_id] = cell
                cells.append(cell)
    return cells


def _json_normalize(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _manifest_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    # Git status and timestamps can change while unrelated report work continues.
    # Actual source, input, dependency, environment and runtime config fingerprints
    # remain mandatory; concurrency is included because it affects wall-time runs.
    return {
        key: value for key, value in manifest.items()
        if key not in {"created_at_utc", "git_commit", "git_dirty"}
    }


def _contract_sha256(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(_manifest_contract(manifest), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _verify_row(
    row: dict[str, Any], cell: dict[str, Any], args: argparse.Namespace,
) -> None:
    for key, expected in cell.items():
        if row.get(key) != expected:
            raise ValueError(f"敏感性单元 {cell['cell_id']} 字段 {key} 不一致")
    scenario = next(s for s in SENSITIVITY_SCENARIOS if s["id"] == cell["scenario"])
    expected_fields = {
        "seed": args.seed,
        "relay": scenario["relay"],
        "station_predeployment": scenario["drone_homes"] == "stations",
        "station_count": cell["requested_station_count"],
        "effective_time_limit_seconds": args.base_seconds + args.bonus_seconds,
    }
    for key, expected in expected_fields.items():
        if row.get(key) != expected:
            raise ValueError(f"敏感性单元 {cell['cell_id']} 字段 {key} 不一致")
    if not scenario["relay"] and row.get("relay_task_count") != 0:
        raise ValueError(f"非 Relay 策略存在 Relay 任务：{cell['cell_id']}")
    solution_path = (args.output_dir / row["cell_output_dir"] / row["solution_file"]).resolve()
    if not solution_path.is_relative_to(args.output_dir.resolve()):
        raise ValueError("solution_file 必须位于当前输出目录")
    if not solution_path.is_file() or _sha256(solution_path) != row.get("solution_sha256"):
        raise ValueError(f"原始解校验失败：{solution_path}")


def validate_rows(rows: Sequence[dict[str, Any]], args: argparse.Namespace) -> None:
    """Reject missing/duplicate/inapplicable cells and incorrect mechanisms."""

    cells = build_cells(args)
    expected = {cell["cell_id"]: cell for cell in cells}
    ids = [row.get("cell_id") for row in rows]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ValueError("敏感性单元缺失、重复或包含不适用组合")
    canonical = {row["run_id"]: row for row in rows if row["reused_from"] is None}
    for row in rows:
        _verify_row(row, expected[row["cell_id"]], args)
        if row["reused_from"] is not None:
            source = canonical[row["run_id"]]
            differing = {"cell_id", "parameter", "level", "reused_from"}
            if any(row[key] != value for key, value in source.items() if key not in differing):
                raise ValueError(f"基准解复用不一致：{row['cell_id']}")


def _run_cell(
    args: argparse.Namespace, source_tasks: Sequence[Task], cell: dict[str, Any],
) -> dict[str, Any]:
    scenario = next(s for s in SENSITIVITY_SCENARIOS if s["id"] == cell["scenario"])
    print(f"[sensitivity] {cell['cell_id']}", flush=True)
    row = run_scenario(
        scale_deadlines(source_tasks, cell["deadline_multiplier"]),
        scenario,
        seed=args.seed,
        output_dir=args.output_dir / cell["cell_output_dir"],
        input_path=args.input,
        drones=cell["drone_count"],
        max_tasks=25,
        base_seconds=args.base_seconds,
        bonus_seconds=args.bonus_seconds,
        safety_margin_seconds=args.safety_margin_seconds,
        effective_seconds_override=args.base_seconds + args.bonus_seconds,
        max_iterations=args.max_iterations,
        candidate_limit=None if args.candidate_limit == 0 else args.candidate_limit,
        relay_count=cell["requested_station_count"],
        relay_location_seed=42,
    )
    row.update(cell)
    _verify_row(row, cell, args)
    return row


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    validate_formal_protocol(args)
    if args.workers < 1:
        raise ValueError("workers 必须为正整数")
    if args.quick:
        args.tasks = min(args.tasks, 24)
        args.base_seconds = min(args.base_seconds, 0.4)
        args.bonus_seconds = min(args.bonus_seconds, 0.2)
    source_tasks = load_tasks_csv(args.input)[:args.tasks]
    if len(source_tasks) != args.tasks:
        raise ValueError("输入任务数量不足")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = _json_normalize(build_manifest(args, task_count=len(source_tasks)))
    manifest_path = args.output_dir / "manifest.json"
    if args.resume:
        if not manifest_path.is_file():
            raise ValueError("恢复运行需要已有 manifest.json")
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if _manifest_contract(previous) != _manifest_contract(manifest):
            raise ValueError("恢复运行的配置或来源指纹与 manifest 不一致")
        manifest = previous
    else:
        if any(args.output_dir.iterdir()):
            raise ValueError("输出目录非空；请使用新目录，或用 --resume 恢复匹配的检查点")
        _write_json(manifest_path, manifest)
    fingerprint = _contract_sha256(manifest)
    cells = build_cells(args)
    unique = {cell["run_id"]: cell for cell in cells if cell["reused_from"] is None}
    checkpoint_dir = args.output_dir / "checkpoints"
    checkpoint_dir.mkdir(exist_ok=True)
    completed = {}
    for checkpoint in sorted(checkpoint_dir.glob("*.json")):
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        run_id = payload.get("run_id")
        if payload.get("contract_sha256") != fingerprint or run_id not in unique or run_id in completed:
            raise ValueError(f"检查点来源、单元或重复性校验失败：{checkpoint}")
        row = payload["row"]
        _verify_row(row, unique[run_id], args)
        completed[run_id] = row

    def record(row: dict[str, Any]) -> None:
        run_id = row["run_id"]
        _write_json(checkpoint_dir / f"{run_id}.json", {
            "schema_version": 1, "contract_sha256": fingerprint,
            "run_id": run_id, "row": row,
        })
        completed[run_id] = row
        print(f"[sensitivity] completed {len(completed)}/{len(unique)} unique solves", flush=True)

    pending = [cell for run_id, cell in unique.items() if run_id not in completed]
    if args.workers == 1:
        for cell in pending:
            record(_run_cell(args, source_tasks, cell))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(_run_cell, args, source_tasks, cell) for cell in pending]
            for future in as_completed(futures):
                record(future.result())
    rows = [{**completed[cell["run_id"]], **cell} for cell in cells]
    validate_rows(rows, args)
    _write_json(args.output_dir / "runs.json", rows)
    _write_csv(args.output_dir / "runs.csv", rows)
    _write_report(
        args.output_dir / "REPORT.md", rows, formal=not args.quick,
        budget_seconds=args.base_seconds + args.bonus_seconds,
    )
    print(f"结果已写入 {args.output_dir}（{len(rows)} 行 / {len(unique)} 次独立求解）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
