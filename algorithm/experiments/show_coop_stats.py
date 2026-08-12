"""Print per-operator stats from Gate C results (helper, not a solver).

LEGACY — reads the old swap / dynamic-station stat keys; the formal
benchmark writes the new flat relay / ownership fields instead."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
OUT = Path(__file__).resolve().parents[1] / "results" / "cooperative_halns"
RUN = OUT / "run_solutions"

for f in sorted(RUN.glob("coop_seed*.json")):
    r = json.loads(f.read_text(encoding="utf-8"))
    print("===", f.stem, "===")
    print(
        "  late", r["late_count"], "dist", round(r["distance_km"], 1),
        "pickup_late", r["pickup_late_count"], "iters", r["iterations"],
    )
    print(
        "  svc", r["service_distribution"], "stations",
        r["active_station_count"], "adds", r["station_adds"],
        "drops", r["station_drops"],
    )
    for k in ("ownership", "relay", "blocker_relay", "swap",
              "dynamic_station"):
        s = r["op_stats"][k]
        print(
            f"  {k:16s} calls={s['calls']:4d} cand={s['candidates']:5d} "
            f"feas={s['feasible']:5d} acc={s['accepted']:3d} "
            f"impr={s['improving']:3d} bestimp={s['best_improving']:3d}"
        )
