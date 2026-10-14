"""Audit the two frozen DIRECT runs using only standard-library route arithmetic.

No production solver, validator, data loader or weighted adapter is imported.
Stored intermediate incumbents have scores but no route snapshots: trajectory
checks establish telemetry consistency and objective monotonicity, while only
the complete initial and final routes can be independently recomputed.
"""
from __future__ import annotations

import argparse
import ast
import csv
from datetime import datetime
import hashlib
from io import StringIO
import json
import math
from pathlib import Path
import subprocess
from zipfile import ZipFile
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "algorithm/results/weighted_direct_single_seed_20261014"
ORDER = ["current_lex", "current_weighted"]
FIELDS = ("late_count", "total_lateness_min", "distance_km")
EXPECTED_COUNTS = {
    "alns.py": dict(attribute_keys=6, regret_keys=2, regret_output_keys=2,
                    regret_missing_keys=2, raw_insertion_keys=0, acceptance=1),
    "search.py": dict(attribute_keys=2, regret_keys=0, regret_output_keys=0,
                      regret_missing_keys=0, raw_insertion_keys=2, acceptance=0),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def within(directory, value):
    require(isinstance(value, str), "Missing artifact path")
    path = (directory / value).resolve()
    require(path.is_relative_to(directory.resolve()), "Artifact path escapes result directory")
    return path


def finite(value, label):
    require(type(value) in (int, float) and math.isfinite(value), label + " must be finite numeric data")
    return value


def same(actual, expected, label):
    finite(actual, label)
    finite(expected, label)
    require(math.isclose(actual, expected, rel_tol=1e-11, abs_tol=1e-8),
            f"{label}: stored {actual!r}, independently expected {expected!r}")


def score(value):
    require(isinstance(value, dict) and set(value) == set(FIELDS), "Physical score fields disagree")
    result = tuple(value[field] for field in FIELDS)
    require(type(result[0]) is int and 0 <= result[0] <= 200, "Invalid late count")
    require(all(finite(x, "score") >= 0 for x in result), "Negative physical score")
    return result


def components(physical, manifest):
    return [w * x / scale for w, x, scale in zip(manifest["weights"], physical, manifest["scales"], strict=True)]


def objective_key(physical, manifest, objective):
    if objective == "lexicographic":
        return tuple(physical)
    # Coefficient-first arithmetic matches the comparison rule, independently
    # implemented here; row score components use mathematically equal w*x/s.
    value = sum((w / scale) * x for w, scale, x in zip(manifest["weights"], manifest["scales"], physical, strict=True))
    return (value, *physical)


def load_official_csv(path):
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    require(text is not None, "CSV encoding could not be read")
    reader = csv.reader(StringIO(text))
    header = next(reader)
    require(header == ["任务编号", "取件点坐标X", "取件点坐标Y", "送达点坐标X", "送达点坐标Y", "最晚送达时间Tmax（min）"],
            "Official CSV schema changed")
    tasks = {}
    for row in reader:
        if not row or not any(row):
            continue
        require(len(row) == 6, "Malformed official task row")
        task_id = int(row[0])
        values = [float(x) for x in row[1:]]
        require(task_id not in tasks and all(math.isfinite(x) for x in values), "Duplicate or nonfinite task")
        require(values[4] >= 0, "Negative deadline")
        tasks[task_id] = (tuple(values[:2]), tuple(values[2:4]), values[4])
    require(set(tasks) == set(range(1, 201)), "Official complete 200-task instance is required")
    return tasks


def walk(tasks, routes):
    require(isinstance(routes, list) and len(routes) == 8, "Exactly eight DIRECT routes are required")
    pickups, deliveries, clocks, route_records = set(), set(), {}, []
    total_distance = 0.0
    for route in routes:
        require(isinstance(route, list), "Encoded route must be a list")
        onboard, route_deliveries, arrivals = set(), set(), []
        point, time, distance = (0.0, 0.0), 0.0, 0.0
        for visit in route:
            require(type(visit) is int and visit != 0 and abs(visit) in tasks,
                    "Unknown visit, relay leg or noninteger visit")
            task_id = abs(visit)
            target = tasks[task_id][0 if visit > 0 else 1]
            leg = math.hypot(target[0] - point[0], target[1] - point[1])
            total_distance += leg
            distance += leg
            time += leg / .9
            point = target
            if visit > 0:
                require(task_id not in pickups, "Duplicate pickup")
                pickups.add(task_id)
                onboard.add(task_id)
                require(len(onboard) <= 2, "Capacity exceeds two packages")
            else:
                require(task_id in onboard, "Delivery precedes pickup or crosses drones")
                require(task_id not in deliveries, "Duplicate delivery")
                onboard.remove(task_id)
                deliveries.add(task_id)
                route_deliveries.add(task_id)
                clocks[task_id] = time
            arrivals.append(time)
        require(not onboard, "Route ends with undelivered load")
        require(len(route_deliveries) <= 25, "Per-drone K25 exceeded")
        late = [max(0., clocks[task_id] - tasks[task_id][2]) for task_id in sorted(route_deliveries)]
        route_records.append(dict(distance_km=distance, completion_time_min=time,
                                  task_count=len(route_deliveries), late_count=sum(x > 1e-9 for x in late),
                                  total_lateness_min=sum(late), arrival_times_min=arrivals))
    require(pickups == deliveries == set(tasks), "All 200 tasks must be picked up and delivered exactly once")
    latenesses = [max(0., clocks[task_id] - tasks[task_id][2]) for task_id in sorted(tasks)]
    physical = (sum(x > 1e-9 for x in latenesses), sum(x for x in latenesses if x > 1e-9), total_distance)
    return physical, clocks, route_records


class UndoWeightedChanges(ast.NodeTransformer):
    """Independently reverse the bounded frozen edits, rejecting other changes."""
    def __init__(self):
        self.counts = dict(attribute_keys=0, regret_keys=0, regret_output_keys=0,
                           regret_missing_keys=0, raw_insertion_keys=0, acceptance=0)
        self.imports = 0

    def visit_Import(self, node):
        if len(node.names) == 1 and node.names[0].name == "weighted_direct_objective" and node.names[0].asname == "_weighted_adapter":
            self.imports += 1
            return None
        return node

    def visit_FunctionDef(self, node):
        if node.name == "_accept_worse":
            expected = ast.parse("if _weighted_adapter.active:\n    return _weighted_adapter.accept(candidate, current, temperature, rng)").body[0]
            require(node.body and ast.dump(node.body[0]) == ast.dump(expected), "Weighted acceptance edit changed")
            node.body = node.body[1:]
            self.counts["acceptance"] += 1
        return self.generic_visit(node)

    def visit_Tuple(self, node):
        node = self.generic_visit(node)
        if not node.elts or not isinstance(node.elts[0], ast.Starred):
            return node
        value = node.elts[0].value
        if isinstance(value, ast.Name) and value.id == "regret":
            self.counts["regret_output_keys"] += 1
            indices = ast.parse("(regret[0], regret[1], regret[2])", mode="eval").body.elts
            node.elts = [*indices, *node.elts[1:]]
            return node
        if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                and isinstance(value.func.value, ast.Name) and value.func.value.id == "_weighted_adapter"
                and value.func.attr == "key"):
            return node
        require(len(value.args) == 3 and not value.keywords, "Unexpected weighted key arguments")
        expressions = [ast.unparse(x) for x in value.args]
        if any(expressions == [p + ".late_count", p + ".total_lateness_min", p + ".distance_km"]
               for p in ("option.delta", "option.local_estimated_delta", "delta")):
            field = "attribute_keys"
        elif expressions == ["float(alternative.late_count - best.late_count)",
                             "alternative.total_lateness_min - best.total_lateness_min",
                             "alternative.distance_km - best.distance_km"]:
            field = "regret_keys"
        elif expressions == ["float('inf')"] * 3 and len(node.elts) == 1:
            field = "regret_missing_keys"
        elif expressions == ["item[0]", "item[1]", "item[2]"] and [ast.unparse(x) for x in node.elts[1:]] == ["item[3]", "item[4]"]:
            field = "raw_insertion_keys"
        else:
            raise ValueError("Candidate changed an unapproved key")
        self.counts[field] += 1
        node.elts = [*value.args, *node.elts[1:]]
        return node


