"""Bounded relay telemetry extension of the existing weighted adapter.

The one additional edit only merges staged best-score checkpoints by the
selected objective. Candidate generation, acceptance and final-route selection
remain the existing weighted DIRECT adapter's responsibility. This module and
its inverse use only the standard library; importing them loads no solver.
"""
from __future__ import annotations

import ast

try:
    from . import weighted_direct_objective as weighted
except ImportError:
    import weighted_direct_objective as weighted


TRAJECTORY_FIELD = "staged_trajectory_keys"
ORIGINAL_COMPARISON = "adjusted[2:] < merged_trajectory[-1][2:]"
ADAPTED_COMPARISON = (
    "_weighted_adapter.key(*adjusted[2:]) "
    "< _weighted_adapter.key(*merged_trajectory[-1][2:])"
)
EXPECTED_COUNTS = {
    name: {**counts, TRAJECTORY_FIELD: int(name == "alns.py")}
    for name, counts in weighted.EXPECTED_COUNTS.items()
}


class _StagedTrajectory(ast.NodeTransformer):
    def __init__(self, *, reverse=False):
        self.count = 0
        self.in_staged = False
        before, after = (
            (ADAPTED_COMPARISON, ORIGINAL_COMPARISON)
            if reverse else (ORIGINAL_COMPARISON, ADAPTED_COMPARISON)
        )
        self.before = ast.dump(ast.parse(before, mode="eval").body)
        self.after = after

    def visit_FunctionDef(self, node):
        previous = self.in_staged
        self.in_staged = node.name == "solve_relay_staged"
        try:
            return self.generic_visit(node)
        finally:
            self.in_staged = previous

    def visit_Compare(self, node):
        if self.in_staged and ast.dump(node) == self.before:
            self.count += 1
            return ast.copy_location(ast.parse(self.after, mode="eval").body, node)
        return self.generic_visit(node)


def _expected(module_name):
    if module_name not in EXPECTED_COUNTS:
        raise ValueError(f"Unsupported weighted relay module: {module_name}")
    return EXPECTED_COUNTS[module_name]


def transform(source, module_name):
    """Return adapted source and exact counts; reject source-shape drift."""
    expected = _expected(module_name)
    adapted, old_counts = weighted.transform(source)
    transformer = _StagedTrajectory()
    tree = transformer.visit(ast.parse(adapted))
    counts = {**old_counts, TRAJECTORY_FIELD: transformer.count}
    if counts != expected:
        raise ValueError(f"Unexpected weighted relay transform counts: {counts!r}")
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n", counts


def restore(source, module_name):
    """Reverse the counted edits for independent comparison with production.

    A caller must compare the returned AST with its frozen production source:
    count checks alone cannot rule out unrelated mutations elsewhere in a file.
    The existing independent DIRECT inverse validates its own edit patterns.
    """
    expected = _expected(module_name)
    try:
        from .audit_weighted_direct_pair import UndoWeightedChanges
    except ImportError:
        from audit_weighted_direct_pair import UndoWeightedChanges
    staged = _StagedTrajectory(reverse=True)
    tree = staged.visit(ast.parse(source))
    original = UndoWeightedChanges()
    restored = original.visit(tree)
    counts = {**original.counts, TRAJECTORY_FIELD: staged.count}
    if original.imports != 1 or counts != expected:
        raise ValueError(f"Unexpected weighted relay reverse counts: {counts!r}")
    return ast.unparse(ast.fix_missing_locations(restored)) + "\n", counts


reverse = restore
