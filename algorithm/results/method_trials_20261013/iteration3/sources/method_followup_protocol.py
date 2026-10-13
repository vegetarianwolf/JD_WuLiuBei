"""Reusable immutable plans for additional small quality-method trials.

Earlier frozen runners are helpers only; their globals and files never change.
Runner decisions and independent audit decisions use separate implementations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import platform
import time
import zipfile

from algorithm.experiments import run_method_trials as first
from algorithm.experiments import audit_method_trials as original

base, checks, ROOT = first.base, original.checks, first.ROOT
_cell, _safe, _aborted = first._cell, first._safe, first._aborted
BASELINE_COMMIT = first.BASELINE_COMMIT
TOTAL_SECONDS, RESERVED_SECONDS = 900, 30
CASES = {s: ["single25", "fleet200"] for s in ("screen", "medium")}
BUDGETS = {"screen": {"single25": 10, "fleet200": 10}, "medium": {"single25": 10, "fleet200": 30}}
CAPS = {"screen": 80, "medium": 240}


@dataclass(frozen=True)
class Plan:
    name: str
    candidate: str
    flag: str
    label: str
    screen_seeds: tuple[int, int]
    medium_seeds: tuple[int, int, int]
    # All locations come from source-controlled plans, never from a manifest.
    priors: tuple[tuple[str, str, str], ...]
    previous_flags: tuple[str, ...]
    allow_screen_regressions: bool = False

    @property
    def seeds(self):
        return {"screen": list(self.screen_seeds), "medium": list(self.medium_seeds)}

    @property
    def variants(self):
        return {"baseline": {}, self.candidate: {self.flag: True}}

    @property
    def kind(self):
        return f"method_trials_{self.name}"

    @property
    def execution_names(self):
        return (*first.EXECUTION_NAMES, "method_followup_protocol", f"run_method_{self.name}")

    @property
    def protocol(self):
        return {**{k: v for k, v in first.PROTOCOL.items() if not k.startswith("equivalence")},
                "stage_caps_seconds": CAPS, "stage_cases": CASES, "stage_budgets_seconds": BUDGETS,
                "screen_gate": "全部预定配对有效且按预算完成；集群至少一胜即可，允许退化并完整披露" if self.allow_screen_regressions else "两实例所有配对均无词典序退化；集群至少一胜",
                "allow_screen_regressions": self.allow_screen_regressions,
                "selection": "唯一候选初筛通过且30秒三种子集群多数胜时采用",
                "budget_accounting": "全部此前批次及本批完整、中止耗时共享900秒；预留全部缺失单元及30秒审计",
                "followup": "用户选择继续文献与方法迭代；新质量候选不声明固定搜索等价",
                "user_choices": [*first.PROTOCOL["user_choices"], *(["第三轮初筛一次集群获胜即可扩大验证；完整披露退化"] if self.allow_screen_regressions else [])]}


def cohort_evidence(output, plan):
    bindings, total = {}, 0.
    for name, relative, kind in plan.priors:
        directory = (output / relative).resolve()
        documents = {n: base.read_json(directory / f"{n}.json") for n in ("manifest", "runs", "decisions", "validation")}
        manifest, validation = documents["manifest"], documents["validation"]
        if (manifest.get("kind") != kind or manifest.get("baseline_commit") != BASELINE_COMMIT
                or validation.get("passed") is not True or validation.get("protocol_complete") is not True
                or validation.get("decision_audit", {}).get("selected_variant") is not None):
            raise ValueError("此前批次必须已审计完成且未晋级")
        for n in ("manifest", "runs", "decisions"):
            if base.sha256(directory / f"{n}.json") != validation.get(f"{n}_sha256"):
                raise ValueError("此前批次审计绑定不一致")
        for source in [*manifest["source_snapshots"].values(), *manifest["execution_sources"].values()]:
            if base.sha256(_safe(directory, source["path"])) != source["sha256"]:
                raise ValueError("此前批次源码改变")
        for row in documents["runs"]:
            if base.sha256(_safe(directory, row["solution_file"])) != row["solution_sha256"]:
                raise ValueError("此前批次完整解改变")
        seconds = first.experiment_seconds(directory)
        audited = validation["cumulative_budget"].get("batch_seconds", validation["cumulative_budget"]["total_seconds"])
        checks._same(seconds, audited, "prior audited batch seconds")
        notes = directory / "execution_notes.json"
        bindings[name] = {"directory": relative, "kind": kind, "experiment_seconds": seconds,
                          **{f"{n}_sha256": base.sha256(directory / f"{n}.json") for n in documents},
                          "execution_notes_sha256": base.sha256(notes) if notes.exists() else None}
        total += seconds
    return {"cohorts": bindings, "prior_experiment_seconds": total}


def prepare(output, *, plan):
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("新追加目录必须为空，禁止覆盖")
    prior = cohort_evidence(output, plan)
    if prior["prior_experiment_seconds"] + CAPS["screen"] > TOTAL_SECONDS - RESERVED_SECONDS:
        raise RuntimeError("共享预算不足以启动追加初筛")
    output.mkdir(parents=True, exist_ok=True)
    snapshots, execution = {}, {}
    for variant, flags in plan.variants.items():
        path = f"sources/{variant}.zip"
        snapshots[variant] = {"path": path, "sha256": base._snapshot(output / path, BASELINE_COMMIT if variant == "baseline" else None),
                              "config": flags, "source_ref": BASELINE_COMMIT if variant == "baseline" else "working_tree",
                              "role": "baseline" if variant == "baseline" else "candidate",
                              "label": "当前 main 基线" if variant == "baseline" else plan.label}
    for name in plan.execution_names:
        source = ROOT / f"algorithm/experiments/{name}.py"
        target = output / "sources" / source.name
        target.write_bytes(source.read_bytes())
        execution[name] = {"path": str(target.relative_to(output)), "sha256": base.sha256(target)}
    manifest = {"schema_version": 7, "kind": plan.kind, "baseline_variant": "baseline", "baseline_commit": BASELINE_COMMIT,
                "input_path": base.INPUT, "input_sha256": base.sha256(ROOT / base.INPUT), "seeds": plan.seeds,
                "protocol": plan.protocol, "source_snapshots": snapshots, "execution_sources": execution,
                "runner_sha256": execution["run_optimization_experiments"]["sha256"], "prior_bindings": prior,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "environment": {"python": platform.python_version(), "platform": platform.platform()}}
    for name, data in (("manifest", manifest), ("runs", []), ("decisions", [])):
        base.write_json(output / f"{name}.json", data)
    return manifest


def _manifest(manifest, plan):
    if (manifest.get("schema_version") != 7 or manifest.get("kind") != plan.kind or manifest.get("baseline_variant") != "baseline"
            or manifest.get("baseline_commit") != BASELINE_COMMIT or manifest.get("input_path") != base.INPUT
            or manifest.get("seeds") != plan.seeds or manifest.get("protocol") != plan.protocol
            or any(type(s) is not int for seeds in manifest["seeds"].values() for s in seeds)
            or set(manifest.get("source_snapshots", {})) != set(plan.variants)):
        raise ValueError("追加批次冻结协议改变")


def _sources(directory, manifest, plan, *, git_baseline=False):
    execution, hashes = manifest.get("execution_sources", {}), {}
    if set(execution) != set(plan.execution_names):
        raise ValueError("冻结执行源码不完整")
    for name, source in execution.items():
        path = checks._within(directory, source.get("path"), name)
        fingerprint = checks._fingerprint(source.get("sha256"), name)
        if base.sha256(path) != fingerprint or base.sha256(ROOT / f"algorithm/experiments/{name}.py") != fingerprint:
            raise ValueError("归档或当前执行源码改变")
    if manifest.get("runner_sha256") != execution["run_optimization_experiments"]["sha256"]:
        raise ValueError("worker源码绑定不一致")
    baseline = {n.removeprefix("algorithm/src/"): checks._git("show", f"{BASELINE_COMMIT}:{n}")
                for n in checks._git("ls-tree", "-r", "--name-only", BASELINE_COMMIT, "--", "algorithm/src/uav_dispatch").decode().splitlines() if n.endswith(".py")} if git_baseline else None
    for variant, flags in plan.variants.items():
        source = manifest["source_snapshots"][variant]
        if (source.get("config") != flags or any(type(source["config"][k]) is not type(v) for k, v in flags.items())
                or source.get("source_ref") != (BASELINE_COMMIT if variant == "baseline" else "working_tree")):
            raise ValueError("候选配置或源码引用改变")
        path = checks._within(directory, source.get("path"), variant)
        fingerprint = checks._fingerprint(source.get("sha256"), variant)
        if base.sha256(path) != fingerprint:
            raise ValueError("求解器快照指纹改变")
        if git_baseline:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                if len(set(names)) != len(names) or any(not n.startswith("uav_dispatch/") or not n.endswith(".py") or len(Path(n).parts) != 2 or ".." in Path(n).parts for n in names):
                    raise ValueError("非法快照成员")
                files = {n: archive.read(n) for n in names}
            if set(files) != set(baseline) or (variant == "baseline" and files != baseline):
                raise ValueError("基线Git字节或模块集合不一致")
        hashes[variant] = fingerprint
    return hashes, {n: s["sha256"] for n, s in execution.items()}


def verify(output, manifest, *, plan):
    _manifest(manifest, plan)
    if manifest.get("prior_bindings") != cohort_evidence(output, plan):
        raise ValueError("此前批次冻结证据改变")
    if manifest["input_sha256"] != base.sha256(ROOT / base.INPUT):
        raise ValueError("官方输入改变")
    _sources(output, manifest, plan)
    cells, paths = set(), set()
    for row in base.read_json(output / "runs.json"):
        _row_protocol(row, plan)
        if _cell(row) in cells:
            raise ValueError("重复实验单元")
        cells.add(_cell(row))
        if row.get("source_sha256") != manifest["source_snapshots"][row["variant"]]["sha256"]:
            raise ValueError("运行来源指纹改变")
        path = _row_path(output, row)
        paths.add(path)
        if base.sha256(path) != row["solution_sha256"]:
            raise ValueError("已有解指纹改变")
    if any(p.resolve() not in paths for p in (output / "run_solutions").glob("*.json")):
        raise RuntimeError("未登记孤立解，禁止覆盖")
    aborted = [_cell(r) for r in _aborted(output)]
    if cells.intersection(aborted) or len(set(aborted)) != len(aborted):
        raise ValueError("中止单元重复或与完整单元重合")
    first.experiment_seconds(output)


def _complete(rows, stage, plan):
    return all({b["seed"] for b, _ in first.pairs(rows, stage, plan.candidate, case)} == set(plan.seeds[stage]) for case in CASES[stage])


def screen_pass(rows, *, plan):
    return (_complete(rows, "screen", plan) and first.wins(first.pairs(rows, "screen", plan.candidate)) >= 1
            and (plan.allow_screen_regressions or all(base.score(c) <= base.score(b) for case in CASES["screen"] for b, c in first.pairs(rows, "screen", plan.candidate, case))))


def medium_pass(rows, *, plan):
    return _complete(rows, "medium", plan) and first.wins(first.pairs(rows, "medium", plan.candidate)) >= 2


def stage(output, manifest, name, *, plan):
    verify(output, manifest, plan=plan)
    rows, aborted = base.read_json(output / "runs.json"), _aborted(output)
    cells = {_cell(r) for r in rows}
    spent = sum(r["end_to_end_seconds"] for r in rows if r["stage"] == name) + sum(r.get("process_elapsed_seconds", 0.) for r in aborted if r.get("stage") == name)
    missing = sum(BUDGETS[name][case] for s in plan.seeds[name] for case in CASES[name] for v in plan.variants if (name, case, v, s) not in cells)
    prior = manifest["prior_bindings"]["prior_experiment_seconds"]
    if spent + missing > CAPS[name] or prior + first.experiment_seconds(output) + missing > TOTAL_SECONDS - RESERVED_SECONDS:
        raise RuntimeError("共享预算不足以预留全部剩余单元与审计")
    for index, seed in enumerate(plan.seeds[name]):
        order = list(plan.variants) if index % 2 == 0 else list(reversed(plan.variants))
        for case in CASES[name]:
            for variant in order:
                cell = (name, case, variant, seed)
                if cell in cells:
                    continue
                if cell in {_cell(r) for r in aborted}:
                    raise RuntimeError("禁止重跑中止单元")
                if (output / f"run_solutions/{name}__{case}__{variant}__{seed}.json").exists():
                    raise RuntimeError("孤立解已存在，禁止覆盖")
                budget = BUDGETS[name][case]
                if spent + budget > CAPS[name] or prior + first.experiment_seconds(output) + budget > TOTAL_SECONDS - RESERVED_SECONDS:
                    raise RuntimeError("累计共享预算不足")
                row = base.invoke(output, manifest, variant, case, seed, name, budget)
                row["order"] = order.index(variant)
                rows.append(row)
                cells.add(cell)
                spent += row["end_to_end_seconds"]
                base.save_rows(output, rows)
    return rows


def run(output, *, plan):
    manifest = base.read_json(output / "manifest.json")
    rows = stage(output, manifest, "screen", plan=plan)
    screened = screen_pass(rows, plan=plan)
    first._decision(output, "screen", candidates={plan.candidate: screened}, eligible_variants=[plan.candidate] if screened else [], reason=plan.protocol["screen_gate"])
    if screened:
        rows = stage(output, manifest, "medium", plan=plan)
    selected = plan.candidate if screened and medium_pass(rows, plan=plan) else None
    first._decision(output, "medium", selected_variant=selected, passed=selected is not None, skipped=not screened,
                    candidates={plan.candidate: medium_pass(rows, plan=plan)} if screened else {}, continuation_after_regression=True, reason=plan.protocol["medium_gate"])
    summary = base.summarize(output)
    summary.update(experiment_seconds=first.experiment_seconds(output), shared_experiment_seconds=manifest["prior_bindings"]["prior_experiment_seconds"] + first.experiment_seconds(output),
                   selected_variant=selected, default_eligible=selected is not None, exploratory_promotion=True)
    base.write_json(output / "summary.json", summary)
    return summary


def _row_protocol(row, plan):
    stage, case, variant, seed = _cell(row)
    if stage not in plan.seeds or case not in CASES[stage] or variant not in plan.variants or type(seed) is not int or seed not in plan.seeds[stage]:
        raise ValueError("实验单元不属于预定协议")
    names = list(plan.variants) if plan.seeds[stage].index(seed) % 2 == 0 else list(reversed(plan.variants))
    if row.get("budget_seconds") != BUDGETS[stage][case] or row.get("scenario") != "pure_direct" or row.get("order") != names.index(variant):
        raise ValueError("运行场景、预算或配对顺序改变")


def _row_path(directory, row):
    run_id = "__".join(str(v) for v in (row["stage"], row["case_id"], row["variant"], row["seed"]))
    if row.get("run_id") != run_id or row.get("solution_file") != f"run_solutions/{run_id}.json":
        raise ValueError("解文件身份与运行单元不一致")
    return checks._within(directory, row["solution_file"], "solution")


def _configuration(row, payload, plan):
    expected = {"max_iterations": 10_000_000, "time_limit_seconds": max(1e-6, row["budget_seconds"] - row["construction_seconds"] - 1.),
                "seed": row["seed"], "candidate_limit": 48, "relay_enabled": False, "relay_direct_warmup_fraction": .8,
                "enable_home_seed": False, "enable_home_bias": False, **plan.variants[row["variant"]]}
    config = row.get("config")
    if not isinstance(config, dict) or set(config) != set(expected) or any(config[k] != v or type(config[k]) is not type(v) for k, v in expected.items() if k != "time_limit_seconds"):
        raise ValueError("执行配置与冻结候选不一致")
    checks._same(expected["time_limit_seconds"], config["time_limit_seconds"], "configured time")
    metadata = payload.get("metadata", {})
    flags = {k: expected[k] for k in ("seed", "relay_enabled", "enable_home_seed", *plan.variants[row["variant"]])}
    if row["variant"] == plan.candidate:
        flags.update(dict.fromkeys(plan.previous_flags, False))
    for key, value in flags.items():
        if metadata.get(key) != value or type(metadata.get(key)) is not type(value):
            raise ValueError(f"保存配置元数据不一致: {key}")
    checks._same(expected["time_limit_seconds"], metadata.get("time_limit_seconds"), "metadata time")
    if row["iterations"] > expected["max_iterations"]:
        raise ValueError("运行超出迭代上限")


def _audit_decisions(directory, rows, plan):
    # Independent pairing and gates: do not call the runner's gate functions.
    def paired(stage, case="fleet200"):
        groups = {v: {r["seed"]: r for r in rows if r["stage"] == stage and r["case_id"] == case and r["variant"] == v and r["valid"] and r["budget_compliant"]} for v in plan.variants}
        return [(groups["baseline"][s], groups[plan.candidate][s]) for s in sorted(groups["baseline"].keys() & groups[plan.candidate].keys())]
    def complete(stage):
        return all({b["seed"] for b, _ in paired(stage, case)} == set(plan.seeds[stage]) for case in CASES[stage])
    score = original._score
    screen = bool(complete("screen") and sum(score(c) < score(b) for b, c in paired("screen")) >= 1
                  and (plan.allow_screen_regressions or all(score(c) <= score(b) for case in CASES["screen"] for b, c in paired("screen", case))))
    medium = bool(complete("medium") and sum(score(c) < score(b) for b, c in paired("medium")) >= 2)
    selected = plan.candidate if screen and medium else None
    path = directory / "decisions.json"
    decisions = base.read_json(path)
    if not isinstance(decisions, list) or any(not isinstance(d, dict) for d in decisions):
        raise ValueError("非法阶段决定")
    stages = {d.get("stage"): d for d in decisions}
    if len(stages) != len(decisions) or not set(stages).issubset(plan.seeds):
        raise ValueError("阶段决定重复或未知")
    expected = {"screen": {"candidates": {plan.candidate: screen}, "eligible_variants": [plan.candidate] if screen else []},
                "medium": {"selected_variant": selected, "passed": selected is not None, "skipped": not screen,
                           "candidates": {plan.candidate: medium} if screen else {}, "continuation_after_regression": True}}
    for stage in plan.seeds:
        actual = {_cell(r) for r in rows if r["stage"] == stage}
        wanted = {(stage, case, v, s) for case in CASES[stage] for v in plan.variants for s in plan.seeds[stage]} if stage == "screen" or screen else set()
        if not actual.issubset(wanted):
            raise ValueError("未过初筛的候选进入验证")
        if stage in stages and (actual != wanted or any(stages[stage].get(k) != v or type(stages[stage].get(k)) is not type(v) for k, v in expected[stage].items())):
            raise ValueError("阶段决定与独立重算门槛不一致")
    if "medium" in stages and "screen" not in stages:
        raise ValueError("阶段决定缺少前置阶段")
    return {"screen": {plan.candidate: screen}, "eligible_variants": [plan.candidate] if screen else [], "medium": {plan.candidate: medium},
            "selected_variant": selected, "default_eligible": selected is not None, "exploratory_promotion": True,
            "protocol_complete": set(stages) == set(plan.seeds), "decisions_sha256": base.sha256(path)}


def _audit_budget(directory, rows, prior, plan):
    stages = {s: {"completed_seconds": 0., "aborted_seconds": 0., "cap_seconds": cap} for s, cap in CAPS.items()}
    cells = {_cell(r) for r in rows}
    for row in rows:
        stages[row["stage"]]["completed_seconds"] += checks._number(row["end_to_end_seconds"], "batch elapsed")
    attempts = _aborted(directory)
    if not isinstance(attempts, list):
        raise ValueError("非法中止记录")
    for attempt in attempts:
        stage, case, variant, seed = _cell(attempt)
        names = list(plan.variants) if seed in plan.seeds.get(stage, [])[::2] else list(reversed(plan.variants))
        _row_protocol({**attempt, "budget_seconds": BUDGETS.get(stage, {}).get(case), "scenario": "pure_direct", "order": names.index(variant) if variant in names else None}, plan)
        if _cell(attempt) in cells:
            raise ValueError("完整或中止单元重复")
        cells.add(_cell(attempt))
        stages[stage]["aborted_seconds"] += checks._number(attempt.get("process_elapsed_seconds"), "aborted elapsed")
    for bucket in stages.values():
        bucket["total_seconds"] = bucket["completed_seconds"] + bucket["aborted_seconds"]
        bucket["budget_compliant"] = bucket["total_seconds"] <= bucket["cap_seconds"] + 1e-9
    batch = sum(b["total_seconds"] for b in stages.values())
    total = batch + prior["prior_experiment_seconds"]
    reserve = total <= TOTAL_SECONDS - RESERVED_SECONDS + 1e-9
    return {"stages": stages, "batch_seconds": batch, "prior_seconds": prior["prior_experiment_seconds"], "total_seconds": total,
            "total_limit_seconds": TOTAL_SECONDS, "reserved_seconds": RESERVED_SECONDS, "reserve_preserved": reserve,
            "aborted_run_count": len(attempts), "budget_compliant": reserve and all(b["budget_compliant"] for b in stages.values())}


def audit_results(output_dir, *, plan):
    directory = Path(output_dir).resolve()
    target = directory / "validation.json"
    target.unlink(missing_ok=True)
    manifest, rows = base.read_json(directory / "manifest.json"), base.read_json(directory / "runs.json")
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ValueError("非法manifest或运行列表")
    _manifest(manifest, plan)
    prior = cohort_evidence(directory, plan)
    if manifest.get("prior_bindings") != prior:
        raise ValueError("此前批次冻结证据改变")
    hashes, execution = _sources(directory, manifest, plan, git_baseline=True)
    paths = {_row_path(directory, r) for r in rows}
    if any(p.resolve() not in paths for p in (directory / "run_solutions").glob("*.json")):
        raise ValueError("未登记孤立解")
    verified = original._audit_rows(directory, manifest, rows, hashes, lambda r: _row_protocol(r, plan), lambda r, p: _configuration(r, p, plan))
    budget, decisions = _audit_budget(directory, rows, prior, plan), _audit_decisions(directory, rows, plan)
    successful = bool(rows) and not verified["invalid_rows"] and not verified["over_budget_rows"] and not budget["aborted_run_count"]
    result = {"schema_version": 7, "passed": successful and budget["budget_compliant"] and decisions["protocol_complete"],
              "evidence_verified": True, "prior_evidence_verified": True, "validated_at_utc": datetime.now(timezone.utc).isoformat(),
              "run_count": len(rows), "method": "Official-CSV _force_full rescoring, independent gates and bound prior audited cohorts; no solver run",
              **verified, "all_runs_successful": successful, "protocol_complete": decisions["protocol_complete"],
              "manifest_sha256": base.sha256(directory / "manifest.json"), "runs_sha256": base.sha256(directory / "runs.json"),
              "decisions_sha256": decisions["decisions_sha256"], "source_snapshots_sha256": hashes, "execution_sources_sha256": execution,
              "audit_script_sha256": base.sha256(Path(__file__)), "prior_bindings": prior,
              "cumulative_budget": budget, "decision_audit": decisions}
    base.write_json(target, result)
    return result