def source_audit(output, manifest):
    snapshots = {}
    for name in ORDER:
        specification = manifest["variants"][name]
        path = within(output, specification["archive"])
        require(sha(path) == specification["sha256"], "Frozen solver ZIP fingerprint changed")
        with ZipFile(path) as package:
            names = package.namelist()
            require(len(names) == len(set(names)) and all(n.startswith("uav_dispatch/") and n.endswith(".py")
                    and len(Path(n).parts) == 2 for n in names), "Unsafe or duplicate ZIP member")
            snapshots[name] = {n: package.read(n) for n in names}
    original, candidate = snapshots[ORDER[0]], snapshots[ORDER[1]]
    require(set(original) == set(candidate), "Solver module sets disagree")
    commit = manifest["source_commit"]
    require(isinstance(commit, str) and len(commit) == 40 and all(x in "0123456789abcdef" for x in commit), "Invalid source commit")
    counts = {}
    for name, content in original.items():
        git_content = subprocess.run(["git", "show", f"{commit}:algorithm/src/{name}"], cwd=ROOT,
                                     check=True, capture_output=True).stdout
        require(content == git_content == (ROOT / "algorithm/src" / name).read_bytes(), "Original ZIP, adopted main and current production source disagree")
        short_name = Path(name).name
        if short_name not in EXPECTED_COUNTS:
            require(candidate[name] == content, "Candidate changed an unrelated solver module")
            continue
        transformer = UndoWeightedChanges()
        restored = transformer.visit(ast.parse(candidate[name]))
        require(transformer.imports == 1 and transformer.counts == EXPECTED_COUNTS[short_name], "Candidate adapter edits or counts disagree")
        require(ast.dump(restored) == ast.dump(ast.parse(content)), "Candidate has other solver AST changes")
        counts[short_name] = transformer.counts
    require(counts == manifest["adapter_changes"], "Manifest adapter counts disagree")
    tree = ast.parse(original["uav_dispatch/alns.py"])
    config_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ALNSConfig")
    defaults = {n.target.id: ast.literal_eval(n.value) for n in config_class.body if isinstance(n, ast.AnnAssign)}
    defaults.update(max_iterations=10_000_000, seed=2026101451, relay_enabled=False,
                    enable_home_seed=False, enable_home_bias=False)
    require(defaults == manifest["config"], "Manifest differs from current public defaults and declared protocol overrides")
    for field, filename in (("runner_sha256", "run_weighted_direct_pair.py"), ("adapter_sha256", "weighted_direct_objective.py")):
        require(sha(ROOT / "algorithm/experiments" / filename) == manifest[field], "Experiment execution source fingerprint changed")
    return dict(source_commit=commit, frozen_sources_match_adopted_main=True,
                current_production_sources_unchanged=True, public_defaults_match=True,
                only_bounded_candidate_ast_edits=True, reversed_adapter_counts=counts)


