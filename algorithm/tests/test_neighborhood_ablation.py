import json
from pathlib import Path

import uav_dispatch.alns as alns_module
from algorithm.experiments.run_neighborhood_ablation import (
    _checkpoint_signature,
    _parse_args,
    build_variants,
    config_for_variant,
    rotate_variants,
    run,
)


def test_ablation_variants_are_cumulative_and_core_stays_unreinforced():
    variants = build_variants()

    assert [variant.code for variant in variants] == [
        "A0",
        "A1",
        "A2",
        "A3",
        "A4",
    ]
    expected_enabled = [
        set(),
        {"enable_assignment_destroy"},
        {"enable_assignment_destroy", "enable_deadline_risk"},
        {
            "enable_assignment_destroy",
            "enable_deadline_risk",
            "enable_vnd",
        },
        {
            "enable_assignment_destroy",
            "enable_deadline_risk",
            "enable_vnd",
            "enable_cluster_repair",
        },
    ]
    for variant, expected in zip(variants, expected_enabled):
        enabled = {name for name, value in variant.flags().items() if value}
        assert enabled == expected
        assert variant.enable_ejection is False
        assert variant.enable_route_pool is False

    hybrid = build_variants(include_hybrid_tuning=True)
    assert [variant.code for variant in hybrid] == [
        "A0",
        "A1",
        "A2",
        "A3",
        "A4",
        "A5",
        "A6",
    ]
    assert hybrid[5].enable_ejection is True
    assert hybrid[5].enable_route_pool is False
    assert hybrid[6].enable_ejection is True
    assert hybrid[6].enable_route_pool is True


def test_quick_plan_and_seed_rotation_build_explicit_configs():
    args = _parse_args(["--quick"])
    variants = build_variants()

    assert args.equal_seed_count == 1
    assert args.equal_iterations == 5
    assert args.wall_seed_count == 1
    assert args.wall_time_limit == 10.0
    assert [variant.code for variant in rotate_variants(variants, 2)] == [
        "A2",
        "A3",
        "A4",
        "A0",
        "A1",
    ]

    config = config_for_variant(
        args,
        variants[0],
        seed=7,
        max_iterations=args.equal_iterations,
        time_limit_seconds=None,
    )
    assert config.seed == 7
    assert config.max_iterations == 5
    assert config.enable_assignment_destroy is False
    assert config.enable_deadline_risk is False
    assert config.enable_vnd is False
    assert config.enable_cluster_repair is False
    assert config.enable_ejection is False
    assert config.enable_route_pool is False


def test_checkpoint_signature_tracks_variant_flags_budgets_and_input_hash(
    tmp_path,
):
    args = _parse_args(["--input", str(tmp_path / "tasks.csv")])
    variants = build_variants()
    signature = _checkpoint_signature(
        args,
        input_sha256="source-a",
        variants=variants,
        initial_routes_sha256="initial-a",
    )

    assert signature["input_sha256"] == "source-a"
    assert signature["schema_version"] == 2
    assert set(signature["source_code"]) == {
        "experiment_script",
        "alns_solver",
    }
    assert len(signature["source_code"]["experiment_script"]["sha256"]) == 64
    assert len(signature["source_code"]["alns_solver"]["sha256"]) == 64
    assert signature["source_code"]["alns_solver"]["path"] == str(
        Path(alns_module.__file__).resolve()
    )
    assert signature["budgets"]["equal_iterations"] == 400
    assert signature["budgets"]["vnd_max_moves"] == 2
    assert signature["variants"][0]["flags"] == variants[0].flags()

    changed_budget = _parse_args(
        [
            "--input",
            str(tmp_path / "tasks.csv"),
            "--vnd-max-moves",
            "3",
        ]
    )
    changed_signature = _checkpoint_signature(
        changed_budget,
        input_sha256="source-a",
        variants=variants,
        initial_routes_sha256="initial-a",
    )
    assert changed_signature != signature

    hybrid_signature = _checkpoint_signature(
        args,
        input_sha256="source-a",
        variants=build_variants(include_hybrid_tuning=True),
        initial_routes_sha256="initial-a",
    )
    assert hybrid_signature != signature

    changed_source_signature = _checkpoint_signature(
        args,
        input_sha256="source-a",
        variants=variants,
        initial_routes_sha256="initial-a",
        source_code_hashes={
            "experiment_script": {
                "path": "run_neighborhood_ablation.py",
                "sha256": "experiment-b",
            },
            "alns_solver": {
                "path": "alns.py",
                "sha256": "solver-b",
            },
        },
    )
    assert changed_source_signature != signature


def test_results_manifest_carries_the_checkpoint_source_fingerprints(tmp_path):
    source = tmp_path / "tasks.csv"
    source.write_text(
        "task_id,pickup_x,pickup_y,delivery_x,delivery_y,deadline_min\n"
        "1,1,0,2,0,10\n",
        encoding="utf-8",
    )
    output = tmp_path / "results"
    args = _parse_args(
        [
            "--input",
            str(source),
            "--output-dir",
            str(output),
            "--equal-seed-count",
            "0",
            "--wall-seed-count",
            "0",
        ]
    )

    payload = run(args)
    source_code = payload["manifest"]["source_code"]

    assert source_code == payload["manifest"]["checkpoint_signature"][
        "source_code"
    ]
    persisted = json.loads((output / "ablation_results.json").read_text())
    assert persisted["manifest"]["source_code"] == source_code
