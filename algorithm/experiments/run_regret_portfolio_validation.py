"""Explicit post-hoc validation of the other short-screen-qualified candidate.

The original failed batch remains immutable. New baselines are paired with
portfolio only in this separate cohort; no scores are selected across cohorts.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from algorithm.experiments import run_optimization_experiments as base
from algorithm.experiments import run_regret_followup as protocol


def parent_evidence(output):
    parent=output.parent
    rows=base.read_json(parent / "runs.json")
    decisions=base.read_json(parent / "decisions.json")
    if not protocol.screen_pass(rows,"portfolio"):
        raise ValueError("组合候选必须已通过原批次初筛")
    if protocol.medium_pass(rows,"fallback") or not any(
        d.get("stage")=="medium" and d.get("selected_variant")=="fallback" and d.get("passed") is False
        for d in decisions
    ):
        raise ValueError("父批次须记录位置回退候选中测失败")
    if not any(d.get("stage")=="extensions" for d in decisions):
        raise ValueError("先结束父批次，避免并行计时及变化中的证据")
    return rows


def prepare(output):
    parent_evidence(output)
    parent_manifest=base.read_json(output.parent / "manifest.json")
    # Validate the parent runner/input before creating any child artifacts.
    # Historical replay must restore the archived scripts, never mix versions.
    protocol.verify(output.parent,parent_manifest)
    if base.sha256(base.ROOT / parent_manifest["input_path"]) != parent_manifest["input_sha256"]:
        raise ValueError("父批次输入改变，不能追加验证")
    manifest=protocol.prepare(output)
    if any(manifest["source_snapshots"][v]["sha256"] != parent_manifest["source_snapshots"][v]["sha256"] for v in protocol.VARIANTS):
        raise ValueError("追加验证只能使用同一冻结源码")
    marker={"reason":"位置回退候选中测出现逾期数退化后，对已通过相同初筛的组合候选追加独立验证；这是事后探索，不更改原批次决定或择优复用旧基线。",
            "parent_directory":"..", "candidate":"portfolio",
            **{f"parent_{name}_sha256":base.sha256(output.parent / f"{name}.json") for name in ("manifest","runs","decisions")}}
    manifest["post_hoc_exploration"]=marker
    target=output / "sources/run_regret_portfolio_validation.py"
    target.write_bytes(Path(__file__).read_bytes())
    manifest["execution_sources"]["run_regret_portfolio_validation"]={"path":str(target.relative_to(output)),"sha256":base.sha256(target)}
    base.write_json(output / "manifest.json",manifest)
    base.decision(output,"screen",selected_variant="portfolio",inherited_qualification=True,
                  reason=marker["reason"],parent_runs_sha256=marker["parent_runs_sha256"])


def verify(output,manifest):
    protocol.verify(output,manifest)
    if base.sha256(Path(__file__)) != manifest["execution_sources"]["run_regret_portfolio_validation"]["sha256"]:
        raise ValueError("追加验证调度源码改变")
    marker=manifest["post_hoc_exploration"]
    for name in ("manifest","runs","decisions"):
        if base.sha256(output.parent / f"{name}.json") != marker[f"parent_{name}_sha256"]:
            raise ValueError("父批次证据改变，停止续跑")
    parent_evidence(output)


def consumed(output):
    seconds=0.
    for directory in (output.parent,output):
        seconds+=sum(r["end_to_end_seconds"] for r in base.read_json(directory / "runs.json"))
        notes=directory / "execution_notes.json"
        if notes.exists():
            seconds+=sum(r.get("process_elapsed_seconds",0.) for r in base.read_json(notes).get("aborted_runs",[]))
    probe=output.parent / "diagnostics/local_repair_probe.json"
    if probe.exists(): seconds+=base.read_json(probe).get("elapsed_seconds",0.)
    return seconds


def stage(output,manifest,name):
    verify(output,manifest)
    rows=base.read_json(output / "runs.json")
    # Reserve the entire not-yet-started phase at its nominal budgets. This
    # prevents a second cohort from gaining an extra independent one-hour cap.
    missing=sum(protocol.BUDGETS[name][case.split("__")[0]]
                for seed in protocol.SEEDS[name] for case in protocol.CASES[name]
                for variant in ("baseline","portfolio")
                if not any((r["stage"],r["case_id"],r["variant"],r["seed"]) == (name,case,variant,seed) for r in rows))
    if consumed(output)+missing>3540:
        raise RuntimeError("两批合计预算不足，预留60秒核验，不启动该阶段")
    return protocol.stage(output,manifest,name,["baseline","portfolio"])


def run(output):
    manifest=base.read_json(output / "manifest.json")
    rows=stage(output,manifest,"medium")
    passed=protocol.medium_pass(rows,"portfolio")
    base.decision(output,"medium",selected_variant="portfolio",passed=passed,
                  reason="独立追加批次；相同预定三种子，新基线按AB交替；不混入原候选的基线或均值")
    if passed:
        rows=stage(output,manifest,"confirm")
        base.decision(output,"confirm",selected_variant="portfolio",passed=protocol.confirm_pass(rows,"portfolio"),
                      early_stopped=base.confirmation_regressed(rows,"portfolio"),
                      completed_pairs=len(protocol.pairs(rows,"confirm","portfolio")),planned_pairs=4,
                      reason="先复测已知反例，再测三个新种子；任何配对退化均不晋级")
        stage(output,manifest,"extensions")
        base.decision(output,"extensions",selected_variant="portfolio",reason="独立小预算兼容验证")
    else:
        base.decision(output,"confirm",passed=False,skipped=True,reason="组合候选中测未过，不扩大")
        base.decision(output,"extensions",skipped=True,reason="中测未过，功能兼容以针对性测试验证")
    summary=base.summarize(output)
    summary["parent_and_supplement_experiment_seconds"]=consumed(output)
    base.write_json(output / "summary.json",summary)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("prepare","run"))
    parser.add_argument("--output-dir",type=Path,default=protocol.OUTPUT / "supplemental_trial")
    args=parser.parse_args(argv)
    (prepare if args.action=="prepare" else run)(args.output_dir.resolve())


if __name__=="__main__": main()