def audit_trajectory(row, payload, manifest, initial_physical):
    trajectory, metadata = row["best_trajectory"], payload["metadata"]
    require(isinstance(trajectory, list) and trajectory and trajectory == metadata["best_trajectory"], "Missing or inconsistent complete trajectory telemetry")
    require(metadata["best_update_count"] == len(trajectory) - 1, "Trajectory checkpoint count disagrees")
    previous, elapsed, iteration = None, -1., -1
    for point in trajectory:
        require(isinstance(point, list) and len(point) == 5, "Malformed trajectory point")
        i, t, *physical = point
        require(type(i) is int and iteration <= i <= row["iterations"], "Trajectory iteration order disagrees")
        require(elapsed <= finite(t, "trajectory time") <= row["search_seconds"] + 1e-7, "Trajectory clock order disagrees")
        score(dict(zip(FIELDS, physical, strict=True)))
        key = objective_key(physical, manifest, row["objective"])
        require(previous is None or key < previous, "Best trajectory is not strictly improving in its selected objective")
        previous, elapsed, iteration = key, t, i
    require(trajectory[0][0] == 0, "Trajectory must include the initial incumbent")
    for actual, expected in zip(trajectory[0][2:], initial_physical, strict=True):
        same(actual, expected, "initial trajectory score")
    for actual, expected in zip(trajectory[-1][2:], (row[x] for x in FIELDS), strict=True):
        same(actual, expected, "final trajectory score")
    for actual, expected in zip(metadata["initial_score"], initial_physical, strict=True):
        same(actual, expected, "metadata initial score")
    same(metadata["time_to_best_seconds"], trajectory[-1][1], "time to best")
    return dict(checkpoint_count=len(trajectory), complete_initial_and_final_scores=True,
                selected_objective_strictly_monotonic=True,
                weighted_score_nonincreasing=(row["objective"] == "weighted"),
                intermediate_routes_available=False)


