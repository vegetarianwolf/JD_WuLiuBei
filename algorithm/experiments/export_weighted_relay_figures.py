"""Export audited paired effects with Matplotlib for manuscript use.

No optimizer, production imports, or inferential confidence intervals.
Individual paired seeds remain visible in each of six effect panels.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "algorithm/results/weighted_relay_ablation_20261014"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(directory):
    directory = directory.resolve()
    audit = json.loads((directory / "audit.json").read_text())
    rows = json.loads((directory / "runs.json").read_text())
    manifest = json.loads((directory / "manifest.json").read_text())
    assert audit["passed"] and audit["run_count"] == len(rows) == 12
    assert audit["manifest_sha256"] == sha(directory / "manifest.json")
    assert audit["runs_sha256"] == sha(directory / "runs.json")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "svg.fonttype": "none", "axes.spines.top": False,
                         "axes.spines.right": False, "axes.spines.left": False,
                         "axes.grid": False, "figure.facecolor": "white"})
    seeds = manifest["seeds"]
    index = {(r["scenario"], r["seed"]): r for r in rows}
    pairs = [("Origin", "pure_direct", "direct_relay"),
             ("Predeployment", "direct_stations", "direct_relay_stations")]
    metrics = [("on_time_count", "On-time tasks", "tasks", 1),
               ("distance_km", "Distance", "km", -1),
               ("weighted_score", "Weighted J", "unitless", -1)]
    effects = []
    for group, direct, relay in pairs:
        for seed in seeds:
            a, b = index[direct, seed], index[relay, seed]
            effects.append(dict(home_group=group, seed=seed, **{
                key: b[key] - a[key] for key in ("on_time_count", "distance_km", "weighted_score", "total_lateness_min")}))
    figures = directory / "figures"
    figures.mkdir(exist_ok=True)
    with (figures / "paired_effects.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(effects[0]))
        writer.writeheader()
        writer.writerows(effects)
    fig, axes = plt.subplots(2, 3, figsize=(10.4, 5.6), layout="constrained")
    for column, (key, label, unit, direction) in enumerate(metrics):
        values = [effect[key] for effect in effects]
        lo, hi = min(0., *values), max(0., *values)
        span = max(hi-lo, 1e-6 if key == "weighted_score" else 1.)
        limits = (lo-.18*span, hi+.22*span)
        for row_number, (group, _, _) in enumerate(pairs):
            ax = axes[row_number, column]
            selected = [effect for effect in effects if effect["home_group"] == group]
            ax.axvline(0, color="#6f7773", linewidth=.8, zorder=1)
            for y, effect in enumerate(selected):
                delta = effect[key]
                color = "#168064" if delta*direction > 0 else "#b34c3d" if delta*direction < 0 else "#5d6963"
                ax.plot([0, delta], [y, y], color=color, linewidth=1.4)
                ax.scatter(delta, y, color=color, s=33, zorder=3)
                digits = 6 if key == "weighted_score" else 0 if key == "on_time_count" else 2
                ax.annotate(f"{delta:+.{digits}f}", (delta, y), xytext=(5 if delta >= 0 else -5, 8),
                            textcoords="offset points", ha="left" if delta >= 0 else "right", fontsize=8)
            ax.set_xlim(*limits)
            ax.set_ylim(-.65, 2.65)
            ax.invert_yaxis()
            ax.set_yticks(range(3), [str(s) for s in seeds])
            ax.tick_params(axis="y", length=0)
            ax.set_title(f"{group} / {label}", loc="left", fontsize=10)
            ax.set_xlabel(f"Relay minus DIRECT ({unit})")
            ax.ticklabel_format(axis="x", style="plain", useOffset=False)
    fig.suptitle("Equal 240 s budgets: relay effects within fixed home configurations", fontsize=12)
    fig.savefig(figures / "relay_paired_effects.svg", bbox_inches="tight")
    fig.savefig(figures / "relay_paired_effects.png", dpi=240, bbox_inches="tight")
    plt.close(fig)
    record = dict(passed=True, source_manifest_sha256=sha(directory / "manifest.json"),
                  source_runs_sha256=sha(directory / "runs.json"), source_audit_sha256=sha(directory / "audit.json"),
                  exporter_sha256=sha(Path(__file__)), unique_pairs=6,
                  description="Individual paired effects; green=improvement, red=worsening. Matching x scales within each metric. No confidence intervals or significance claim.",
                  outputs={path.name: sha(path) for path in sorted(figures.iterdir()) if path.suffix in {".csv", ".svg", ".png"}})
    (figures / "validation.json").write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({"figure": str(figures / "relay_paired_effects.png"), "paired_records": 6}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    export(parser.parse_args().output_dir)
