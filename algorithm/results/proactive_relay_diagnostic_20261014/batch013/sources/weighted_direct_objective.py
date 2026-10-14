"""Process-local, experimental scalar objective for frozen DIRECT source.

Production modules never import this module. Transformations are deliberately
bounded and counted: a source change must be reviewed before an experiment.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
import math

active = False
coefficients = None


def scalar(late, lateness, distance):
    if coefficients is None:
        raise RuntimeError("Weighted objective has not been configured")
    return coefficients[0] * late + coefficients[1] * lateness + coefficients[2] * distance


def key(late, lateness, distance):
    physical = (late, lateness, distance)
    return (scalar(*physical), *physical) if active else physical


@contextmanager
def objective(score_type, scales, weights=(0.5, 0.25, 0.25)):
    """Restore ordinary Score ordering even when a validation fails."""
    global active, coefficients
    if active:
        raise RuntimeError("Nested weighted objectives are unsupported")
    if len(scales) != 3 or len(weights) != 3:
        raise ValueError("Three scales and weights are required")
    if any(not math.isfinite(x) or x <= 0 for x in (*scales, *weights)):
        raise ValueError("Scales and weights must be finite and positive")
    if not math.isclose(sum(weights), 1.0):
        raise ValueError("Weights must sum to one")
    original = {name: getattr(score_type, name) for name in ("__lt__", "__le__", "__gt__", "__ge__")}
    coefficients = tuple(w / s for w, s in zip(weights, scales))

    def comparison(op):
        def compare(a, b):
            if type(a) is not type(b):
                return NotImplemented
            ka = key(a.late_count, a.total_lateness_min, a.distance_km)
            kb = key(b.late_count, b.total_lateness_min, b.distance_km)
            return op(ka, kb)
        return compare

    import operator
    for name, op in (("__lt__", operator.lt), ("__le__", operator.le),
                     ("__gt__", operator.gt), ("__ge__", operator.ge)):
        setattr(score_type, name, comparison(op))
    active = True
    try:
        yield
    finally:
        for name, method in original.items():
            setattr(score_type, name, method)
        active = False
        coefficients = None


def accept(candidate, current, temperature, rng):
    delta = scalar(candidate.late_count - current.late_count,
                   candidate.total_lateness_min - current.total_lateness_min,
                   candidate.distance_km - current.distance_km)
    return delta <= 0 or rng.random() < math.exp(-delta / max(temperature, 1e-12))


class Adapt(ast.NodeTransformer):
    def __init__(self):
        self.counts = dict(attribute_keys=0, regret_keys=0, regret_output_keys=0,
                           regret_missing_keys=0, raw_insertion_keys=0, acceptance=0)

    def visit_Tuple(self, node):
        self.generic_visit(node)
        if not isinstance(node.ctx, ast.Load) or len(node.elts) < 3:
            return node
        expressions = [ast.unparse(e) for e in node.elts[:3]]
        if expressions == ["regret[0]", "regret[1]", "regret[2]"]:
            self.counts["regret_output_keys"] += 1
            return ast.copy_location(ast.Tuple(elts=[ast.Starred(value=ast.Name(id="regret", ctx=ast.Load()),
                                                                ctx=ast.Load()), *node.elts[3:]], ctx=ast.Load()), node)
        missing = len(node.elts) == 3 and all(e == "float('inf')" for e in expressions)
        attributes = any(expressions == [p + ".late_count", p + ".total_lateness_min", p + ".distance_km"]
                         for p in ("option.delta", "option.local_estimated_delta", "delta"))
        regret = expressions == ["float(alternative.late_count - best.late_count)",
                                 "alternative.total_lateness_min - best.total_lateness_min",
                                 "alternative.distance_km - best.distance_km"]
        raw = expressions == ["item[0]", "item[1]", "item[2]"] and len(node.elts) == 5 and [
            ast.unparse(e) for e in node.elts[3:]] == ["item[3]", "item[4]"]
        if attributes or regret or raw or missing:
            self.counts["attribute_keys" if attributes else "regret_keys" if regret else
                        "regret_missing_keys" if missing else "raw_insertion_keys"] += 1
            call = ast.Call(func=ast.Attribute(value=ast.Name(id="_weighted_adapter", ctx=ast.Load()),
                                               attr="key", ctx=ast.Load()), args=node.elts[:3], keywords=[])
            return ast.copy_location(ast.Tuple(elts=[ast.Starred(value=call, ctx=ast.Load()),
                                                      *node.elts[3:]], ctx=ast.Load()), node)
        return node

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        if node.name == "_accept_worse":
            node.body.insert(0, ast.parse("if _weighted_adapter.active:\n    return _weighted_adapter.accept(candidate, current, temperature, rng)").body[0])
            self.counts["acceptance"] += 1
        return node


EXPECTED_COUNTS = {
    "alns.py": dict(attribute_keys=6, regret_keys=2, regret_output_keys=2,
                    regret_missing_keys=2, raw_insertion_keys=0, acceptance=1),
    "search.py": dict(attribute_keys=2, regret_keys=0, regret_output_keys=0,
                      regret_missing_keys=0, raw_insertion_keys=2, acceptance=0),
}


def transform(source):
    tree = ast.parse(source)
    transformer = Adapt()
    tree = transformer.visit(tree)
    index = 0
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        index = 1
    while index < len(tree.body) and isinstance(tree.body[index], ast.ImportFrom) and tree.body[index].module == "__future__":
        index += 1
    tree.body.insert(index, ast.Import(names=[ast.alias(name="weighted_direct_objective", asname="_weighted_adapter")]))
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n", transformer.counts