def audit_run(output, row, manifest, tasks, initial_physical):
    name = row["variant"]
    objective = "lexicographic" if name == "current_lex" else "weighted"
    require(row["objective"] == objective == manifest["variants"][name]["objective"], "Objective arm changed")
    require(row["seed"] == manifest["seed"] == 2026101451 and type(row["seed"]) is int, "Seed changed")
    require(row["budget_seconds"] == 240 and row["valid"] is True and row["budget_compliant"] is True, "Run not valid within declared budget")
    elapsed = finite(row["end_to_end_seconds"], "end-to-end time")
    construction = finite(row["construction_seconds"], "construction time")
    search = finite(row["search_seconds"], "search time")
    require(0 <= construction and 0 <= search and construction + search <= elapsed <= 240, "End-to-end timing is inconsistent or over budget")
    expected_config = {**manifest["config"], "time_limit_seconds": max(.001, 240 - construction - 1)}
    require(row["config"] == expected_config, "Run changed frozen public-default configuration")
    require(row["initial_routes_sha256"] == manifest["initial_routes_sha256"], "Run did not share the declared initial routes")
    require(row["source_sha256"] == manifest["variants"][name]["sha256"], "Run source hash differs from frozen variant")
    path = within(output, row["solution_path"])
    require(sha(path) == row["solution_sha256"], "Final solution fingerprint changed")
    payload = read(path)
    require(payload["valid"] is True and payload["violations"] == [], "Final solution marked invalid")
    require(payload["source"] == manifest["input_path"], "Solution input source changed")
    problem = payload["problem"]
    expected_problem = dict(task_count=200, drone_count=8, max_tasks_per_drone=25, capacity=2,
                            speed_km_per_min=.9, depot_km=[0., 0.], drone_homes=["origin"] * 8,
                            open_routes=True, service_time_min=0.)
    require(all(problem.get(k) == v for k, v in expected_problem.items()), "Official problem constraints changed")
    require(problem["objective_order"] == (["weighted_total_score", *FIELDS] if objective == "weighted" else list(FIELDS)), "Solution objective order changed")
    require(problem["experiment_objective"] == dict(kind=objective, formula=manifest["formula"], weights=manifest["weights"], scales=manifest["scales"]), "Solution normalization changed")
    routes = payload["routes"]
    require([r["drone_id"] for r in routes] == list(range(1, 9)), "Drone route identity changed")
    encoded = [r["encoded_visits"] for r in routes]
    physical, clocks, records = walk(tasks, encoded)
    for actual, expected in zip(score(payload["score"]), physical, strict=True):
        same(actual, expected, "saved physical score")
    for field, expected in zip(FIELDS, physical, strict=True):
        same(row[field], expected, "row " + field)
    require(set(payload["delivery_times_min"]) == {str(i) for i in tasks}, "Delivery clock coverage changed")
    for task_id, time in clocks.items():
        same(payload["delivery_times_min"][str(task_id)], time, "delivery time")
    for route, record in zip(routes, records, strict=True):
        for field in ("distance_km", "completion_time_min", "task_count", "late_count", "total_lateness_min"):
            same(route[field], record[field], "route " + field)
        require(route["transport_leg_count"] == 25 and route["relay_drop_count"] == route["relay_pickup_count"] == 0, "Route relay or transport-leg metadata changed")
        require(len(route["visits"]) == len(route["encoded_visits"]), "Missing visit metadata")
        for visit, encoded_visit, arrival in zip(route["visits"], route["encoded_visits"], record["arrival_times_min"], strict=True):
            task_id, pickup = abs(encoded_visit), encoded_visit > 0
            require(visit["task_id"] == task_id and visit["leg_id"] is None and visit["relay_id"] is None
                    and visit["action"] == ("pickup" if pickup else "delivery")
                    and visit["kind"] == ("PICKUP" if pickup else "DELIVERY"), "Visit semantics changed")
            require(visit["location_km"] == list(tasks[task_id][0 if pickup else 1]), "Visit coordinates differ from official CSV")
            same(visit["arrival_time_min"], arrival, "visit arrival time")
    require(set(payload["task_plans"]) == {str(i) for i in tasks}
            and all(p == dict(mode="DIRECT", relay_id=None, leg_ids=[]) for p in payload["task_plans"].values()), "Relay service plan found")
    expected_components = components(physical, manifest)
    same(row["weighted_score"], sum(expected_components), "independent weighted score")
    require(len(row["weighted_components"]) == 3, "Missing weighted components")
    for actual, expected in zip(row["weighted_components"], expected_components, strict=True):
        same(actual, expected, "weighted component")
    same(row["on_time_rate"], 1 - physical[0] / 200, "on-time rate")
    same(payload["on_time_rate"], 1 - physical[0] / 200, "saved on-time rate")
    require(row["on_time_count"] == 200 - physical[0] and row["delivered_count"] == 200, "Task completion counts disagree")
    require(type(row["iterations"]) is int and 0 <= row["iterations"] <= 10_000_000 and payload["iterations"] == row["iterations"], "Iteration telemetry disagrees")
    metadata = payload["metadata"]
    require(metadata["seed"] == row["seed"] and metadata["relay_enabled"] is False
            and metadata["enable_home_seed"] is False, "Solution execution metadata changed")
    for key, expected in manifest["config"].items():
        if key in metadata:
            require(metadata[key] == expected or key == "time_limit_seconds", "Solution configuration metadata changed: " + key)
    same(metadata["time_limit_seconds"], expected_config["time_limit_seconds"], "solver search budget")
    require(metadata["terminal_polish"]["active"] is True and metadata["enable_terminal_polish"] is True
            and metadata["enable_adjacent_polish"] is True, "Current adopted polish defaults were not used")
    trajectory = audit_trajectory(row, payload, manifest, initial_physical)
    return dict(variant=name, passed=True, independent_score=dict(zip(FIELDS, physical, strict=True)),
                independent_weighted_score=sum(expected_components), delivered_count=200,
                pickups_and_deliveries_once=True, capacity_precedence_and_K25=True,
                origin_open_direct_only=True, every_event_clock_and_coordinate=True,
                end_to_end_budget_compliant=True, hashes_and_default_config_match=True,
                trajectory=trajectory)


