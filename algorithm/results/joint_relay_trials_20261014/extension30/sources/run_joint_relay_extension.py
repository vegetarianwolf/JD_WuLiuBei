"""User-requested 30-second diagnostic after a failed whole-solver screen."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import time

from algorithm.experiments import run_joint_relay_trials as parent

base, ROOT = parent.base, parent.ROOT
OUTPUT = ROOT / "algorithm/results/joint_relay_trials_20261014/extension30"
PARENT = parent.OUTPUT
BASELINE_COMMIT, VARIANTS = parent.BASELINE_COMMIT, parent.VARIANTS
PRIMARY = parent.PRIMARY
SEEDS = {"medium": [2026101931, 2026101932]}
CASES = {"medium": [PRIMARY]}
BUDGETS = {"medium": {PRIMARY: 30}}
CAPS = {"medium": 120}
TOTAL_SECONDS, RESERVED_SECONDS = parent.TOTAL_SECONDS, parent.RESERVED_SECONDS
EXECUTION_NAMES = (*parent.EXECUTION_NAMES, "run_joint_relay_extension")
case_spec, requested_config = parent.case_spec, parent.requested_config
PROTOCOL = {**parent.PROTOCOL, "stage_cases": CASES, "stage_budgets_seconds": BUDGETS,
            "stage_caps_seconds": CAPS, "default_gate": "两组追加仅诊断启动慢，不构成三种子多数胜；默认不替换",
            "continuation": "用户显式选择在初筛失败后追加两个30秒新配对，全部完成，结果单独保存",
            "user_choices": [*parent.PROTOCOL["user_choices"], "初筛失败后追加少量30秒配对观察启动慢"],
            "shared_budget": "首批与追加共用540秒上限，预留30秒；追加最多4次、120秒"}


def prepare(output):
    if output.exists() and any(output.iterdir()):
        raise ValueError("新追加目录必须为空")
    documents = {n: base.read_json(PARENT / f"{n}.json") for n in ("manifest", "runs", "decisions", "validation")}
    if (not documents["validation"].get("passed") or not documents["validation"].get("protocol_complete")
            or parent.screen_pass(documents["runs"]) or len(documents["runs"]) != 12):
        raise ValueError("仅支持已核验且未通过初筛的完整首批")
    for name in ("manifest", "runs", "decisions"):
        if documents["validation"].get(f"{name}_sha256") != base.sha256(PARENT / f"{name}.json"):
            raise ValueError("首批审计已过期")
    output.mkdir(parents=True, exist_ok=True)
    (output / "sources").mkdir()
    snapshots = {}
    for variant, source in documents["manifest"]["source_snapshots"].items():
        shutil.copyfile(PARENT / source["path"], output / source["path"])
        snapshots[variant] = dict(source)
    execution = {}
    for name in EXECUTION_NAMES:
        path = f"sources/{name}.py"
        (output / path).write_bytes((ROOT / f"algorithm/experiments/{name}.py").read_bytes())
        execution[name] = dict(path=path, sha256=base.sha256(output / path))
    manifest = dict(kind="joint_relay_extension", schema_version=1, baseline_variant="baseline",
                    baseline_commit=BASELINE_COMMIT, input_path=base.INPUT,
                    input_sha256=documents["manifest"]["input_sha256"], source_snapshots=snapshots,
                    execution_sources=execution, seeds=SEEDS, protocol=PROTOCOL,
                    parent_directory="../batch1",
                    parent_bindings={n: base.sha256(PARENT / f"{n}.json") for n in documents},
                    prior_seconds=parent.spent(PARENT, documents["runs"]),
                    created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    environment=documents["manifest"]["environment"])
    for name, value in (("manifest", manifest), ("runs", []), ("decisions", [])):
        base.write_json(output / f"{name}.json", value)
    return manifest


def verify(output, manifest):
    if (manifest.get("kind") != "joint_relay_extension" or manifest.get("schema_version") != 1
            or manifest.get("seeds") != SEEDS or manifest.get("protocol") != PROTOCOL
            or manifest.get("baseline_commit") != BASELINE_COMMIT
            or manifest.get("parent_directory") != "../batch1"):
        raise ValueError("追加协议改变")
    if base.sha256(ROOT / base.INPUT) != manifest["input_sha256"]:
        raise ValueError("官方输入改变")
    for name, fingerprint in manifest["parent_bindings"].items():
        if base.sha256(PARENT / f"{name}.json") != fingerprint:
            raise ValueError("首批证据改变")
    for source in [*manifest["source_snapshots"].values(), *manifest["execution_sources"].values()]:
        path = (output / source["path"]).resolve()
        if not path.is_relative_to(output.resolve()) or base.sha256(path) != source["sha256"]:
            raise ValueError("追加归档源码改变")
    for name in EXECUTION_NAMES:
        if base.sha256(ROOT / f"algorithm/experiments/{name}.py") != manifest["execution_sources"][name]["sha256"]:
            raise ValueError("执行源码改变")


def invoke(output, manifest, variant, case, seed):
    # The immutable parent worker owns the exact same construction and solver
    # configuration. Only the declared seeds, cohort and total budgets differ.
    from zipfile import ZipFile
    import os
    import subprocess
    import sys
    import tempfile
    run_id = f"medium__{case}__{variant}__{seed}"
    relative = f"run_solutions/{run_id}.json"
    if (output / relative).exists():
        raise ValueError("已有解不能覆盖")
    source = manifest["source_snapshots"][variant]
    with tempfile.TemporaryDirectory(prefix="joint-relay-30-") as temporary:
        temp = Path(temporary)
        with ZipFile(output / source["path"]) as archive:
            if any(len(Path(n).parts) != 2 or not n.startswith("uav_dispatch/") or not n.endswith(".py") for n in archive.namelist()):
                raise ValueError("非法源码归档")
            archive.extractall(temp / "src")
        request = dict(stage="medium",case_id=case,variant=variant,seed=seed,source_sha256=source["sha256"],
                       budget_seconds=30,solution_path=str(output/relative),response_path=str(temp/"response.json"))
        base.write_json(temp/"request.json",request)
        env={**os.environ,"PYTHONPATH":os.pathsep.join((str(temp/"src"),str(ROOT))),"PYTHONDONTWRITEBYTECODE":"1"}
        clock=time.perf_counter()
        try:
            subprocess.run([sys.executable,str(Path(parent.__file__).resolve()),"--worker",str(temp/"request.json")],
                           cwd=ROOT,env=env,check=True,timeout=60)
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as error:
            path=output/"execution_notes.json"
            notes=base.read_json(path) if path.exists() else {}
            notes.setdefault("aborted_runs",[]).append(dict(stage="medium",case_id=case,variant=variant,seed=seed,
                process_elapsed_seconds=time.perf_counter()-clock,reason=str(error)))
            base.write_json(path,notes)
            raise
        row=base.read_json(temp/"response.json")
    row.update(run_id=run_id,solution_file=relative)
    print(f"extension {run_id}: {base.score(row)} {row['end_to_end_seconds']:.2f}s",flush=True)
    return row


def run(output):
    manifest=base.read_json(output/"manifest.json")
    verify(output,manifest)
    rows=base.read_json(output/"runs.json")
    cells={parent._cell(r) for r in rows}
    aborted=output/"execution_notes.json"
    attempts=base.read_json(aborted).get("aborted_runs",[]) if aborted.exists() else []
    for r in rows:
        if base.sha256(output/r["solution_file"])!=r["solution_sha256"]:
            raise ValueError("已有追加解指纹改变")
    missing=sum(30 for seed in SEEDS["medium"] for variant in VARIANTS if ("medium",PRIMARY,variant,seed) not in cells)
    if (parent.spent(output,rows)+missing>120 or manifest["prior_seconds"]+parent.spent(output,rows)+missing>TOTAL_SECONDS-RESERVED_SECONDS):
        raise ValueError("共享预算不足以预留所有追加配对")
    for i,seed in enumerate(SEEDS["medium"]):
        order=list(VARIANTS) if i%2==0 else list(reversed(VARIANTS))
        for variant in order:
            cell=("medium",PRIMARY,variant,seed)
            if cell in cells:
                continue
            if any(parent._cell(r)==cell for r in attempts):
                raise ValueError("中止单元不能重跑择优")
            row=invoke(output,manifest,variant,PRIMARY,seed)
            row["order"]=order.index(variant)
            rows.append(row)
            base.save_rows(output,rows)
            cells.add(cell)
    base.decision(output,"medium",selected_variant="joint",passed=False,diagnostic_only=True,
                  reason=PROTOCOL["default_gate"])
    base.summarize(output)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("prepare","run"))
    parser.add_argument("--output-dir",type=Path,default=OUTPUT)
    args=parser.parse_args(argv)
    prepare(args.output_dir.resolve()) if args.command=="prepare" else run(args.output_dir.resolve())


if __name__=="__main__":
    main()
