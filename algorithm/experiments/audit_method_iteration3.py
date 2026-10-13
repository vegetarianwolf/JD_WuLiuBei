"""Independent audit entry point for the adjacent-polish follow-up."""
import argparse

from algorithm.experiments import run_method_iteration3 as protocol


def audit_results(output_dir):
    return protocol.shared.audit_results(output_dir, plan=protocol.PLAN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=protocol.Path, default=protocol.OUTPUT)
    result = audit_results(parser.parse_args(argv).output_dir)
    print(f"verified {result['run_count']} polish runs; passed={result['passed']}; selected={result['decision_audit']['selected_variant']}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
