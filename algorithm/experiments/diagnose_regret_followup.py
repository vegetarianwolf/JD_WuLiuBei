"""Rebuild the completed portfolio confirmation diagnosis using only JSON logs.

No solver imports or executions. Evidence binds the two selected run rows and
their solution files, so subsequently appended extension rows do not invalidate it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "algorithm/results/regret_followup_20261013/supplemental_trial"
DEFAULT_OUTPUT = ROOT / "algorithm/results/regret_followup_20261013/diagnostics"
VARIANTS = ("baseline", "portfolio")
SCORE_FIELDS = ("late_count", "total_lateness_min", "distance_km")
COUNTERS = (
    "regret_distinct_round_count", "regret_distinct_ranking_count",
    "regret_distinct_shortage_count", "regret_position_fallback_count",
    "regret_position_shortage_count", "regret_all_infinite_round_count",
)
STAT_FIELDS = ("uses", "accepted", "current_improvements", "best_improvements", "total_reward")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def value_hash(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def score(values):
    return dict(zip(SCORE_FIELDS, values))


def point_record(point, construction):
    return {"iteration": point[0], "end_to_end_seconds": point[1],
            "search_seconds": point[1] - construction, "score": score(point[2:])}


def load_evidence(directory):
    selected = [row for row in read_json(directory / "runs.json")
                if (row["stage"], row["case_id"], row["seed"]) == ("confirm", "fleet200", 2026101321)
                and row["variant"] in VARIANTS]
    require(len(selected) == 2 and {r["variant"] for r in selected} == set(VARIANTS),
            "Expected exactly one completed baseline/portfolio confirmation pair")
    rows = {row["variant"]: row for row in selected}
    payloads, evidence = {}, {}
    for variant, row in rows.items():
        require(row["valid"] is True and row["budget_compliant"] is True, "Invalid or over-budget evidence")
        require(row["scenario"] == "pure_direct" and row["budget_seconds"] == 240
                and row["task_count"] == 200 and row["drone_count"] == 8, "Unexpected confirmation case")
        path = (directory / row["solution_file"]).resolve()
        require(path.is_relative_to(directory), "Solution path escapes the cohort")
        require(sha256(path) == row["solution_sha256"], "Saved solution fingerprint mismatch")
        payload = read_json(path)
        metadata = payload["metadata"]
        require(payload["valid"] is True and payload["iterations"] == row["iterations"], "Solution/run disagreement")
        require(payload["score"] == {key: row[key] for key in SCORE_FIELDS}, "Solution/run score disagreement")
        require(metadata["profiling"] == row["profiling"], "Profiling disagreement")
        points, meta_points = row["best_trajectory"], metadata["best_trajectory"]
        require(bool(points) and len(points) == len(meta_points), "Incomplete trajectory")
        for point, saved in zip(points, meta_points):
            require(len(point) == len(saved) == 5 and point[0] == saved[0] and point[2:] == saved[2:],
                    "Trajectory values disagree")
            require(math.isclose(point[1], saved[1] + row["construction_seconds"], abs_tol=1e-8, rel_tol=0),
                    "Trajectory time boundaries disagree")
        for previous, current in zip(points, points[1:]):
            require(previous[0] <= current[0] and previous[1] <= current[1]
                    and tuple(current[2:]) < tuple(previous[2:]), "Incumbent trajectory is not monotone")
        require(points[-1][0] <= row["iterations"] and score(points[-1][2:]) == payload["score"],
                "Final incumbent disagrees with saved score")
        for flag, value in row["config"].items():
            if flag.startswith("enable_regret_") or flag in {"enable_lazy_repair", "enable_route_distinct_regret"}:
                require(metadata.get(flag) is value, "Experimental switch metadata disagrees")
        payloads[variant] = payload
        evidence[variant] = {
            "solution_file": str(path.relative_to(ROOT)),
            "solution_sha256": row["solution_sha256"],
            "run_row_sha256": value_hash(row), "run_row": row,
        }
    require(rows["baseline"]["initial_routes_sha256"] == rows["portfolio"]["initial_routes_sha256"],
            "Initial routes differ")
    require(rows["baseline"]["best_trajectory"][0][2:] == rows["portfolio"]["best_trajectory"][0][2:],
            "Initial scores differ")
    return rows, payloads, evidence


def trajectory_diagnostics(row, payload):
    points = row["best_trajectory"]
    primary = [current for previous, current in zip(points, points[1:]) if current[2] < previous[2]]
    last = primary[-1] if primary else None
    search_time = last[1] - row["construction_seconds"] if last else None
    return {
        "final_score": payload["score"], "iterations": row["iterations"],
        "end_to_end_seconds": row["end_to_end_seconds"],
        "search_runtime_seconds": payload["runtime_seconds"],
        "primary_improvement_count": len(primary),
        "last_primary_improvement": point_record(last, row["construction_seconds"]) if last else None,
        "primary_plateau_seconds": payload["runtime_seconds"] - search_time if last else None,
        "last_incumbent_improvement": point_record(points[-1], row["construction_seconds"]),
        "lex_improvements_after_last_primary": sum(p[0] > last[0] for p in points) if last else None,
    }


def operator_diagnostics(row, payload):
    metadata = payload["metadata"]
    per_operator = {name: dict(stats) for name, stats in metadata["operator_statistics"].items()
                    if name.startswith("repair:")}
    iterations = row["iterations"]
    require(sum(s["uses"] for s in per_operator.values()) == iterations, "Repair uses do not sum to iterations")
    require(sum(s["accepted"] for s in per_operator.values()) == metadata["accepted_solutions"],
            "Repair accepts disagree with total")
    require(sum(s["best_improvements"] for s in per_operator.values()) == len(row["best_trajectory"]) - 1,
            "Repair best improvements disagree with trajectory")
    for name, stats in per_operator.items():
        require(stats["uses"] == metadata["operator_uses"][name], "Operator counts disagree")
        require(0 <= stats["best_improvements"] <= stats["current_improvements"] <= stats["accepted"] <= stats["uses"],
                "Operator event counts are inconsistent")
        # Existing reward categories are disjoint: best=8, current-only=4,
        # accepted-only=1. They do not distinguish objective tiers.
        require(stats["total_reward"] == stats["accepted"] + 3 * stats["current_improvements"] + 4 * stats["best_improvements"],
                "Operator rewards do not match the frozen reward rule")

    def aggregate(names):
        totals = {field: sum(per_operator.get(name, {}).get(field, 0) for name in names) for field in STAT_FIELDS}
        uses = totals["uses"]
        return {**totals, "use_fraction": uses / iterations,
                "acceptance_rate": totals["accepted"] / uses if uses else None,
                "reward_per_use": totals["total_reward"] / uses if uses else None}

    groups = {
        "route_regret": aggregate(("repair:route_regret2", "repair:route_regret3")),
        "position_regret": aggregate(("repair:regret2", "repair:regret3")),
    }
    for name in per_operator:
        per_operator[name] = {**aggregate((name,)), "final_weight": metadata["operator_weights"][name]}
    return {"repair_pool_size": len(per_operator), "groups": groups, "per_operator": per_operator,
            "scope": "Cumulative completed-iteration totals; no per-time-bin or per-objective-tier operator log exists."}


def diagnose(directory):
    rows, payloads, evidence = load_evidence(directory)
    common = min(row["iterations"] for row in rows.values())
    aligned = {}
    for variant, row in rows.items():
        point = [p for p in row["best_trajectory"] if p[0] <= common][-1]
        aligned[variant] = {"score": score(point[2:]), "last_incumbent_iteration": point[0],
                            "last_incumbent_end_to_end_seconds": point[1]}
    counters = {}
    for variant, row in rows.items():
        values = {key: row["profiling"][key] for key in COUNTERS if key in row["profiling"]}
        counters[variant] = {"available": len(values) == len(COUNTERS), "values": values}
        if values:
            require(len(values) == len(COUNTERS), "Partially recorded regret counters")
            for value in values.values():
                require(type(value) is int and value >= 0, "Invalid regret counter")
            rankings = values["regret_distinct_ranking_count"]
            rounds = values["regret_distinct_round_count"]
            counters[variant].update(
                fallback_fraction_of_task_rankings=values["regret_position_fallback_count"] / rankings if rankings else None,
                all_infinite_fraction_of_distinct_rounds=values["regret_all_infinite_round_count"] / rounds if rounds else None,
            )
    return {
        "schema_version": 1, "full_alns_solves": 0,
        "provenance": {
            "runs_file": str((directory / "runs.json").relative_to(ROOT)),
            "selection": {"stage": "confirm", "case_id": "fleet200", "seed": 2026101321},
            "path_base": "repository root",
            "run_row_hash_format": "UTF-8 json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)",
            "diagnostic_script": str(Path(__file__).resolve().relative_to(ROOT)),
            "diagnostic_script_sha256": sha256(Path(__file__)), "evidence": evidence,
        },
        "run_diagnostics": {v: trajectory_diagnostics(rows[v], payloads[v]) for v in VARIANTS},
        "common_iterations": {"iteration": common, **aligned},
        "operator_diagnostics": {v: operator_diagnostics(rows[v], payloads[v]) for v in VARIANTS},
        "regret_counters": counters,
        "limitations": [
            "Only the completed known-counterexample seed is analyzed; this is not an independent seed holdout or proof of general performance.",
            "No solver or full-route reevaluation is run here. Saved valid flags, scores, trajectories, counts and fingerprints are cross-checked; independent solution auditing is separate.",
            "Iteration alignment compares observed incumbents; portfolio expands the repair pool from 5 to 7 and changes random operator selection and adaptive search trajectories. Equal seeds or iterations are not equal moves.",
            "Counter elimination of all-infinite ranking rounds verifies the local fallback mechanism in this recorded run; it does not identify the unique cause of the remaining primary-objective plateau.",
            "Operator frequencies, acceptance and rewards are whole-run aggregates. They cannot establish a time-local or causal link to late-count stagnation, and rewards do not distinguish the three objective tiers.",
            "Regret counters cover distinct Stage-A repair work, including any unfinished last repair; shortage/fallback count task rankings, while all-infinite counts rounds. Missing baseline instrumentation is unavailable, not zero.",
        ],
    }


def markdown(data):
    runs, ops = data["run_diagnostics"], data["operator_diagnostics"]
    lines = ["# 组合候选长测日志诊断", "",
             "仅分析补充批次已完成的 `confirm / fleet200 / seed 2026101321`，预算均为 240 秒。只读 JSON 日志；没有启动求解或重新评价路线。两份解文件及两条完整运行记录分别绑定 SHA-256，后续追加扩展结果不会使本诊断过期。", "",
             "| 版本 | 最终逾期数 | 总逾期分钟 | 距离 km | 完成迭代 | 最后一次逾期数改善（端到端 / 搜索秒） | 此后搜索停滞秒 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for variant in VARIANTS:
        item = runs[variant]
        last, final = item["last_primary_improvement"], item["final_score"]
        lines.append(f"| {variant} | {final['late_count']} | {final['total_lateness_min']:.6f} | {final['distance_km']:.6f} | {item['iterations']} | {last['end_to_end_seconds']:.3f} / {last['search_seconds']:.3f}（第 {last['iteration']} 轮） | {item['primary_plateau_seconds']:.3f} |")
    common = data["common_iterations"]
    lines += ["", f"共同迭代上限为 **{common['iteration']}**。按该轮及以前最后一次 incumbent 记录对齐：baseline = `{tuple(common['baseline']['score'].values())}`，portfolio = `{tuple(common['portfolio']['score'].values())}`。这能观察同迭代规模下的差距，不能把两者当作相同搜索路径。", "",
              "| 版本 / 修复算子组 | 调用次数 | 占完成迭代 | 接受数 | 接受率 | 累计奖励 | 每次调用奖励 | 当前改善 / 最优改善 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for variant in VARIANTS:
        for name, values in ops[variant]["groups"].items():
            accept = f"{values['acceptance_rate']:.2%}" if values["acceptance_rate"] is not None else "无调用"
            reward = f"{values['reward_per_use']:.4f}" if values["reward_per_use"] is not None else "无调用"
            lines.append(f"| {variant} / {name} | {values['uses']} | {values['use_fraction']:.2%} | {values['accepted']} | {accept} | {values['total_reward']:.0f} | {reward} | {values['current_improvements']} / {values['best_improvements']} |")
    values = data["regret_counters"]["portfolio"]["values"]
    lines += ["", f"组合候选记录 {values['regret_distinct_round_count']:,} 个跨机排序轮次、{values['regret_distinct_ranking_count']:,} 次任务排序；跨机备选不足与位置回退均为 **{values['regret_position_fallback_count']:,}** 次，真正位置备选不足为 {values['regret_position_shortage_count']}，整轮 regret 全为 inf 为 **{values['regret_all_infinite_round_count']}**。基线缺少这组计数，不能写作零。", "",
              "这些日志支持“位置回退消除了本次记录中的整轮 inf 退化”这一局部机制结论，但组合候选仍停在 65 个逾期任务。算子池从 5 项扩大为 7 项，同时改变随机选取和自适应过程；累计接受率与奖励只有相关性，不能证明停滞的唯一原因，也不能据此认定局部机制无效。奖励没有区分第一、第二、第三目标的改善。", "",
              "完整原始计数、每个算子、最后改善时间、共同迭代分数、限定条件及指纹见 [followup_observations.json](followup_observations.json)。", "",
              "重建：", "", "```sh", ".venv/bin/python algorithm/experiments/diagnose_regret_followup.py", "```", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    data = diagnose(args.input_dir.resolve())
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "followup_observations.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "followup_observations.md").write_text(markdown(data), encoding="utf-8")
    print(json.dumps({"common_iterations": data["common_iterations"], "regret_counters": data["regret_counters"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
