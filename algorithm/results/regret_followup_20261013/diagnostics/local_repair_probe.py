"""36 authorized local repair probes; no ALNS solve or new incumbent search.

Starts from the saved allocation fleet200/seed1321 final routes.  Observes
capacity-slot scarcity while old route-distinct regret repairs ten removals.
Fallback and portfolio additions remain disabled.  Run with the solver package
on PYTHONPATH; hashes bind the exact imported source used by this fresh probe.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from random import Random
from time import perf_counter
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

import uav_dispatch.alns as alns
from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.validation import evaluate_solution

ROOT = Path(__file__).resolve().parents[4]
OUTPUT = Path(__file__).resolve().parent
SOURCE = ROOT / "algorithm/results/optimization_20261013"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def value_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main():
    started = perf_counter()
    manifest = json.loads((SOURCE / "manifest.json").read_text())
    input_path = ROOT / manifest["input_path"]
    rows = json.loads((SOURCE / "runs.json").read_text())
    source_row = next(row for row in rows if row["run_id"] == "confirm__fleet200__allocation__2026101321")
    solution_path = SOURCE / source_row["solution_file"]
    assert sha256(input_path) == manifest["input_sha256"]
    assert sha256(solution_path) == source_row["solution_sha256"]
    payload = json.loads(solution_path.read_text())
    routes = tuple(tuple(route["encoded_visits"]) for route in payload["routes"])
    problem = Problem(tuple(load_tasks_csv(input_path)), drone_count=8, max_tasks_per_drone=25)
    initial_evaluation = evaluate_solution(problem, routes, _force_full=True)
    assert initial_evaluation.valid and asdict(initial_evaluation.score) == payload["score"]
    package_root = Path(alns.__file__).resolve().parent
    actual_source_hashes = {path.name: sha256(path) for path in sorted(package_root.glob("*.py"))}
    frozen_source = OUTPUT / "probe_solver.zip"
    with ZipFile(frozen_source, "w", compression=ZIP_DEFLATED) as archive:
        for filename in actual_source_hashes:
            info = ZipInfo("uav_dispatch/" + filename, (2026, 10, 13, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, (package_root / filename).read_bytes())
    defaults = alns.ALNSConfig()
    assert defaults.enable_regret_position_fallback is False
    assert defaults.enable_regret_portfolio is False
    original = alns._RepairCache.direct_options
    observed, seen = [], set()

    def observe(self, problem_, evaluator, routes_, task_id, route_index, candidate_limit, option_count, deadline):
        if routes_ not in seen:
            seen.add(routes_)
            delivery_counts = [sum(visit < 0 for visit in route) for route in routes_]
            remaining = 200 - sum(delivery_counts)
            observed.append({"round": len(observed) + 1, "available_routes": sum(count < 25 for count in delivery_counts),
                             "remaining_tasks": remaining, "tasks_per_route": delivery_counts,
                             "partial_routes_sha256": value_hash(routes_)})
        return original(self, problem_, evaluator, routes_, task_id, route_index, candidate_limit, option_count, deadline)

    alns._RepairCache.direct_options = observe
    cells = []
    aggregates = defaultdict(Counter)
    by_destroy = defaultdict(Counter)
    try:
        for destroy in alns.DESTROY_OPERATORS:
            for seed in (3, 11):
                partial, removed = alns.destroy_solution(problem, RouteEvaluator(problem), routes, destroy, 10, Random(seed))
                removed = tuple(removed)
                assert len(removed) == 10
                for strategy in ("regret2", "regret3"):
                    observed, seen = [], set()
                    evaluator = RouteEvaluator(problem)
                    repaired = alns._repair(
                        problem, evaluator, partial, removed, strategy, 48,
                        use_deadline_risk=False, repair_rank_candidate_limit=24,
                        repair_exact_candidate_limit=48, enable_lazy_repair=False,
                        enable_route_distinct_regret=True, enable_regret_position_fallback=False,
                    )
                    result = evaluate_solution(problem, repaired, _force_full=True)
                    assert result.valid
                    assert len(observed) == 10
                    k = int(strategy[-1])
                    count = Counter(rounds=len(observed))
                    for observation in observed:
                        shortage = observation["available_routes"] < k
                        observation["structural_available_routes_less_than_k"] = shortage
                        observation["remaining_tasks_more_than_one"] = observation["remaining_tasks"] > 1
                        count["infinite_regret_rounds"] += shortage
                        count["infinite_with_multiple_tasks"] += shortage and observation["remaining_tasks"] > 1
                        count["rounds_free1"] += observation["available_routes"] == 1
                        count["rounds_free2"] += observation["available_routes"] == 2
                    counters = evaluator.counters.as_dict()
                    assert counters["regret_distinct_round_count"] == len(observed)
                    assert counters["regret_all_infinite_round_count"] == count["infinite_regret_rounds"]
                    assert counters["regret_position_fallback_count"] == 0
                    aggregates[strategy].update(count)
                    by_destroy[(destroy, strategy)].update(count)
                    cells.append({"destroy": destroy, "seed": seed, "repair": strategy, "regret_k": k,
                                  "removed_task_ids": list(removed), "partial_routes": partial,
                                  "partial_routes_sha256": value_hash(partial), "round_observations": observed,
                                  "counts": dict(count), "search_counters": counters,
                                  "repaired_routes": repaired, "repaired_routes_sha256": value_hash(repaired),
                                  "independent_validation": {"valid": result.valid, "violations": list(result.violations),
                                                             "score": asdict(result.score),
                                                             "completed_tasks": len(result.delivery_times_min)}})
    finally:
        alns._RepairCache.direct_options = original
    assert actual_source_hashes == {path.name: sha256(path) for path in sorted(package_root.glob("*.py"))}
    result = {
        "schema_version": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "fresh_local_probe": True, "full_alns_solves": 0, "cell_count": len(cells),
        "elapsed_seconds": perf_counter() - started,
        "provenance": {"source_solution_file": str(solution_path.relative_to(ROOT)), "source_solution_sha256": sha256(solution_path),
                       "original_runs_sha256": sha256(SOURCE / "runs.json"), "official_input_sha256": sha256(input_path),
                       "script_sha256": sha256(Path(__file__)), "actual_solver_source_sha256": actual_source_hashes,
                       "solver_snapshot_file": frozen_source.name, "solver_snapshot_sha256": sha256(frozen_source),
                       "source_final_routes_sha256": value_hash(routes)},
        "configuration": {"destroy_operators": list(alns.DESTROY_OPERATORS), "seeds": [3, 11], "removed_tasks": 10,
                          "repair_operators": ["regret2", "regret3"], "candidate_limit": 48,
                          "repair_rank_candidate_limit": 24, "repair_exact_candidate_limit": 48,
                          "use_deadline_risk": False, "enable_lazy_repair": False,
                          "enable_route_distinct_regret": True, "enable_regret_position_fallback": False,
                          "enable_regret_portfolio": False},
        "measurement": "One observation for each first-seen partial routes tuple within one repair. A route has a free task slot if delivered task count <25. Fewer than k such routes structurally prevents k route-distinct alternatives; aggregate equality with actual regret_all_infinite_round_count is asserted in every cell.",
        "aggregates_by_repair": {key: dict(value) for key, value in aggregates.items()},
        "aggregates_by_destroy_and_repair": [{"destroy": key[0], "repair": key[1], **dict(value)} for key, value in by_destroy.items()],
        "cells": cells,
        "limitations": [
            "Fresh deterministic local repair probes, not records captured during the original long ALNS trajectory.",
            "All probes begin at one saved final allocation solution; earlier search states are not sampled.",
            "Deadline-risk guidance is disabled here, unlike the original ALNS configuration; counts are conditional on these probe settings.",
            "Lazy repair, newly introduced position fallback and adaptive regret portfolio are disabled; only old route-distinct behavior is observed.",
            "Operator/seed cells share the same final solution and are not independent samples of global performance.",
            "A structural all-infinite-regret round identifies loss of k-alternative discrimination, not infeasibility or proof of the long-run score regression.",
        ],
    }
    (OUTPUT / "local_repair_probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    lines = ["# 36 次局部修复探针", "",
             "本探针是另行授权的新局部诊断，从上一批 allocation / fleet200 / seed1321 的最终完整路线出发；未运行完整 ALNS，也不将新修复得分计入旧批次。", "",
             "9 种 destroy × 2 个种子（3、11）× regret2/3，共 36 次，每次移除并重新插入 10 项任务。全部末解经独立完整评分验证合法。", "",
             "使用当前已冻结源码，`enable_regret_position_fallback=False`、`enable_regret_portfolio=False`，同时关闭 lazy，仅观察旧 route-distinct regret。`use_deadline_risk=False` 与原长测的风险引导不同，结果只适用于本探针设置。", "",
             "| 策略 | 修复轮数 | 所有任务均不足 k 个不同无人机备选 | 尚有多项任务时出现上述情况 |", "| --- | ---: | ---: | ---: |"]
    for strategy, count in aggregates.items():
        lines.append(f"| {strategy} | {count['rounds']} | {count['infinite_regret_rounds']} ({count['infinite_regret_rounds']/count['rounds']:.2%}) | {count['infinite_with_multiple_tasks']} |")
    lines.extend(["", "逐轮以可用任务槽无人机数 `< k` 标记结构性备选不足，并逐次断言其累计数量等于源码实际 `regret_all_infinite_round_count`。不足备选并不等于无法插入；36 次修复全部合法。", "",
                  "该现象支持检查候选不足时的判别规则，但不能证明它导致原长测退化：只探测一个最终状态，且没有原搜索中的被拒候选、算子组合或状态序列。", "",
                  "[完整逐次/逐轮记录、修复路线和哈希](local_repair_probe.json) · [实际源码快照](probe_solver.zip) · [复算脚本](local_repair_probe.py)", "",
                  "从仓库根目录运行（当前 solver 源码须与记录的哈希一致；也可将 probe_solver.zip 解压到独立临时目录并将该目录置于 PYTHONPATH）：", "", "```bash",
                  "PYTHONPATH=algorithm/src python algorithm/results/regret_followup_20261013/diagnostics/local_repair_probe.py", "```", ""])
    (OUTPUT / "local_repair_probe.md").write_text("\n".join(lines))
    print(json.dumps({"cell_count": len(cells), "all_valid": True, "full_alns_solves": 0,
                      "elapsed_seconds": result["elapsed_seconds"], "aggregates_by_repair": result["aggregates_by_repair"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
