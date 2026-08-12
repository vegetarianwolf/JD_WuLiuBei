"""Project-wide integrity check (helper script)."""
import importlib
import pkgutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # project root (algorithm/experiments -> up 2)
sys.path.insert(0, str(ROOT / "algorithm" / "src"))

issues: list[str] = []

# 1. modules import
import uav_dispatch

mods = [m.name for m in pkgutil.iter_modules(uav_dispatch.__path__)]
for name in mods:
    if name == "__main__":
        continue  # python -m entry point; importing it would run the CLI
    try:
        importlib.import_module(f"uav_dispatch.{name}")
    except Exception as exc:  # noqa: BLE001
        issues.append(f"import uav_dispatch.{name}: {exc!r}")
print(f"modules: {len(mods)} imported, issues={len(issues)}")

# 2. key paths
checks = {
    "data/raw/命题1-低空经济场景下的物流无人机调度算法数据.csv": ROOT / "data" / "raw",
    "docs/COOPERATIVE_HALNS_REPORT.md": ROOT / "docs",
    "docs/SWAP_ONLY_COMPARISON_REPORT.md": ROOT / "docs",
    "docs/THREE_LAYER_ABLATION_REPORT.md": ROOT / "docs",
    "docs/RSLA_2S_ALNS_RELAY_VALIDATION_REPORT.docx": ROOT / "docs",
    "algorithm/experiments/run_cooperative_halns_benchmark.py": ROOT / "algorithm" / "experiments",
    "algorithm/experiments/run_phase1_verify.py": ROOT / "algorithm" / "experiments",
    "algorithm/experiments/run_relay_swap_validate.py": ROOT / "algorithm" / "experiments",
    "algorithm/tests/test_cooperative_halns.py": ROOT / "algorithm" / "tests",
    "algorithm/results/cooperative_halns": ROOT / "algorithm" / "results",
}
data_dir = ROOT / "data" / "raw"
csv = data_dir / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
xls = data_dir / "命题1-低空经济场景下的物流无人机调度算法数据.xls"
xlsx = data_dir / "命题1-低空经济场景下的物流无人机调度算法数据.xlsx"
print(f"data/raw csv: {csv.exists()} | xls: {xls.exists()} | xlsx: {xlsx.exists()}")
for name, d in checks.items():
    present = any(d.glob(Path(name).name)) if d.exists() else False
    if not present:
        issues.append(f"missing: {name}")
print(f"key paths OK, issues={len(issues)}")

# 3. stale references anywhere in code
for base in (ROOT / "algorithm" / "src", ROOT / "algorithm" / "experiments", ROOT / "algorithm" / "tests"):
    for py in base.rglob("*.py"):
        if py.name == "check_project_integrity.py":
            continue  # this file itself contains the search keywords
        text = py.read_text(encoding="utf-8", errors="ignore")
        for bad in ("run_b23_validate", "_PROJECT / \"命题1", "algorithm/命题1"):
            if bad in text:
                issues.append(f"stale ref in {py.relative_to(ROOT)}: {bad}")

# 4. README link targets exist
for md in (ROOT / "README.md", ROOT / "algorithm" / "README.md"):
    text = md.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "](" in line and line.strip().startswith("- "):
            start = line.find("](") + 2
            end = line.find(")", start)
            if end == -1:
                continue
            target = line[start:end].split("#")[0]
            if target.startswith(("http", "mailto")):
                continue
            resolved = (md.parent / target).resolve()
            if not resolved.exists():
                issues.append(f"broken link in {md.relative_to(ROOT)}: {target}")

print("\n=== ISSUES ===")
for i in issues:
    print(" -", i)
print(f"total issues: {len(issues)}")
