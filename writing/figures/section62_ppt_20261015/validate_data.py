#!/usr/bin/env python3
"""Check saved evidence and chart data. Does not run a solver or modify sources."""
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
COMMIT = "303e711ec9287c11e8f544bae850a95cab0dc8d4"
CSV = "algorithm/results/final_sensitivity/runs.csv"
EXPECTED_SHA = "77f7682911d71495b966f427479a1bf112ba3a81226e4076ac1a5906c0d1e3cc"
METRICS = ["late_count", "total_lateness_min", "distance_km"]


def git_blob(path):
    return subprocess.check_output(["git", "show", f"{COMMIT}:{path}"], cwd=REPO)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    raw = (HERE / "data/historical_runs.csv").read_bytes()
    assert digest(raw) == EXPECTED_SHA
    assert raw == git_blob(CSV), "Pinned CSV changed"
    manifest_raw = (HERE / "data/historical_manifest.json").read_bytes()
    assert manifest_raw == git_blob("algorithm/results/final_sensitivity/manifest.json")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    data = json.loads((HERE / "data/plot_data.json").read_text())
    assert len(rows) == len(data["records"]) == 15
    assert data["source_commit"] == COMMIT
    assert {r["seed"] for r in rows} == {"2026081701"}
    checked = []
    for row, plot in zip(rows, data["records"]):
        assert row["parameter"] == plot["parameter"]
        for key, value in plot.items():
            if key not in {"parameter", "solution_sha256", "source_path"}:
                assert float(row[key]) == value, (row["parameter"], row["level"], key)
        path = "algorithm/results/final_sensitivity/" + row["cell_output_dir"] + "/" + row["solution_file"]
        blob = git_blob(path)
        assert digest(blob) == row["solution_sha256"] == plot["solution_sha256"]
        assert path == plot["source_path"]
        solution = json.loads(blob)
        assert solution["valid"] and not solution["violations"]
        for key in METRICS:
            assert float(row[key]) == solution["score"][key]
        checked.append({"parameter": row["parameter"], "level": float(row["level"]),
                        "solution_sha256": digest(blob), "saved_score_matches": True})
    manuscript = "writing/manuscript/低空物流协同调度_示意数据修订版.docx"
    audit = json.loads((HERE / "data/source_audit.json").read_text())
    assert digest((REPO / manuscript).read_bytes()) == audit["manuscript_sha256"]
    assert [int(r["late_count"]) for r in rows] == [60, 46, 33, 13, 0, 81, 71, 60, 49, 39, 59, 61, 60, 61, 58]
    report = {"passed": True, "source_commit": COMMIT, "csv_sha256": digest(raw),
              "manuscript_unchanged": True, "record_count": 15, "solutions": checked,
              "scope": "Checks immutable source bytes, saved scores, validity flags and plotting values; no fresh route feasibility evaluation, no experiment runs."}
    (HERE / "data/validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("PASS: 15 source rows, 15 solution hashes and saved scores, plotting values, unchanged manuscript.")


if __name__ == "__main__":
    main()
