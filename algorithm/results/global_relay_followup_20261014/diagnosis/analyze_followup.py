"""Diagnose saved follow-up trajectories; no solver imports or new searches.

Run only after the complete batch passes its independent audit.  This script
reads immutable evidence and writes diagnosis/followup_analysis.json.  Times
identify phase boundaries only; incumbent prefixes are compared exactly as
(iteration, late count, total lateness, distance), without their timestamps.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SCORE_FIELDS = ("late_count", "total_lateness_min", "distance_km")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def score(row):
    return [row[key] for key in SCORE_FIELDS]


def delta(before, after):
    """Componentwise after minus before; comparison remains lexicographic."""
    return [b - a for a, b in zip(before, after, strict=True)]


def outcome(before, after):
    return "win" if tuple(after) < tuple(before) else "loss" if tuple(after) > tuple(before) else "tie"


def direct_phase(row, payload):
    metadata = payload["metadata"]
    staged = metadata.get("staged_relay", False)
    completed = metadata["relay_warmup_iterations"] if staged else row["iterations"]
    runtime = metadata["relay_warmup_runtime_seconds"] if staged else payload["runtime_seconds"]
    all_points = metadata["best_trajectory"]
    # Relay seeding can improve at local iteration zero, giving an adjusted
    # iteration equal to warmup's last iteration.  The strict time boundary
    # therefore matters; iteration alone cannot separate these two phases.
    points = [p for p in all_points if p[0] <= completed and p[1] < runtime]
    phase_score = metadata["relay_warmup_score"] if staged else score(row)
    if not points or list(points[-1][2:]) != list(phase_score):
        raise ValueError(f"Cannot isolate complete DIRECT incumbent trajectory: {row['run_id']}")
    terminal = metadata.get("terminal_polish", {})
    before = list(terminal.get("before_score", phase_score))
    after = list(terminal.get("after_score", phase_score))
    accepted = bool(terminal.get("accepted", False))
    removed_point = None
    if after != list(phase_score) or accepted != (tuple(after) < tuple(before)):
        raise ValueError(f"Inconsistent terminal checkpoint: {row['run_id']}")
    if accepted:
        if points[-1][0] != completed or len(points) < 2 or list(points[-2][2:]) != before:
            raise ValueError(f"Terminal improvement is not the final DIRECT point: {row['run_id']}")
        removed_point = list(points.pop())
    if list(points[-1][2:]) != before:
        raise ValueError(f"Pre-terminal incumbent is missing: {row['run_id']}")
    fraction = metadata.get("relay_direct_warmup_fraction", 1.) if staged else 1.
    phase_budget = row["config"]["time_limit_seconds"] * fraction
    return {
        "run_id": row["run_id"], "stage": row["stage"], "case_id": row["case_id"],
        "variant": row["variant"], "seed": row["seed"], "staged_relay": staged,
        "solution_file": row["solution_file"], "solution_sha256": row["solution_sha256"],
        "main_search_reported_iterations": completed,
        "conservative_completed_iteration_limit": max(0, completed - 1),
        "main_search_score_before_terminal": before, "direct_phase_final_score": list(phase_score),
        "final_score": score(row), "direct_runtime_seconds": runtime,
        "direct_budget_fraction": fraction, "direct_phase_budget_seconds": phase_budget,
        "relay_runtime_seconds": metadata.get("relay_search_runtime_seconds", 0.),
        "relay_reported_iterations": metadata.get("relay_search_iterations", 0),
        "terminal_accepted": accepted, "terminal_delta": delta(before, after),
        "terminal_reserved_seconds": terminal.get("reserved_seconds", 0.),
        "terminal_elapsed_seconds": terminal.get("elapsed_seconds", 0.),
        "terminal_statistics": terminal.get("statistics", {}),
        "terminal_removed_trajectory_point": removed_point,
        "excluded_relay_points": len(all_points) - len(points) - int(accepted),
        "main_search_incumbent_points": [[p[0], *p[2:]] for p in points],
    }


def compare(reference, candidate, *, prefix_role):
    cutoff = min(reference["conservative_completed_iteration_limit"],
                 candidate["conservative_completed_iteration_limit"])
    prefixes = [[p for p in run["main_search_incumbent_points"] if p[0] <= cutoff]
                for run in (reference, candidate)]
    if not all(prefixes):
        raise ValueError("A DIRECT trajectory lacks its iteration-zero incumbent")
    shared = 0
    for left, right in zip(*prefixes):
        if left != right:
            break
        shared += 1
    first_difference = None
    if prefixes[0] != prefixes[1]:
        first_difference = {
            "point_index": shared,
            "reference": prefixes[0][shared] if shared < len(prefixes[0]) else None,
            "candidate": prefixes[1][shared] if shared < len(prefixes[1]) else None,
        }
    before_reference = reference["main_search_score_before_terminal"]
    before_candidate = candidate["main_search_score_before_terminal"]
    return {
        "stage": candidate["stage"], "case_id": candidate["case_id"], "seed": candidate["seed"],
        "reference_run_id": reference["run_id"], "candidate_run_id": candidate["run_id"],
        "reference_variant": reference["variant"], "candidate_variant": candidate["variant"],
        "prefix_role": prefix_role,
        "reference_main_search_iterations": reference["main_search_reported_iterations"],
        "candidate_main_search_iterations": candidate["main_search_reported_iterations"],
        "main_search_iteration_delta": candidate["main_search_reported_iterations"] - reference["main_search_reported_iterations"],
        "common_completed_iteration_limit": cutoff,
        "common_incumbent_prefix_equal": prefixes[0] == prefixes[1],
        "reference_prefix_entries": len(prefixes[0]), "candidate_prefix_entries": len(prefixes[1]),
        "shared_prefix_entries": shared, "first_prefix_difference": first_difference,
        "reference_incumbent_at_common_iteration": prefixes[0][-1][1:],
        "candidate_incumbent_at_common_iteration": prefixes[1][-1][1:],
        "reference_main_search_final_score": before_reference,
        "candidate_main_search_final_score": before_candidate,
        "main_search_endpoint_outcome": outcome(before_reference, before_candidate),
        "main_search_endpoint_delta": delta(before_reference, before_candidate),
        "candidate_terminal_accepted": candidate["terminal_accepted"],
        "candidate_terminal_delta": candidate["terminal_delta"],
        "direct_phase_net_delta": delta(reference["direct_phase_final_score"], candidate["direct_phase_final_score"]),
        "reference_final_score": reference["final_score"], "candidate_final_score": candidate["final_score"],
        "net_pair_outcome": outcome(reference["final_score"], candidate["final_score"]),
        "net_pair_delta": delta(reference["final_score"], candidate["final_score"]),
        "reference_direct_budget_fraction": reference["direct_budget_fraction"],
        "candidate_direct_budget_fraction": candidate["direct_budget_fraction"],
        "reference_direct_phase_budget_seconds": reference["direct_phase_budget_seconds"],
        "candidate_direct_phase_budget_seconds": candidate["direct_phase_budget_seconds"],
        "reference_direct_runtime_seconds": reference["direct_runtime_seconds"],
        "candidate_direct_runtime_seconds": candidate["direct_runtime_seconds"],
        "reference_relay_runtime_seconds": reference["relay_runtime_seconds"],
        "candidate_relay_runtime_seconds": candidate["relay_runtime_seconds"],
    }


def summarize(pairs):
    losses = [p for p in pairs if p["net_pair_outcome"] == "loss"]
    return {
        "pairs": len(pairs), "common_prefix_equal_pairs": sum(p["common_incumbent_prefix_equal"] for p in pairs),
        "wins": sum(p["net_pair_outcome"] == "win" for p in pairs),
        "ties": sum(p["net_pair_outcome"] == "tie" for p in pairs), "losses": len(losses),
        "candidate_terminal_improved_pairs": sum(p["candidate_terminal_accepted"] for p in pairs),
        "candidate_fewer_main_iterations_pairs": sum(p["main_search_iteration_delta"] < 0 for p in pairs),
        "candidate_more_main_iterations_pairs": sum(p["main_search_iteration_delta"] > 0 for p in pairs),
        "losses_with_equal_common_prefix": sum(p["common_incumbent_prefix_equal"] for p in losses),
        "losses_with_fewer_main_iterations": sum(p["main_search_iteration_delta"] < 0 for p in losses),
        "losses_despite_terminal_improvement": sum(p["candidate_terminal_accepted"] for p in losses),
        "losses_with_worse_preterminal_endpoint": sum(p["main_search_endpoint_outcome"] == "loss" for p in losses),
        "net_late_count_delta_sum": sum(p["net_pair_delta"][0] for p in pairs),
        "terminal_late_count_delta_sum": sum(p["candidate_terminal_delta"][0] for p in pairs),
    }


def analyze(output_dir):
    directory = Path(output_dir).resolve()
    bindings = {}

    def bind(path, expected=None):
        fingerprint = sha(path)
        if expected is not None and fingerprint != expected:
            raise ValueError(f"Input evidence changed: {path}")
        key = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
        bindings[key] = fingerprint

    validation_path = directory / "validation.json"
    validation = read(validation_path)
    if validation.get("passed") is not True or validation.get("protocol_complete") is not True:
        raise ValueError("Final diagnosis requires the complete independently validated batch")
    bind(validation_path)
    for name in ("manifest", "runs", "decisions"):
        bind(directory / f"{name}.json", validation[f"{name}_sha256"])
    manifest, rows = read(directory / "manifest.json"), read(directory / "runs.json")
    if len(rows) != validation["run_count"]:
        raise ValueError("Validated run count changed")
    for spec in [*manifest["source_snapshots"].values(), *manifest["execution_sources"].values()]:
        bind(directory / spec["path"], spec["sha256"])
    bind(ROOT / manifest["input_path"], manifest["input_sha256"])
    for name in ("environment_notes", "execution_notes"):
        path = directory / f"{name}.json"
        if path.exists():
            expected = (validation.get("environment_notes_sha256") if name == "environment_notes"
                        else validation["cumulative_budget"].get("execution_notes_sha256"))
            bind(path, expected)
    runs = []
    for row in rows:
        path = directory / row["solution_file"]
        bind(path, row["solution_sha256"])
        if row["stage"] in ("screen", "validate"):
            runs.append(direct_phase(row, read(path)))
    index = {(r["stage"], r["case_id"], r["variant"], r["seed"]): r for r in runs}
    terminal_pairs, rebalanced_pairs = [], []
    for candidate in runs:
        if candidate["variant"] == "baseline":
            continue
        key = candidate["stage"], candidate["case_id"], "baseline", candidate["seed"]
        pair = compare(index[key], candidate, prefix_role=(
            "terminal vs baseline common-iteration diagnostic"
            if candidate["variant"] == "terminal" else
            "separate exploratory check: 95% DIRECT plus seed checkpoint changes the phase allocation"))
        (terminal_pairs if candidate["variant"] == "terminal" else rebalanced_pairs).append(pair)
    stages = [s for s in ("screen", "validate") if any(r["stage"] == s for r in runs)]
    cases = manifest["protocol"]["cases"]
    summary = summarize(terminal_pairs)
    return {
        "kind": "read_only_followup_common_iteration_diagnosis", "schema_version": 1,
        "source_directory": str(directory.relative_to(ROOT)) if directory.is_relative_to(ROOT) else str(directory),
        "script_sha256": sha(Path(__file__)), "input_bindings": bindings, "no_new_searches": True,
        "batch_protocol_complete": True, "validated_run_count": len(rows),
        "score_order": list(SCORE_FIELDS), "delta_convention": "candidate minus reference; negative is better in that component; outcomes use the whole lexicographic tuple",
        "method": {
            "diagnostic_status": "Post-hoc mechanism diagnosis, specified while the screen batch was running; it does not change the frozen experiment gates or constitute a new confirmatory test.",
            "phase_boundary": "Use payload metadata timestamps (no construction offset), retain iteration <= warmup iterations and time strictly below warmup runtime; excludes relay seeding at adjusted warmup iteration.",
            "terminal_removal": "If accepted, remove exactly the last DIRECT improvement after matching its score and preceding incumbent to terminal metadata.",
            "common_iteration_limit": "max(0, min(reference completed_iterations, candidate completed_iterations) - 1); excludes a possibly timed-out final iteration in either run, while retaining the initialization point.",
            "prefix_comparison": "Exact ordered (iteration, late count, total lateness, distance) entries up to the common limit; ignore all timestamps.",
            "scope": "Recorded best-incumbent prefixes, not every accepted state, route, random draw or operator decision.",
            "validation_selection": "Validate has only the prespecified selected variant; absent terminal relay pairs are not inferred or substituted with rebalanced pairs.",
        },
        "terminal_vs_baseline": terminal_pairs,
        "terminal_summary": summary,
        "terminal_by_stage": {s: summarize([p for p in terminal_pairs if p["stage"] == s]) for s in stages},
        "terminal_by_stage_and_case": {s: {c: summarize([p for p in terminal_pairs if p["stage"] == s and p["case_id"] == c])
                                          for c in cases} for s in stages},
        "rebalanced_vs_baseline_separate": rebalanced_pairs,
        "rebalanced_separate_summary": summarize(rebalanced_pairs),
        "runs": runs,
        "evidence_summary": [
            f"Terminal has {summary['pairs']} recorded baseline pairs; {summary['common_prefix_equal_pairs']} have identical observed DIRECT incumbent prefixes through the conservative common iteration.",
            f"Of {summary['losses']} net terminal losses, {summary['losses_with_equal_common_prefix']} share that common prefix, {summary['losses_with_fewer_main_iterations']} end at fewer DIRECT iterations, and {summary['losses_despite_terminal_improvement']} still received a strict terminal improvement.",
            "Equal prefixes with different endpoint scores are consistent with different wall-clock cutoff positions on the recorded incumbent sequence. They do not establish the cause of the runtime difference.",
            "Terminal's before/after gain is a local observed change, while the paired final score includes main-search cutoff effects and, for staged cases, the subsequent relay search.",
        ],
        "limitations": [
            "Do not attribute iteration-count or score differences separately to host noise, the reserved slice, or implementation speed: this batch did not isolate those causes.",
            "An equal incumbent prefix is not proof that all internal search states, RNG draws or operator decisions were identical.",
            "Rebalanced deliberately changes phase duration and enables the existing relay seed checkpoint. Its separate prefix observations are not pooled with the terminal-only comparison or treated as a pure ratio ablation.",
            "Componentwise deltas describe scores; they are not additive causal attributions, especially after the relay phase starts from a changed warmup incumbent.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=HERE.parent)
    args = parser.parse_args()
    result = analyze(args.output_dir)
    target = args.output_dir.resolve() / "diagnosis/followup_analysis.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"terminal": result["terminal_summary"], "output": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
