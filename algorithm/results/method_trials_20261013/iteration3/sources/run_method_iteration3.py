"""Fresh paired adjacent-polish trial, sharing first + strings batch budgets."""
import argparse
from functools import partial
from pathlib import Path

from algorithm.experiments import method_followup_protocol as shared

PLAN = shared.Plan("iteration3", "polish", "enable_adjacent_polish", "相邻访问局部优化",
                   (2026101701, 2026101702), (2026101711, 2026101712, 2026101713),
                   (("first", "..", "method_trials"), ("strings", "../iteration2", "method_trials_iteration2")),
                   ("enable_direct_option_cache", "enable_budget_cooling", "enable_multiroute_string_destroy"),
                   allow_screen_regressions=True)
ROOT, base = shared.ROOT, shared.base
OUTPUT = ROOT / "algorithm/results/method_trials_20261013/iteration3"
BASELINE_COMMIT = shared.BASELINE_COMMIT
VARIANTS, SEEDS, PROTOCOL = PLAN.variants, PLAN.seeds, PLAN.protocol
LABELS = {"baseline": "当前 main 基线", "polish": PLAN.label}
CASES, BUDGETS, CAPS = shared.CASES, shared.BUDGETS, shared.CAPS
TOTAL_SECONDS, RESERVED_SECONDS = shared.TOTAL_SECONDS, shared.RESERVED_SECONDS
EXECUTION_NAMES = PLAN.execution_names
prepare, verify, stage, run = (partial(getattr(shared, name), plan=PLAN) for name in ("prepare", "verify", "stage", "run"))
screen_pass, medium_pass = partial(shared.screen_pass, plan=PLAN), partial(shared.medium_pass, plan=PLAN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    prepare(args.output_dir.resolve()) if args.command == "prepare" else run(args.output_dir.resolve())


if __name__ == "__main__":
    main()
