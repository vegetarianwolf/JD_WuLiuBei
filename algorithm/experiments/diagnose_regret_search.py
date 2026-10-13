"""Diagnose the saved, paired 240 s regret experiment without running a solver.

All comparisons use fleet200 / confirm / seed 2026101321.  Checkpoints hold
forward the last recorded global best; they never interpolate objective values.
Run from the repository root with PYTHONPATH=algorithm/src.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem
from uav_dispatch.validation import evaluate_solution

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "algorithm/results/optimization_20261013"
DEFAULT_OUTPUT = ROOT / "algorithm/results/regret_followup_20261013/diagnostics"
SCORE_FIELDS = ("late_count", "total_lateness_min", "distance_km")
VARIANTS = ("baseline", "allocation")
TIME_CHECKPOINTS = (5, 10, 30, 60, 120, 180, 239)
ITERATION_CHECKPOINTS = (0, 200, 500, 1000, 2000, 4000, 6000, 8000, 10000, 12000, 14000, 16000)
SEED = 2026101321


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def checked_file(directory: Path, relative: str, expected_hash: str) -> Path:
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError(f"Missing or escaping evidence path: {relative}")
    if sha256(path) != expected_hash:
        raise ValueError(f"Evidence SHA256 mismatch: {relative}")
    return path


def close(actual: float, recorded: float, label: str) -> None:
    if not math.isfinite(recorded) or not math.isclose(actual, recorded, rel_tol=0, abs_tol=1e-8):
        raise ValueError(f"Independent recomputation disagrees with {label}")


def score(point: list) -> tuple:
    return tuple(point[2:])


def score_dict(values: tuple | list) -> dict[str, float]:
    return dict(zip(SCORE_FIELDS, values, strict=True))


def winner(baseline: tuple, candidate: tuple) -> str:
    return "allocation" if candidate < baseline else "baseline" if baseline < candidate else "tie"


def at_checkpoint(trajectory: list[list], checkpoint: float, axis: int) -> dict[str, Any]:
    index = bisect_right([p[axis] for p in trajectory], checkpoint) - 1
    if index < 0:
        raise ValueError("Checkpoint predates the saved initial solution")
    point = trajectory[index]
    return {
        "score": score_dict(score(point)),
        "last_best_update_iteration": point[0],
        "last_best_update_end_to_end_seconds": point[1],
        "last_best_update_age": checkpoint - point[axis],
    }


def alignment(rows: dict, checkpoints: list | tuple, axis: int) -> list[dict]:
    records = []
    for checkpoint in checkpoints:
        values = {variant: at_checkpoint(rows[variant]["best_trajectory"], checkpoint, axis) for variant in VARIANTS}
        scores = {variant: tuple(values[variant]["score"][field] for field in SCORE_FIELDS) for variant in VARIANTS}
        records.append({
            "checkpoint": checkpoint,
            "axis": "end_to_end_seconds" if axis == 1 else "iterations",
            **values,
            "allocation_minus_baseline": score_dict(tuple(c - b for b, c in zip(scores["baseline"], scores["allocation"], strict=True))),
            "lexicographic_winner": winner(scores["baseline"], scores["allocation"]),
        })
    return records


def dominance_intervals(rows: dict, axis: int, upper: float) -> list[dict]:
    trajectories = {variant: rows[variant]["best_trajectory"] for variant in VARIANTS}
    lower = max(points[0][axis] for points in trajectories.values())
    events = sorted({lower, upper, *(point[axis] for points in trajectories.values() for point in points if lower <= point[axis] <= upper)})
    intervals = []
    for left, right in zip(events, events[1:]):
        values = {variant: at_checkpoint(points, left, axis) for variant, points in trajectories.items()}
        scores = {variant: tuple(values[variant]["score"][field] for field in SCORE_FIELDS) for variant in VARIANTS}
        state = winner(scores["baseline"], scores["allocation"])
        if intervals and intervals[-1]["winner"] == state:
            intervals[-1]["end"] = right
        else:
            intervals.append({"start": left, "end": right, "winner": state,
                              "baseline_score_at_start": score_dict(scores["baseline"]),
                              "allocation_score_at_start": score_dict(scores["allocation"])})
    return intervals


def quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def distribution(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values), "min": min(values) if values else None,
        "q25": quantile(values, .25), "median": quantile(values, .5),
        "q75": quantile(values, .75), "q90": quantile(values, .9),
        "q95": quantile(values, .95), "max": max(values) if values else None,
        "mean": statistics.mean(values) if values else None,
        "sum": sum(values),
    }


def trajectory_diagnostics(row: dict) -> dict[str, Any]:
    points = row["best_trajectory"]
    primary_changes = []
    changes = {field: 0 for field in SCORE_FIELDS}
    changes["identical"] = 0
    for before, after in zip(points, points[1:]):
        for i, field in enumerate(SCORE_FIELDS, 2):
            if before[i] != after[i]:
                if after[i] >= before[i]:
                    raise ValueError("Recorded global-best trajectory deteriorates")
                changes[field] += 1
                if i == 2:
                    primary_changes.append({"iteration": after[0], "end_to_end_seconds": after[1],
                                            "late_count": after[2], "reduction": before[2] - after[2]})
                break
        else:
            changes["identical"] += 1
    gaps = [{"seconds": after[1] - before[1], "iteration_span": after[0] - before[0],
             "start_seconds": before[1], "end_seconds": after[1], "start_iteration": before[0],
             "end_iteration": after[0], "score_held": score_dict(score(before))}
            for before, after in zip(points, points[1:])]
    first_reach = {}
    for limit in (75, 70, 68, 67, 66, 65, 64, 63):
        point = next((p for p in points if p[2] <= limit), None)
        first_reach[str(limit)] = None if point is None else {"iteration": point[0], "end_to_end_seconds": point[1], "score": score_dict(score(point))}
    last_drop = primary_changes[-1]
    search_end = row["construction_seconds"] + row["search_seconds"]
    return {
        "best_update_count": len(points) - 1,
        "best_updates_by_first_improved_objective": changes,
        "late_count_improvements": primary_changes,
        "first_reach_at_most_late_count": first_reach,
        "longest_no_best_update_gap": max(gaps, key=lambda gap: gap["seconds"]),
        "last_late_count_improvement": last_drop,
        "seconds_without_further_late_count_improvement": search_end - last_drop["end_to_end_seconds"],
        "iterations_without_further_late_count_improvement": row["iterations"] - last_drop["iteration"],
        "final_best_update": {"iteration": points[-1][0], "end_to_end_seconds": points[-1][1], "score": score_dict(score(points[-1]))},
    }


def operator_diagnostics(row: dict, payload: dict) -> dict[str, Any]:
    metadata = payload["metadata"]
    iterations = row["iterations"]
    operators = {}
    for name, statistics_ in metadata["operator_statistics"].items():
        uses = statistics_["uses"]
        if metadata["operator_uses"][name] != uses:
            raise ValueError("Operator usage counters disagree")
        if not 0 <= statistics_["best_improvements"] <= statistics_["current_improvements"] <= statistics_["accepted"] <= uses:
            raise ValueError("Impossible operator success counters")
        operators[name] = {
            **statistics_, "usage_share_of_iterations": uses / iterations,
            "uses_per_search_second": uses / row["search_seconds"],
            "acceptance_rate": statistics_["accepted"] / uses if uses else None,
            "current_improvement_rate": statistics_["current_improvements"] / uses if uses else None,
            "best_improvement_rate": statistics_["best_improvements"] / uses if uses else None,
            "reward_per_use": statistics_["total_reward"] / uses if uses else None,
            "final_weight": metadata["operator_weights"][name],
        }
    families = {}
    for family in ("destroy", "repair"):
        relevant = [stats for name, stats in operators.items() if name.startswith(family + ":")]
        totals = {key: sum(stats[key] for stats in relevant) for key in ("uses", "accepted", "current_improvements", "best_improvements")}
        if totals["uses"] != iterations or totals["accepted"] != metadata["accepted_solutions"]:
            raise ValueError("Family operator counts do not match total iterations/accepted solutions")
        if totals["best_improvements"] != len(row["best_trajectory"]) - 1:
            raise ValueError("Best improvements do not match saved trajectory")
        families[family] = totals
    repair = families["repair"]
    return {
        "total_iterations": iterations, "search_seconds": row["search_seconds"],
        "iterations_per_search_second": iterations / row["search_seconds"],
        "iterations_per_end_to_end_second": iterations / row["end_to_end_seconds"],
        "accepted_solutions": metadata["accepted_solutions"],
        "acceptance_rate": metadata["accepted_solutions"] / iterations,
        "current_improvements": repair["current_improvements"],
        "current_improvement_rate": repair["current_improvements"] / iterations,
        "best_improvements": repair["best_improvements"],
        "best_improvement_rate": repair["best_improvements"] / iterations,
        "operators": operators, "family_totals": families,
        "profiling": {name: {"count": count, "per_iteration": count / iterations,
                             "per_search_second": count / row["search_seconds"]}
                      for name, count in row["profiling"].items()},
    }


def load_evidence(input_dir: Path) -> tuple[dict, dict, dict, dict, Problem]:
    manifest = read_json(input_dir / "manifest.json")
    raw_rows = read_json(input_dir / "runs.json")
    validation = read_json(input_dir / "validation.json")
    for key, filename in (("manifest_sha256", "manifest.json"), ("runs_sha256", "runs.json")):
        if validation.get(key) != sha256(input_dir / filename):
            raise ValueError(f"Independent validation is stale: {filename}")
    input_path = checked_file(ROOT, manifest["input_path"], manifest["input_sha256"])
    problem = Problem(tuple(load_tasks_csv(input_path)), drone_count=8, max_tasks_per_drone=25)
    if len(problem.tasks) != 200:
        raise ValueError("Expected complete official 200-task input")
    selected = [row for row in raw_rows if row["stage"] == "confirm" and row["case_id"] == "fleet200"
                and row["seed"] == SEED and row["variant"] in VARIANTS]
    if len(selected) != 2 or {row["variant"] for row in selected} != set(VARIANTS):
        raise ValueError("Expected exactly one saved 240-second run for each variant")
    rows = {row["variant"]: row for row in selected}
    payloads, evaluations, source_files = {}, {}, {}
    for name in ("manifest.json", "runs.json", "validation.json", "equivalence.json", "execution_notes.json"):
        source_files[name] = sha256(input_dir / name)
    for variant, row in rows.items():
        if row["scenario"] != "pure_direct" or row["budget_seconds"] != 240 or not row["valid"] or not row["budget_compliant"]:
            raise ValueError("Expected valid, within-budget original DIRECT cases")
        if row["end_to_end_seconds"] > 240 or row["task_count"] != 200 or row["drone_count"] != 8:
            raise ValueError("Saved problem/budget differs from intended paired comparison")
        snapshot = manifest["source_snapshots"][variant]
        checked_file(input_dir, snapshot["path"], snapshot["sha256"])
        if row["source_sha256"] != snapshot["sha256"]:
            raise ValueError("Run differs from its frozen solver snapshot")
        source_files[snapshot["path"]] = snapshot["sha256"]
        path = checked_file(input_dir, row["solution_file"], row["solution_sha256"])
        source_files[row["solution_file"]] = row["solution_sha256"]
        payload = read_json(path)
        if [route["drone_id"] for route in payload["routes"]] != list(range(1, 9)):
            raise ValueError("Saved drone IDs are missing, duplicated or reordered")
        routes = [route["encoded_visits"] for route in payload["routes"]]
        evaluated = evaluate_solution(problem, routes, _force_full=True)
        if not evaluated.valid or set(evaluated.delivery_times_min) != {task.id for task in problem.tasks}:
            raise ValueError("Independent full evaluation found incomplete or invalid routes")
        for field in SCORE_FIELDS:
            actual = getattr(evaluated.score, field)
            close(actual, row[field], f"row {variant}/{field}")
            close(actual, payload["score"][field], f"solution {variant}/{field}")
        if set(payload["delivery_times_min"]) != {str(task.id) for task in problem.tasks}:
            raise ValueError("Incomplete saved delivery-time map")
        for task_id, arrival in evaluated.delivery_times_min.items():
            close(arrival, payload["delivery_times_min"][str(task_id)], f"delivery time {variant}/{task_id}")
        points = row["best_trajectory"]
        metadata_points = payload["metadata"]["best_trajectory"]
        if len(points) != len(metadata_points) or payload["iterations"] != row["iterations"]:
            raise ValueError("Row and solution search traces disagree")
        for index, (point, recorded) in enumerate(zip(points, metadata_points, strict=True)):
            if point[0] != recorded[0] or point[2:] != recorded[2:]:
                raise ValueError("Row and solution global-best traces disagree")
            close(point[1], recorded[1] + row["construction_seconds"], "trajectory clock origin")
            if index and (point[0] < points[index - 1][0] or point[1] < points[index - 1][1] or score(point) > score(points[index - 1])):
                raise ValueError("Saved trajectory is not monotonically best")
        if score(points[-1]) != tuple(row[field] for field in SCORE_FIELDS):
            raise ValueError("Last trajectory score differs from final score")
        payloads[variant], evaluations[variant] = payload, evaluated
    if rows["baseline"]["initial_routes_sha256"] != rows["allocation"]["initial_routes_sha256"]:
        raise ValueError("The compared runs did not share initial routes")
    if score(rows["baseline"]["best_trajectory"][0]) != score(rows["allocation"]["best_trajectory"][0]):
        raise ValueError("Initial scores differ")
    for source in manifest["execution_sources"].values():
        checked_file(input_dir, source["path"], source["sha256"])
        source_files[source["path"]] = source["sha256"]
    provenance = {
        "input_directory": input_dir.relative_to(ROOT).as_posix() if input_dir.is_relative_to(ROOT) else str(input_dir),
        "source_files_sha256": source_files,
        "official_input_path": manifest["input_path"], "official_input_sha256": manifest["input_sha256"],
        "baseline_commit": manifest["baseline_commit"],
        "diagnostic_script_sha256": sha256(Path(__file__)),
        "validation_source_sha256": {path.name: sha256(path) for path in sorted((ROOT / "algorithm/src/uav_dispatch").glob("*.py"))},
        "independent_full_recomputation": {variant: {"valid": evaluations[variant].valid,
            "score": score_dict(tuple(getattr(evaluations[variant].score, field) for field in SCORE_FIELDS)),
            "completed_tasks": len(evaluations[variant].delivery_times_min)} for variant in VARIANTS},
    }
    return provenance, rows, payloads, evaluations, problem


def final_task_diagnostics(problem: Problem, payloads: dict, evaluations: dict) -> dict:
    drone_by_task = {variant: {abs(visit): route["drone_id"] for route in payloads[variant]["routes"]
                              for visit in route["encoded_visits"] if visit < 0} for variant in VARIANTS}
    tasks = []
    buckets = {"on_time_both": [], "late_both": [], "baseline_on_time_allocation_late": [], "baseline_late_allocation_on_time": []}
    summaries = {}
    for task in problem.tasks:
        item = {"task_id": task.id, "deadline_min": task.deadline_min}
        for variant in VARIANTS:
            delivery = evaluations[variant].delivery_times_min[task.id]
            lateness = max(0., delivery - task.deadline_min)
            item[variant] = {"drone_id": drone_by_task[variant][task.id], "delivery_time_min": delivery,
                             "lateness_min": lateness, "late": lateness > 1e-9,
                             "slack_min": task.deadline_min - delivery}
        b_late, c_late = item["baseline"]["late"], item["allocation"]["late"]
        category = "late_both" if b_late and c_late else "on_time_both" if not b_late and not c_late else "baseline_on_time_allocation_late" if c_late else "baseline_late_allocation_on_time"
        item["timeliness_transition"] = category
        item["lateness_delta_min"] = item["allocation"]["lateness_min"] - item["baseline"]["lateness_min"]
        item["delivery_time_delta_min"] = item["allocation"]["delivery_time_min"] - item["baseline"]["delivery_time_min"]
        buckets[category].append(task.id)
        tasks.append(item)
    for variant in VARIANTS:
        late = [task[variant]["lateness_min"] for task in tasks if task[variant]["late"]]
        all_lateness = [task[variant]["lateness_min"] for task in tasks]
        bins = {"on_time": sum(not task[variant]["late"] for task in tasks)}
        for label, lower, upper in (("late_(0,5]", 1e-9, 5), ("late_(5,15]", 5, 15),
                                    ("late_(15,30]", 15, 30), ("late_(30,60]", 30, 60),
                                    ("late_(60,+inf)", 60, math.inf)):
            bins[label] = sum(lower < value <= upper for value in all_lateness)
        summaries[variant] = {
            "late_only_distribution_min": distribution(late),
            "all_tasks_distribution_min": distribution(all_lateness),
            "lateness_bins": bins,
            "within_one_minute_of_deadline": [task["task_id"] for task in tasks if abs(task[variant]["slack_min"]) <= 1],
            "on_time_with_at_most_one_minute_slack": [task["task_id"] for task in tasks if not task[variant]["late"] and task[variant]["slack_min"] <= 1],
            "per_drone": [{key: route[key] for key in ("drone_id", "task_count", "late_count", "total_lateness_min", "distance_km", "completion_time_min")}
                          for route in payloads[variant]["routes"]],
            "distance_km": evaluations[variant].score.distance_km,
            "max_route_distance_km": evaluations[variant].max_route_distance_km,
        }
    transitions = {
        name: {"count": len(ids), "task_ids": ids,
               "baseline_lateness_min": sum(task["baseline"]["lateness_min"] for task in tasks if task["task_id"] in ids),
               "allocation_lateness_min": sum(task["allocation"]["lateness_min"] for task in tasks if task["task_id"] in ids),
               "lateness_delta_min": sum(task["lateness_delta_min"] for task in tasks if task["task_id"] in ids)}
        for name, ids in buckets.items()
    }
    return {"summaries": summaries, "timeliness_transitions": transitions, "tasks": tasks,
            "largest_lateness_increases": sorted(tasks, key=lambda t: t["lateness_delta_min"], reverse=True)[:10],
            "largest_lateness_reductions": sorted(tasks, key=lambda t: t["lateness_delta_min"])[:10],
            "route_identity_caveat": "All drones share the origin; drone IDs are exchangeable labels, so same-ID route differences are descriptive, not causal."}


def diagnose(input_dir: Path) -> dict:
    provenance, rows, payloads, evaluations, problem = load_evidence(input_dir)
    common_iterations = min(row["iterations"] for row in rows.values())
    checkpoints = sorted(set([value for value in ITERATION_CHECKPOINTS if value <= common_iterations] + [common_iterations]))
    operators = {variant: operator_diagnostics(rows[variant], payloads[variant]) for variant in VARIANTS}
    final_scores = {variant: {field: rows[variant][field] for field in SCORE_FIELDS} for variant in VARIANTS}
    differences = {field: {"absolute": final_scores["allocation"][field] - final_scores["baseline"][field],
                           "relative": final_scores["allocation"][field] / final_scores["baseline"][field] - 1}
                   for field in SCORE_FIELDS}
    result = {
        "schema_version": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {"stage": "confirm", "case_id": "fleet200", "seed": SEED, "budget_seconds": 240,
                  "variants": list(VARIANTS), "task_count": 200, "drone_count": 8,
                  "selection": "The only complete 240-second paired seed in the original batch; all conclusions are conditional on this saved pair."},
        "provenance": provenance,
        "method": {
            "no_search_performed": True,
            "alignment": "Stepwise last recorded global best at or before each time/iteration checkpoint; no linear interpolation.",
            "time_axis": "End-to-end clock from CSV read; source best-update solver time plus recorded construction_seconds.",
            "iteration_axis": "Same ALNS iteration index, limited to the smaller recorded iteration count; last best-update time is not the actual completion time of the checkpoint iteration.",
            "operator_success": "current_improvement_rate = accepted current improvements / uses; best_improvement_rate = global lexicographic best improvements / uses; neither isolates late-count improvements.",
            "operator_denominator": "Each iteration selects one destroy and one repair; analyze families separately, never sum both as independent observations.",
            "quantiles": "Linear interpolation on sorted samples at index (n-1)*p; late-only samples exclude lateness <=1e-9.",
        },
        "runs": {variant: {key: rows[variant][key] for key in ("run_id", "source_sha256", "initial_routes_sha256", "solution_file", "solution_sha256", "config", "construction_seconds", "search_seconds", "end_to_end_seconds", "iterations", "order")} for variant in VARIANTS},
        "time_checkpoints": alignment(rows, TIME_CHECKPOINTS, 1),
        "iteration_checkpoints": alignment(rows, checkpoints, 0),
        "common_iteration_limit": common_iterations,
        "dominance_intervals_seconds": dominance_intervals(rows, 1, min(row["end_to_end_seconds"] for row in rows.values())),
        "dominance_intervals_iterations": dominance_intervals(rows, 0, common_iterations),
        "trajectory_diagnostics": {variant: trajectory_diagnostics(rows[variant]) for variant in VARIANTS},
        "operator_diagnostics": operators,
        "operator_comparison": [{"operator": name,
            **{variant: operators[variant]["operators"][name] for variant in VARIANTS},
            "allocation_minus_baseline": {key: operators["allocation"]["operators"][name][key] - operators["baseline"]["operators"][name][key]
                for key in ("usage_share_of_iterations", "acceptance_rate", "current_improvement_rate", "best_improvement_rate", "reward_per_use", "final_weight")}}
            for name in operators["baseline"]["operators"]],
        "final_score": final_scores, "final_score_allocation_minus_baseline": differences,
        "final_task_diagnostics": final_task_diagnostics(problem, payloads, evaluations),
        "limitations": [
            "Only one completed long-budget paired seed; no population inference or significance claim.",
            "Allocation simultaneously enables lazy repair and route-distinct regret; this is not a regret-only causal contrast.",
            "Equal seeds do not preserve equal stochastic search paths after algorithm/weight choices diverge.",
            "Final operator counters and weights have no timestamps or joint destroy-repair counts; phase-specific operator effects and attribution are unidentified.",
            "Only global-best objective history is saved, not incumbent route/task histories, current-state history, rejected proposals or per-operator elapsed time.",
            "Same-iteration alignment controls completed iterations, not neighborhood evaluation count, task rank work, temperature path or solution-state distribution.",
            "Sequential run order is baseline then allocation for this seed; machine load/order and both enabled flags remain confounded.",
            "Final per-task comparisons do not identify which tasks or moves caused earlier late-count transitions.",
        ],
    }
    result["findings"] = findings(result)
    return result


def findings(data: dict) -> dict:
    b, c = (data["operator_diagnostics"][variant] for variant in VARIANTS)
    tb, tc = (data["trajectory_diagnostics"][variant] for variant in VARIANTS)
    final = data["final_task_diagnostics"]["timeliness_transitions"]
    equal = data["iteration_checkpoints"][-1]
    regret = next(item for item in data["operator_comparison"] if item["operator"] == "repair:regret2")
    ahead = [record["checkpoint"] for record in data["time_checkpoints"] if record["lexicographic_winner"] == "allocation"]
    behind = [record["checkpoint"] for record in data["time_checkpoints"] if record["lexicographic_winner"] == "baseline"]
    late_delta = final['baseline_on_time_allocation_late']['count'] - final['baseline_late_allocation_on_time']['count']
    return {
        "evidence": [
            f"同一seed1321的240秒运行内，allocation在{ahead}秒检查点词典序领先，在{behind}秒落后；不引用其他种子的短测来解释长测。",
            f"共同迭代上限{data['common_iteration_limit']}时baseline为{equal['baseline']['score']['late_count']}逾期、allocation为{equal['allocation']['score']['late_count']}；最终分别{data['final_score']['baseline']['late_count']}和{data['final_score']['allocation']['late_count']}。吞吐差不能单独解释回退。",
            f"baseline完成{b['total_iterations']}轮、allocation完成{c['total_iterations']}轮；搜索吞吐分别{b['iterations_per_search_second']:.2f}和{c['iterations_per_search_second']:.2f}轮/秒。",
            f"allocation在{tc['first_reach_at_most_late_count']['65']['end_to_end_seconds']:.2f}秒首次达到65逾期，之后未再下降；baseline在{tb['first_reach_at_most_late_count']['64']['end_to_end_seconds']:.2f}秒达到64，{tb['first_reach_at_most_late_count']['63']['end_to_end_seconds']:.2f}秒达到63。",
            f"allocation全局最优更新{c['best_improvements']}次，高于baseline的{b['best_improvements']}次；较多三级得分更新不等同于较少逾期任务。",
            f"regret2使用占比由{regret['baseline']['usage_share_of_iterations']:.2%}升至{regret['allocation']['usage_share_of_iterations']:.2%}，其接受率由{regret['baseline']['acceptance_rate']:.2%}升至{regret['allocation']['acceptance_rate']:.2%}。这是各自整段轨迹的描述统计。",
            f"最终{final['baseline_on_time_allocation_late']['count']}项任务从基线按时变成候选逾期，{final['baseline_late_allocation_on_time']['count']}项反向改善，逾期数净变化{late_delta:+d}；总逾期分钟变化{data['final_score_allocation_minus_baseline']['total_lateness_min']['absolute']:.3f}，航程变化{data['final_score_allocation_minus_baseline']['distance_km']['absolute']:.3f}km。",
        ],
        "mechanism_hypotheses": [
            "候选较早降低逾期数，随后在65这一第一目标水平上持续改善总逾期/航程；这与搜索集中于当前第一目标水平附近的解一致，但现有数据不能证明陷入哪个局部最优。",
            "regret2更高的累计接受、奖励与使用占比，可能和自适应权重的正反馈及搜索方向改变有关；缺少时序权重、配对算子和被拒候选日志，不能断言它导致长测退化。",
            "应优先在后续独立试验拆开lazy与route-distinct开关，并记录第一目标改善次数/归属与阶段算子统计，再检验候选机制；本诊断未启动这些试验。",
        ],
        "unidentified_confounding": data["limitations"],
    }


def markdown(data: dict) -> str:
    lines = ["# 同一长测配对的 regret 搜索诊断", "",
             "仅分析上一批 `confirm / fleet200 / seed 2026101321` 的 baseline 与 allocation，均为 240 秒预算。没有启动求解；完整路线已从官方 CSV 独立复算。所有派生数字、200 项任务明细和来源 SHA-256 见 [diagnostics.json](diagnostics.json)。", "",
             "## 直接证据", ""]
    lines.extend(f"- {item}" for item in data["findings"]["evidence"])
    lines.extend(["", "## 同一运行内的时间与迭代对齐", "",
                  "得分记为 `(逾期任务数, 总逾期分钟, 总航程 km)`。按检查点以前最后一次全局最优更新保持得分，不作线性插值。时间为端到端时钟。", "",
                  "| 秒 | baseline 得分 | allocation 得分 | 词典序胜者 |", "| ---: | --- | --- | --- |"])
    def triplet(point):
        s = point["score"]
        return f"({s['late_count']}, {s['total_lateness_min']:.3f}, {s['distance_km']:.3f})"
    for record in data["time_checkpoints"]:
        lines.append(f"| {record['checkpoint']} | {triplet(record['baseline'])} | {triplet(record['allocation'])} | {record['lexicographic_winner']} |")
    lines.extend(["", "| 相同迭代数 | baseline 得分 | allocation 得分 | 词典序胜者 |", "| ---: | --- | --- | --- |"])
    for record in data["iteration_checkpoints"]:
        lines.append(f"| {record['checkpoint']} | {triplet(record['baseline'])} | {triplet(record['allocation'])} | {record['lexicographic_winner']} |")
    common = data["iteration_checkpoints"][-1]
    lines.extend(["", "相同迭代并不等于相同邻域评价工作量；记录中的末次最优更新时间也不等于该检查点迭代的完成时间。"
                  f"共同迭代上限时逾期数为 {common['baseline']['score']['late_count']} 对 {common['allocation']['score']['late_count']}，完整预算末尾为 {data['final_score']['baseline']['late_count']} 对 {data['final_score']['allocation']['late_count']}。", "",
                  "## 吞吐、算子频次与成功率", "",
                  "每轮各选一个 destroy 和一个 repair，不能把两类次数相加当作独立样本。成功率分别定义为当前解改善／使用数、全局词典序最优改善／使用数；两者均不单独衡量逾期数改善。", "",
                  "| 总体 | baseline | allocation |", "| --- | ---: | ---: |"])
    b, c = (data["operator_diagnostics"][variant] for variant in VARIANTS)
    for label, key, percentage in (("迭代数", "total_iterations", False), ("搜索吞吐（轮/s）", "iterations_per_search_second", False),
                                   ("接受率", "acceptance_rate", True), ("当前解改善率", "current_improvement_rate", True),
                                   ("全局最优改善率", "best_improvement_rate", True)):
        values = [f"{item[key]:.2%}" if percentage else f"{item[key]:.2f}" for item in (b, c)]
        lines.append(f"| {label} | {values[0]} | {values[1]} |")
    lines.extend(["", "| 修复算子 | 使用占比 B → A | 接受率 B → A | 当前改善率 B → A | 最优改善率 B → A |", "| --- | ---: | ---: | ---: | ---: |"])
    for item in data["operator_comparison"]:
        if not item["operator"].startswith("repair:"):
            continue
        values = [f"{item['baseline'][key]:.2%} → {item['allocation'][key]:.2%}" for key in ("usage_share_of_iterations", "acceptance_rate", "current_improvement_rate", "best_improvement_rate")]
        lines.append("| " + " | ".join([item["operator"], *values]) + " |")
    lines.extend(["", "全部 14 个算子的计数、频率、奖励、末端权重和 profiling 每轮工作量均保存在 JSON。当前文件只保存末端累计统计，不能据此计算前期／后期算子成功率或某对 destroy–repair 的效果。", "",
                  "## 最终逾期任务与航程分布", "",
                  "| 指标 | baseline | allocation |", "| --- | ---: | ---: |"])
    task_data = data["final_task_diagnostics"]
    for field, label in (("late_count", "逾期任务数"), ("total_lateness_min", "总逾期分钟"), ("distance_km", "总航程 km")):
        lines.append(f"| {label} | {data['final_score']['baseline'][field]:.3f} | {data['final_score']['allocation'][field]:.3f} |")
    for field, label in (("median", "逾期任务逾期时长中位数 min"), ("q90", "逾期时长 P90 min"), ("max", "最大逾期 min")):
        values = [task_data["summaries"][v]["late_only_distribution_min"][field] for v in VARIANTS]
        lines.append(f"| {label} | {values[0]:.3f} | {values[1]:.3f} |")
    for label in task_data["summaries"]["baseline"]["lateness_bins"]:
        values = [task_data["summaries"][v]["lateness_bins"][label] for v in VARIANTS]
        lines.append(f"| {label} | {values[0]} | {values[1]} |")
    transitions = task_data["timeliness_transitions"]
    lines.extend(["", f"按时→逾期任务：{transitions['baseline_on_time_allocation_late']['task_ids']}。",
                  f"逾期→按时任务：{transitions['baseline_late_allocation_on_time']['task_ids']}。",
                  "上述集合比较两个最终解，不能反推中间哪一轮换了哪些任务。每架无人机都从同一原点出发，编号可交换，按同一 drone_id 对比路线也不构成因果证据。", "",
                  "## 机制推断与尚未识别的混杂", ""])
    lines.extend(f"- **待检验机制：**{item}" for item in data["findings"]["mechanism_hypotheses"])
    lines.extend(["- **混杂：**候选同时启用 lazy repair 和跨无人机 regret；相同种子在路径分歧后不代表随机选择逐轮匹配。只有一个已完成长测配对，且按 baseline 后 allocation 的顺序运行，不能排除主机负载与顺序影响。",
                  "- **日志缺口：**缺少中间路线、当前状态、拒绝候选、算子耗时、时序权重和 destroy–repair 联合统计；不能定位某个具体算子或某项任务导致的长测退化，也不能由累计改善数作显著性判断。", "",
                  "## 复算", "", "在仓库根目录执行；只读取冻结证据并完整评分，不运行 ALNS：", "", "```bash",
                  "PYTHONPATH=algorithm/src python -m algorithm.experiments.diagnose_regret_search \\",
                  "  --input-dir algorithm/results/optimization_20261013 \\",
                  "  --output-dir algorithm/results/regret_followup_20261013/diagnostics", "```", "",
                  "另有单独授权的 [36 次局部修复探针](local_repair_probe.md)，其新计算与本页既有长测诊断分别记录，不混入原批次成绩。", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    input_dir, output_dir = args.input_dir.resolve(), args.output_dir.resolve()
    if output_dir == input_dir or output_dir.is_relative_to(input_dir):
        raise ValueError("Diagnostic outputs must not alter the frozen source batch")
    data = diagnose(input_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "diagnostics.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output_dir / "README.md").write_text(markdown(data), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "source_seed": SEED,
                      "independent_routes_valid": True, "new_solves": 0,
                      "common_iteration_limit": data["common_iteration_limit"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
