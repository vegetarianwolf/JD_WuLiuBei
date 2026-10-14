"""The new CLI method runs the whole joint solver without an ALNS phase."""
import json

from uav_dispatch import cli


def test_joint_cli_runs_without_calling_either_old_search(monkeypatch, tmp_path):
    source = tmp_path / "tasks.csv"
    source.write_text(
        "task_id,pickup_x,pickup_y,delivery_x,delivery_y,deadline_min\n"
        "1,-4,0,4,0,20\n2,4,1,-4,1,20\n"
        "3,-4,2,-3,2,20\n4,4,3,3,3,20\n"
    )
    target = tmp_path / "solution.json"
    def no_old_search(*args, **kwargs):
        raise AssertionError("Joint method called old search")
    monkeypatch.setattr(cli, "solve_alns", no_old_search)
    monkeypatch.setattr(cli, "solve_relay_staged", no_old_search)
    assert cli.main(["solve", "--input", str(source), "--output", str(target),
                     "--method", "joint-relay", "--tasks", "4", "--drones", "2",
                     "--max-tasks", "2", "--relay-count", "1", "--iterations", "0",
                     "--candidate-limit", "12", "--time-limit", "2"]) == 0
    saved = json.loads(target.read_text())
    assert saved["valid"]
    assert saved["metadata"]["method"] == "Joint-Relay-VNS"
    assert saved["metadata"]["direct_warmup"] is False
    assert "staged_relay" not in saved["metadata"]
    assert saved["metadata"]["construction_seconds"] >= 0
    assert saved["metadata"]["cli_total_time_limit_seconds"] == 2
    assert len(saved["delivery_times_min"]) == 4
