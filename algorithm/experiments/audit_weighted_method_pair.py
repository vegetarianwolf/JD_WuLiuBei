"""Independent audit of the fresh weighted-method pair; standard library only.

Route arithmetic and bounded adapter reversal come from the earlier independent
audit, never from the production solver, validator, loader, or objective adapter.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile
from zoneinfo import ZoneInfo

try:
    from . import audit_weighted_direct_pair as common
except ImportError:
    import audit_weighted_direct_pair as common


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "algorithm/results/weighted_method_improvement_20261014"
ORDER = ["weighted_baseline", "weighted_exchange_lazy"]
SEED = 2026101461
CHANGED_MODULES = {"alns.py", "incumbent_polish.py", "direct_task_exchange.py"}
OVERRIDES = {"enable_lazy_repair": True, "enable_terminal_task_exchange": True}
FIELDS = common.FIELDS
read, sha, digest = common.read, common.sha, common.digest
require, same, finite, within = common.require, common.same, common.finite, common.within
score, walk = common.score, common.walk
components, objective_key = common.components, common.objective_key


def archive_modules(path):
    with ZipFile(path) as package:
        names = package.namelist()
        require(names and len(names) == len(set(names)) and all(
            n.startswith("uav_dispatch/") and n.endswith(".py")
            and len(Path(n).parts) == 2 for n in names), "Unsafe or duplicate ZIP member")
        return {Path(n).name: package.read(n) for n in names}


def restored_ast(content, name):
    tree = ast.parse(content)
    if name not in common.EXPECTED_COUNTS:
        return tree, None
    reversal = common.UndoWeightedChanges()
    tree = reversal.visit(tree)
    require(reversal.imports == 1 and reversal.counts == common.EXPECTED_COUNTS[name],
            "Weighted adapter edits or counts disagree: " + name)
    return tree, reversal.counts


def config_defaults(tree):
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ALNSConfig")
    return {n.target.id: ast.literal_eval(n.value) for n in cls.body if isinstance(n, ast.AnnAssign)}


def audit_protocol(manifest, parent):
    require(manifest["schema_version"] == 2 and manifest["order"] == ORDER
            and set(manifest["variants"]) == set(ORDER) and manifest["baseline_variant"] == ORDER[0],
            "Expected exactly the fresh weighted two-arm experiment")
    require(type(manifest["seed"]) is int and manifest["seed"] == SEED and manifest["seed"] != parent["seed"],
            "Fresh shared seed changed")
    require(manifest["budget_seconds"] == 240 and manifest["finalization_reserve_seconds"] == 1,
            "Frozen timing protocol changed")
    require(manifest["weights"] == parent["weights"] == [.5, .25, .25]
            and manifest["scales"] == parent["scales"]
            and len(manifest["scales"]) == 3 and all(finite(x, "scale") > 0 for x in manifest["scales"]),
            "Parent fixed normalization changed")
    for name in ("formula", "input_path", "input_sha256", "problem_path", "problem_sha256",
                 "initial_score", "initial_routes", "initial_routes_sha256"):
        require(manifest[name] == parent[name], "Frozen parent contract changed: " + name)
    expected_base = {**parent["config"], "seed": SEED}
    require(manifest["variants"][ORDER[0]]["config"] == expected_base,
            "Baseline differs from the adopted weighted configuration")
    require(manifest["variants"][ORDER[1]]["config"] == {**expected_base, **OVERRIDES},
            "Candidate changed configuration beyond the two declared overrides")
    require(all(manifest["variants"][n]["objective"] == "weighted" for n in ORDER),
            "Both experiment arms must use weighted J")


def source_audit(output, manifest, parent):
    frozen = {}
    for name in ORDER:
        spec = manifest["variants"][name]
        path = within(output, spec["archive"])
        require(sha(path) == spec["sha256"], "Frozen solver ZIP fingerprint changed")
        frozen[name] = archive_modules(path)
    parent_output = within(ROOT, manifest["parent_manifest_path"]).parent
    parent_zip = within(parent_output, parent["variants"]["current_weighted"]["archive"])
    parent_hash = sha(parent_zip)
    require(parent_hash == parent["variants"]["current_weighted"]["sha256"]
            == manifest["parent_weighted_source_sha256"] == manifest["variants"][ORDER[0]]["sha256"],
            "Baseline is not the exact previously run weighted ZIP")
    baseline, candidate = frozen[ORDER[0]], frozen[ORDER[1]]
    require(baseline == archive_modules(parent_zip), "Baseline archive content changed")
    require(set(baseline) <= set(candidate) and set(candidate) - set(baseline) <= {"direct_task_exchange.py"},
            "Unexpected solver module addition or deletion")
    require(set(manifest["production_files_sha256"]) == set(candidate), "Production module inventory changed")
    parent_commit = parent["source_commit"]
    require(isinstance(parent_commit, str) and len(parent_commit) == 40
            and all(c in "0123456789abcdef" for c in parent_commit), "Invalid parent source commit")
    changes, counts, baseline_tree = [], {}, None
    for name, content in baseline.items():
        tree, _ = restored_ast(content, name)
        original = subprocess.run(["git", "show", f"{parent_commit}:algorithm/src/uav_dispatch/{name}"],
                                  cwd=ROOT, check=True, capture_output=True).stdout
        if name in common.EXPECTED_COUNTS:
            require(ast.dump(tree) == ast.dump(ast.parse(original)), "Parent baseline source/adapter disagrees: " + name)
        else:
            require(content == original, "Parent baseline production source disagrees: " + name)
    for name, content in candidate.items():
        path = ROOT / "algorithm/src/uav_dispatch" / name
        production = path.read_bytes()
        require(sha(path) == manifest["production_files_sha256"][name], "Current production fingerprint changed: " + name)
        tree, edits = restored_ast(content, name)
        require(ast.dump(tree) == ast.dump(ast.parse(production)), "Candidate differs from declared production AST: " + name)
        if name not in common.EXPECTED_COUNTS:
            require(content == production, "Unadapted candidate bytes differ from production: " + name)
        if edits is not None:
            counts[name] = edits
        if name not in baseline or ast.dump(tree) != ast.dump(restored_ast(baseline[name], name)[0]):
            require(name in CHANGED_MODULES, "Candidate changed an undeclared solver module: " + name)
            changes.append(name)
        if name == "alns.py":
            baseline_tree = tree
    require(counts == manifest["adapter_changes"] == common.EXPECTED_COUNTS,
            "Manifest adapter transformations changed")
    require("direct_task_exchange.py" in changes and "alns.py" in changes and "incumbent_polish.py" in changes,
            "Declared task exchange implementation is missing")
    defaults = config_defaults(baseline_tree)
    require(defaults.get("enable_terminal_task_exchange") is False and defaults["enable_lazy_repair"] is False,
            "Research switches must remain disabled in public defaults")
    public_frozen = {**defaults, "max_iterations": 10_000_000, "seed": SEED, "relay_enabled": False,
                     "enable_home_seed": False, "enable_home_bias": False, **OVERRIDES}
    require(public_frozen == manifest["variants"][ORDER[1]]["config"],
            "Candidate differs from public defaults beyond declared overrides")
    required_scripts = {"run_weighted_method_pair.py", "run_weighted_direct_pair.py", "weighted_direct_objective.py"}
    require(set(manifest["execution_sources"]) == required_scripts, "Frozen execution script inventory changed")
    for name, spec in manifest["execution_sources"].items():
        require(sha(within(output, spec["path"])) == spec["sha256"]
                == sha(ROOT / "algorithm/experiments" / name), "Frozen execution source changed: " + name)
    require(manifest["execution_sources"]["weighted_direct_objective.py"]["sha256"] == parent["adapter_sha256"],
            "Weighted adapter differs from the adopted baseline")
    return dict(exact_parent_weighted_zip=True, parent_production_commit=parent_commit,
                adapter_reversal_matches_current_production=True, changed_modules=sorted(changes),
                declared_configuration_only=True, research_defaults_disabled=True, execution_sources_frozen=True)


def audit_run(output, row, manifest, tasks, initial_physical):
    name = row["variant"]
    spec = manifest["variants"][name]
    require(row["objective"] == spec["objective"] == "weighted", "Both arms must use weighted J")
    require(type(row["seed"]) is int and row["seed"] == manifest["seed"] == SEED, "Run seed changed")
    require(row["budget_seconds"] == 240 and row["valid"] is True and row["budget_compliant"] is True,
            "Run not valid within declared budget")
    elapsed = finite(row["end_to_end_seconds"], "end-to-end time")
    construction = finite(row["construction_seconds"], "construction time")
    search = finite(row["search_seconds"], "search time")
    require(0 <= construction and 0 <= search and construction + search <= elapsed <= 240,
            "End-to-end timing is inconsistent or over budget")
    expected_config = {**spec["config"], "time_limit_seconds": max(.001, 240 - construction - 1)}
    require(row["config"] == expected_config, "Run changed its frozen configuration")
    require(row["initial_routes_sha256"] == manifest["initial_routes_sha256"], "Run initial routes changed")
    require(row["source_sha256"] == spec["sha256"], "Run source hash changed")
    path = within(output, row["solution_path"])
    require(sha(path) == row["solution_sha256"], "Final solution fingerprint changed")
    payload = read(path)
    require(payload["valid"] is True and payload["violations"] == [] and payload["source"] == manifest["input_path"],
            "Final solution validity or source changed")
    problem = payload["problem"]
    expected_problem = dict(task_count=200, drone_count=8, max_tasks_per_drone=25, capacity=2,
                            speed_km_per_min=.9, depot_km=[0., 0.], drone_homes=["origin"] * 8,
                            open_routes=True, service_time_min=0.)
    require(all(problem.get(k) == v for k, v in expected_problem.items()), "Official problem constraints changed")
    require(problem["objective_order"] == ["weighted_total_score", *FIELDS], "Solution objective order changed")
    require(problem["experiment_objective"] == dict(kind="weighted", formula=manifest["formula"],
            weights=manifest["weights"], scales=manifest["scales"]), "Solution normalization changed")
    routes = payload["routes"]
    require([r["drone_id"] for r in routes] == list(range(1, 9)), "Drone route identity changed")
    physical, clocks, records = walk(tasks, [r["encoded_visits"] for r in routes])
    for actual, expected in zip(score(payload["score"]), physical, strict=True):
        same(actual, expected, "saved physical score")
    for field, expected in zip(FIELDS, physical, strict=True):
        same(row[field], expected, "row " + field)
    require(set(payload["delivery_times_min"]) == {str(i) for i in tasks}, "Delivery clock coverage changed")
    for task_id, clock in clocks.items():
        same(payload["delivery_times_min"][str(task_id)], clock, "delivery time")
    for route, record in zip(routes, records, strict=True):
        for field in ("distance_km", "completion_time_min", "task_count", "late_count", "total_lateness_min"):
            same(route[field], record[field], "route " + field)
        require(route["transport_leg_count"] == 25 and route["relay_drop_count"] == route["relay_pickup_count"] == 0,
                "Route transport metadata changed")
        require(len(route["visits"]) == len(route["encoded_visits"]), "Missing visit metadata")
        for visit, encoded, arrival in zip(route["visits"], route["encoded_visits"], record["arrival_times_min"], strict=True):
            task_id, pickup = abs(encoded), encoded > 0
            require(visit["task_id"] == task_id and visit["leg_id"] is None and visit["relay_id"] is None
                    and visit["action"] == ("pickup" if pickup else "delivery")
                    and visit["kind"] == ("PICKUP" if pickup else "DELIVERY"), "Visit semantics changed")
            require(visit["location_km"] == list(tasks[task_id][0 if pickup else 1]), "Visit coordinates changed")
            same(visit["arrival_time_min"], arrival, "visit arrival time")
    require(set(payload["task_plans"]) == {str(i) for i in tasks}
            and all(p == dict(mode="DIRECT", relay_id=None, leg_ids=[]) for p in payload["task_plans"].values()),
            "Non-DIRECT service plan found")
    contributions = components(physical, manifest)
    same(row["weighted_score"], sum(contributions), "independent weighted score")
    require(len(row["weighted_components"]) == 3, "Missing weighted components")
    for actual, expected in zip(row["weighted_components"], contributions, strict=True):
        same(actual, expected, "weighted contribution")
    same(row["on_time_rate"], 1 - physical[0] / 200, "on-time rate")
    same(payload["on_time_rate"], 1 - physical[0] / 200, "saved on-time rate")
    require(row["on_time_count"] == 200 - physical[0] and row["delivered_count"] == 200,
            "Task completion counts changed")
    require(type(row["iterations"]) is int and 0 <= row["iterations"] <= 10_000_000
            and payload["iterations"] == row["iterations"], "Iteration telemetry disagrees")
    metadata = payload["metadata"]
    require(metadata["seed"] == row["seed"] and metadata["relay_enabled"] is False
            and metadata["enable_home_seed"] is False, "Execution metadata changed")
    for key, expected in expected_config.items():
        if key in metadata:
            require(metadata[key] == expected, "Configuration metadata changed: " + key)
    same(metadata["time_limit_seconds"], expected_config["time_limit_seconds"], "solver search budget")
    terminal = metadata["terminal_polish"]
    require(terminal["active"] is True and metadata["enable_terminal_polish"] is True
            and metadata["enable_adjacent_polish"] is True, "Adopted refinement defaults changed")
    require(0 <= finite(terminal["reserved_seconds"], "terminal reserve") <= .1,
            "Terminal task exchange added a search budget")
    require(finite(terminal["elapsed_seconds"], "terminal time") >= 0, "Negative terminal time")
    terminal_before = score(dict(zip(FIELDS, terminal["before_score"], strict=True)))
    terminal_after = score(dict(zip(FIELDS, terminal["after_score"], strict=True)))
    for actual, expected in zip(terminal_after, physical, strict=True):
        same(actual, expected, "terminal final score")
    before_key = objective_key(terminal_before, manifest, "weighted")
    after_key = objective_key(terminal_after, manifest, "weighted")
    require(after_key <= before_key and terminal["accepted"] is (after_key < before_key),
            "Terminal refinement violated the fixed weighted objective or acceptance telemetry")
    require(row["terminal_polish"] == terminal and row["operator_statistics"] == metadata["operator_statistics"],
            "Mechanism telemetry differs from saved solution")
    require(row["performance_counters"] == metadata["profiling"]
            and all(type(value) is int and value >= 0 for value in row["performance_counters"].values()),
            "Performance counters differ from saved profiling or contain invalid counts")
    if name == ORDER[1]:
        require(metadata["enable_lazy_repair"] is True and metadata["enable_terminal_task_exchange"] is True,
                "Candidate research switches were not used")
        require(terminal["task_exchange_active"] is True, "Candidate terminal neighborhood was not active")
        terminal_stats = terminal["statistics"]
        for counter, maximum in (("task_exchange_calls", 1), ("task_exchange_candidates", 256),
                                 ("task_exchange_shortlisted_candidates", 256),
                                 ("task_exchange_shortlist_probes", 64 * 56)):
            value = terminal_stats.get(counter, 0)
            require(type(value) is int and 0 <= value <= maximum,
                    "Terminal neighborhood exceeded its declared work bound: " + counter)
    else:
        require(metadata["enable_lazy_repair"] is False
                and metadata.get("enable_terminal_task_exchange", False) is False, "Baseline research switches changed")
    trajectory = common.audit_trajectory(row, payload, manifest, initial_physical)
    return dict(variant=name, passed=True, independent_score=dict(zip(FIELDS, physical, strict=True)),
                independent_weighted_score=sum(contributions), delivered_count=200,
                pickups_and_deliveries_once=True, capacity_precedence_and_K25=True,
                origin_open_direct_only=True, every_event_clock_and_coordinate=True,
                end_to_end_budget_compliant=True, hashes_and_configuration_match=True, trajectory=trajectory)


def audit_results(output):
    output = output.resolve()
    manifest = read(output / "manifest.json")
    parent_path = within(ROOT, manifest["parent_manifest_path"])
    require(sha(parent_path) == manifest["parent_manifest_sha256"], "Parent manifest fingerprint changed")
    parent = read(parent_path)
    audit_protocol(manifest, parent)
    sources = source_audit(output, manifest, parent)
    input_path = within(ROOT, manifest["input_path"])
    require(sha(input_path) == manifest["input_sha256"], "Official input fingerprint changed")
    require(sha(within(ROOT, manifest["problem_path"])) == manifest["problem_sha256"], "Problem fingerprint changed")
    tasks = common.load_official_csv(input_path)
    require(digest(manifest["initial_routes"]) == manifest["initial_routes_sha256"], "Initial routes fingerprint changed")
    initial_physical, _, _ = walk(tasks, manifest["initial_routes"])
    for actual, expected in zip(score(manifest["initial_score"]), initial_physical, strict=True):
        same(actual, expected, "independent initial score")
    rows = read(output / "runs.json")
    require(isinstance(rows, list) and [r["variant"] for r in rows] == ORDER,
            "Exactly two fresh completed runs are required")
    runs = [audit_run(output, row, manifest, tasks, initial_physical) for row in rows]
    baseline, candidate = rows
    comparison = dict(candidate_minus_baseline={f: candidate[f] - baseline[f]
                      for f in (*FIELDS, "weighted_score", "on_time_rate", "end_to_end_seconds")},
                      weighted_objective_improves=objective_key(tuple(candidate[f] for f in FIELDS), manifest, "weighted")
                      < objective_key(tuple(baseline[f] for f in FIELDS), manifest, "weighted"),
                      original_lex_objective_improves=tuple(candidate[f] for f in FIELDS) < tuple(baseline[f] for f in FIELDS))
    report = output / "index.html"
    return dict(schema_version=2, passed=True, run_count=2, manifest_sha256=sha(output / "manifest.json"),
                runs_sha256=sha(output / "runs.json"), audited_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                evaluator="Independent CSV + math.hypot DIRECT walk; no production imports",
                source_checks=sources, input_sha256=manifest["input_sha256"],
                initial_routes_sha256=manifest["initial_routes_sha256"],
                independent_initial_score=dict(zip(FIELDS, initial_physical, strict=True)), runs=runs,
                comparison=comparison, report_sha256=sha(report) if report.exists() else None,
                limitations=["One fresh shared seed on one complete official instance; no statistical or cross-instance claim.",
                             "Intermediate trajectory routes are unavailable; only initial/final complete routes are independently recomputed.",
                             "Elapsed time is checked against stored telemetry, not reconstructed from route geometry.",
                             "Candidate combines task exchange and lazy repair; combined gains do not identify individual causal contributions."])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    output = parser.parse_args(argv).output_dir.resolve()
    try:
        result = audit_results(output)
    except (ValueError, KeyError, TypeError, OSError, StopIteration, subprocess.CalledProcessError) as error:
        result = dict(schema_version=2, passed=False, error=f"{type(error).__name__}: {error}")
    (output / "audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({k: result[k] for k in ("passed", "run_count", "error") if k in result}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
