"""One additional 240s periodic-exchange candidate, sharing the fresh baseline.

The existing baseline is copied exactly, explicitly marked as shared, and
never counted as a newly executed or independent sample.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import MappingProxyType
from zipfile import ZipFile

import run_weighted_method_pair as first

ROOT, read, write, sha = first.ROOT, first.read, first.write, first.sha
FIRST = first.OUTPUT
OUTPUT = ROOT / "algorithm/results/weighted_periodic_improvement_20261014"
ORDER = ["weighted_baseline", "weighted_periodic"]


def prepare(output, reuse_baseline=True):
    if (output / "manifest.json").exists():
        raise ValueError("Frozen experiment exists; select another output directory")
    parent = read(FIRST / "manifest.json")
    audit = read(FIRST / "audit.json")
    assert audit["passed"] and audit["manifest_sha256"] == sha(FIRST / "manifest.json")
    assert audit["runs_sha256"] == sha(FIRST / "runs.json")
    baseline = read(FIRST / "runs.json")[0]
    assert baseline["variant"] == "weighted_baseline"
    manifest = {**parent, "schema_version": 3, "order": ORDER,
                "parent_method_manifest_path": str((FIRST / "manifest.json").relative_to(ROOT)),
                "parent_method_manifest_sha256": sha(FIRST / "manifest.json"),
                "parent_method_audit_sha256": sha(FIRST / "audit.json"),
                "parent_method_runs_sha256": sha(FIRST / "runs.json")}
    sources = output / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    variants = {}
    for old_name, new_name in zip(first.ORDER, ORDER):
        spec = dict(parent["variants"][old_name])
        original = FIRST / spec["archive"]
        assert sha(original) == spec["sha256"]
        destination = sources / (new_name + ".zip")
        shutil.copyfile(original, destination)
        spec["archive"] = str(destination.relative_to(output))
        spec["label"] = "共享的当前加权基线" if new_name == ORDER[0] else "周期互换＋末端互换＋惰性修复"
        spec["periodic_exchange"] = new_name == ORDER[1]
        variants[new_name] = spec
    manifest["variants"] = variants
    execution = {}
    for name in (*parent["execution_sources"], "run_weighted_periodic_pair.py", "weighted_periodic_exchange.py"):
        path = ROOT / "algorithm/experiments" / name
        frozen = sources / name
        shutil.copyfile(path, frozen)
        execution[name] = dict(path=str(frozen.relative_to(output)), sha256=sha(frozen))
    manifest["execution_sources"] = execution
    manifest["periodic_parameters"] = dict(interval=120, max_candidates=16, anchors_per_route=4, pair_budget_seconds=.008)
    manifest["variant_descriptions"] = {
        ORDER[0]: "上一阶段同一种子2026101461、240秒预算的实际加权基线，完整复制其原始解和全部记录；共享一个基线，不增加独立样本。",
        ORDER[1]: "保留末端请求互换与已有惰性修复；每120次完整修复轮转一对路线，最多精评16个原位请求交换，单次至多8ms预算，从原总预算内支出。候选再进入原ALNS接受/奖励机制。",
    }
    manifest["implementation_notes"] = [
        "首轮末端组合的得分改善仅约0.005%，按时数不变、航程略增；保留该记录并继续有界周期邻域。",
        "周期步骤不抽随机数；其改善候选进入原接受与奖励，因此后续搜索轨迹会自然改变，不能称固定迭代决策等价。",
        "权重、尺度、种子、初解、温度和总预算均不改变；原位请求交换完整保留取送和容量结构。",
        "报告的基线是共享实测记录，不是重新运行或新增样本；两阶段所有退化均保留。",
    ]
    manifest["shared_baseline"] = dict(reused=reuse_baseline, source_runs_path=str((FIRST / "runs.json").relative_to(ROOT)),
                                       source_runs_sha256=sha(FIRST / "runs.json"),
                                       source_solution_path=str((FIRST / baseline["solution_path"]).relative_to(ROOT)),
                                       source_solution_sha256=baseline["solution_sha256"])
    manifest["reproduction_commands"] = [
        ".venv/bin/python algorithm/experiments/run_weighted_periodic_pair.py prepare --fresh-baseline --output-dir algorithm/results/weighted_periodic_replay_NEW",
        ".venv/bin/python algorithm/experiments/run_weighted_periodic_pair.py run --output-dir algorithm/results/weighted_periodic_replay_NEW",
        ".venv/bin/python algorithm/experiments/audit_weighted_periodic_pair.py",
        ".venv/bin/python algorithm/experiments/render_weighted_periodic_report.py",
    ]
    rows = []
    if reuse_baseline:
        source = FIRST / baseline["solution_path"]
        assert sha(source) == baseline["solution_sha256"]
        destination = output / baseline["solution_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        rows.append(baseline)
    write(output / "manifest.json", manifest)
    write(output / "runs.json", rows)
    print(json.dumps({"prepared": str(output), "shared_baseline": reuse_baseline,
                      "new_runs": 1 if reuse_baseline else 2}, ensure_ascii=False))


def worker(request):
    req = read(request)
    manifest = read(Path(req["output"]) / "manifest.json")
    if req["variant"] == "weighted_periodic":
        import weighted_periodic_exchange as periodic
        from uav_dispatch import alns
        original_solve = alns.solve_alns

        def solve_with_periodic(problem, *, config, initial_routes=None):
            stats = {}
            with periodic.periodic_exchange(alns, statistics=stats, **manifest["periodic_parameters"]):
                result = original_solve(problem, config=config, initial_routes=initial_routes)
            return dataclasses.replace(result, metadata=MappingProxyType({**result.metadata, "periodic_task_exchange": stats}))

        alns.solve_alns = solve_with_periodic
        try:
            first.worker(request)
        finally:
            alns.solve_alns = original_solve
    else:
        first.worker(request)
    row = read(req["response"])
    payload = read(Path(req["output"]) / row["solution_path"])
    row["periodic_task_exchange"] = payload["metadata"].get("periodic_task_exchange", {})
    write(req["response"], row)


def run(output):
    manifest = read(output / "manifest.json")
    for name, spec in manifest["execution_sources"].items():
        assert sha(output / spec["path"]) == spec["sha256"] == sha(ROOT / "algorithm/experiments" / name)
    rows = read(output / "runs.json")
    assert [r["variant"] for r in rows] == ORDER[:len(rows)]
    for name in ORDER[len(rows):]:
        spec = manifest["variants"][name]
        archive = output / spec["archive"]
        assert sha(archive) == spec["sha256"]
        assert not (output / "run_solutions" / (name + ".json")).exists(), "Never overwrite a completed result"
        with tempfile.TemporaryDirectory(prefix="weighted-periodic-") as temporary:
            temporary = Path(temporary)
            with ZipFile(archive) as package:
                assert all(n.startswith("uav_dispatch/") and n.endswith(".py") and len(Path(n).parts) == 2 for n in package.namelist())
                package.extractall(temporary)
            request = temporary / "request.json"
            write(request, dict(output=str(output), variant=name, response=str(temporary / "response.json")))
            print("START " + name, flush=True)
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker", "--request", str(request)], cwd=ROOT,
                           env={**os.environ, "PYTHONPATH": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}, check=True, timeout=270)
            row = read(temporary / "response.json")
        rows.append(row)
        write(output / "runs.json", rows)
        print(json.dumps({k: row[k] for k in ("variant", "late_count", "total_lateness_min", "distance_km", "weighted_score",
                                              "end_to_end_seconds", "iterations")}, ensure_ascii=False), flush=True)
        if not row["valid"] or not row["budget_compliant"]:
            raise RuntimeError("Invalid or over-budget result retained for diagnosis")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "worker"))
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--fresh-baseline", action="store_true")
    parser.add_argument("--request", type=Path)
    args = parser.parse_args()
    if args.command == "worker":
        worker(args.request)
    elif args.command == "prepare":
        prepare(args.output_dir.resolve(), not args.fresh_baseline)
    else:
        run(args.output_dir.resolve())