def audit_results(output):
    output = output.resolve()
    manifest = read(output / "manifest.json")
    require(manifest["schema_version"] == 1 and manifest["order"] == ORDER
            and set(manifest["variants"]) == set(ORDER), "Expected exactly the current-main two-arm experiment")
    require(manifest["budget_seconds"] == 240 and manifest["finalization_reserve_seconds"] == 1, "Frozen timing protocol changed")
    require(manifest["weights"] == [.5, .25, .25] and len(manifest["scales"]) == 3
            and all(finite(x, "scale") > 0 for x in manifest["scales"]), "Frozen normalization changed")
    source_checks = source_audit(output, manifest)
    input_path = within(ROOT, manifest["input_path"])
    require(sha(input_path) == manifest["input_sha256"], "Official input fingerprint changed")
    require(sha(within(ROOT, manifest["problem_path"])) == manifest["problem_sha256"], "Problem statement fingerprint changed")
    tasks = load_official_csv(input_path)
    initial = manifest["initial_routes"]
    require(digest(initial) == manifest["initial_routes_sha256"], "Shared initial route fingerprint changed")
    initial_physical, _, _ = walk(tasks, initial)
    for actual, expected in zip(score(manifest["initial_score"]), initial_physical, strict=True):
        same(actual, expected, "independent initial score")
    for actual, expected in zip(manifest["scales"], (200, *initial_physical[1:]), strict=True):
        same(actual, expected, "initial normalization scale")
    rows = read(output / "runs.json")
    require(isinstance(rows, list) and [r["variant"] for r in rows] == ORDER, "Exactly two fresh completed paired runs are required")
    runs = [audit_run(output, row, manifest, tasks, initial_physical) for row in rows]
    baseline, candidate = rows
    return dict(schema_version=1, passed=True, run_count=2,
                manifest_sha256=sha(output / "manifest.json"), runs_sha256=sha(output / "runs.json"),
                audited_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                evaluator="Independent CSV + math.hypot DIRECT walk; no production imports",
                source_checks=source_checks, input_sha256=manifest["input_sha256"],
                initial_routes_sha256=manifest["initial_routes_sha256"],
                independent_initial_score=dict(zip(FIELDS, initial_physical, strict=True)), runs=runs,
                comparison=dict(weighted_minus_lex={field: candidate[field] - baseline[field]
                                                   for field in (*FIELDS, "weighted_score", "on_time_rate", "end_to_end_seconds")},
                                weighted_objective_improves=candidate["weighted_score"] < baseline["weighted_score"],
                                original_lex_objective_improves=tuple(candidate[x] for x in FIELDS) < tuple(baseline[x] for x in FIELDS)),
                limitations=["One complete official instance and one shared seed; no statistical or cross-instance claim.",
                             "Intermediate trajectory routes are not stored; scores are checked for complete telemetry and selected-objective monotonicity, not independently rerouted.",
                             "Elapsed time is checked against stored timing telemetry; it cannot be reconstructed from route geometry."])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    output = parser.parse_args(argv).output_dir.resolve()
    try:
        result = audit_results(output)
    except (ValueError, KeyError, TypeError, OSError, StopIteration, subprocess.CalledProcessError) as error:
        result = dict(schema_version=1, passed=False, error=f"{type(error).__name__}: {error}")
    (output / "audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({k: result[k] for k in ("passed", "run_count", "error") if k in result}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
