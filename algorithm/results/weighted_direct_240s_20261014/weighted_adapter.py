"""Experiment-only scalar objective. Never imported by production entry points."""
import ast
import math
active = False
coefficients = None

def scalar(late, lateness, distance):
    return coefficients[0]*late + coefficients[1]*lateness + coefficients[2]*distance

def key(late, lateness, distance):
    if not active:
        return late, lateness, distance
    return scalar(late, lateness, distance), late, lateness, distance

def enable(score_type, scales):
    global active, coefficients
    coefficients = (.5/scales[0], .25/scales[1], .25/scales[2])
    def k(s): return key(s.late_count, s.total_lateness_min, s.distance_km)
    score_type.__lt__ = lambda a,b: k(a)<k(b)
    score_type.__le__ = lambda a,b: k(a)<=k(b)
    score_type.__gt__ = lambda a,b: k(a)>k(b)
    score_type.__ge__ = lambda a,b: k(a)>=k(b)
    active = True

def accept(candidate, current, temperature, rng):
    delta = scalar(candidate.late_count-current.late_count,
                   candidate.total_lateness_min-current.total_lateness_min,
                   candidate.distance_km-current.distance_km)
    return delta <= 0 or rng.random() < math.exp(-delta/max(temperature, 1e-12))

class Adapt(ast.NodeTransformer):
    def __init__(self): self.counts = {'attribute_keys':0,'regret_keys':0,'raw_insertion_keys':0,'acceptance':0}
    def visit_Tuple(self, node):
        self.generic_visit(node)
        if not isinstance(node.ctx, ast.Load) or len(node.elts)<3: return node
        x = [ast.unparse(e) for e in node.elts[:3]]
        allowed = ['option.delta','option.local_estimated_delta','delta']
        attr = any(x == [p+'.late_count',p+'.total_lateness_min',p+'.distance_km'] for p in allowed)
        regret = x == ['float(alternative.late_count - best.late_count)', 'alternative.total_lateness_min - best.total_lateness_min','alternative.distance_km - best.distance_km']
        # Raw insertion records stay untouched; only their sort key is adapted.
        raw = x == ['item[0]','item[1]','item[2]'] and len(node.elts)==5 and [ast.unparse(e) for e in node.elts[3:]]==['item[3]','item[4]']
        if attr or regret or raw:
            self.counts['attribute_keys' if attr else 'regret_keys' if regret else 'raw_insertion_keys'] += 1
            call = ast.Call(func=ast.Attribute(value=ast.Name(id='_weighted_adapter',ctx=ast.Load()),attr='key',ctx=ast.Load()),args=node.elts[:3],keywords=[])
            return ast.copy_location(ast.Tuple(elts=[ast.Starred(value=call,ctx=ast.Load()),*node.elts[3:]],ctx=ast.Load()),node)
        return node
    def visit_FunctionDef(self,node):
        self.generic_visit(node)
        if node.name=='_accept_worse':
            node.body.insert(0,ast.parse('if _weighted_adapter.active:\n    return _weighted_adapter.accept(candidate, current, temperature, rng)').body[0])
            self.counts['acceptance'] += 1
        return node

def transform(text):
    tree=ast.parse(text); transformer=Adapt();tree=transformer.visit(tree)
    idx=0
    while idx<len(tree.body) and (isinstance(tree.body[idx],ast.Expr) and isinstance(tree.body[idx].value,ast.Constant) or isinstance(tree.body[idx],ast.ImportFrom) and tree.body[idx].module=='__future__'):idx+=1
    tree.body.insert(idx,ast.Import(names=[ast.alias(name='weighted_adapter',asname='_weighted_adapter')]))
    return ast.unparse(ast.fix_missing_locations(tree))+'\n',transformer.counts
