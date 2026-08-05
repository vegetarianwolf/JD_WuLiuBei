import json

import pytest

from uav_dispatch.cli import _parser, main


def test_cli_requires_an_explicit_input_path():
    with pytest.raises(SystemExit) as exc_info:
        _parser().parse_args(["solve"])

    assert exc_info.value.code == 2


def test_cli_solves_a_csv_and_writes_a_reproducible_json(tmp_path):
    source = tmp_path / "tasks.csv"
    source.write_text(
        "task_id,pickup_x,pickup_y,delivery_x,delivery_y,deadline_min\n"
        "1,1,0,2,0,10\n"
        "2,0,1,0,2,10\n"
        "3,1,1,2,2,10\n",
        encoding="utf-8",
    )
    output = tmp_path / "solution.json"

    exit_code = main(
        [
            "solve",
            "--input",
            str(source),
            "--output",
            str(output),
            "--method",
            "exact",
            "--tasks",
            "3",
            "--drones",
            "1",
            "--max-tasks",
            "3",
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["valid"] is True
    assert payload["problem"]["speed_km_per_min"] == 0.9
    assert payload["problem"]["depot_km"] == [0.0, 0.0]
    assert payload["score"]["late_count"] == 0
    assert len(payload["routes"]) == 1
    assert payload["routes"][0]["task_count"] == 3


def test_cli_exposes_an_explicit_alns_core_method(tmp_path):
    source = tmp_path / "tasks.csv"
    source.write_text(
        "task_id,pickup_x,pickup_y,delivery_x,delivery_y,deadline_min\n"
        "1,1,0,2,0,10\n"
        "2,0,1,0,2,10\n",
        encoding="utf-8",
    )
    output = tmp_path / "solution.json"

    exit_code = main(
        [
            "solve",
            "--input",
            str(source),
            "--output",
            str(output),
            "--method",
            "alns-core",
            "--iterations",
            "2",
            "--drones",
            "1",
            "--max-tasks",
            "2",
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["valid"] is True
    assert payload["metadata"]["method"] == "C2-Lex-ALNS-Core"
    assert payload["metadata"]["enable_route_pool"] is False
    assert payload["metadata"]["enable_ejection"] is False
