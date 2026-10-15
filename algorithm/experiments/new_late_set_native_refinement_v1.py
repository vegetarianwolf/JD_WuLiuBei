"""Insert a complete newly-late repair stage before each paid native leaf.

The frozen joint/raw/outer pipeline and native metadata clocks are preserved.
Only the native callable is scoped, restored, and explicitly traced.
"""
from __future__ import annotations

from contextlib import contextmanager
from math import isfinite
from time import perf_counter
try:
    import joint_source_native_refinement_v1 as joint
    import new_late_set_repair_v1 as focus
except ImportError:
    from . import joint_source_native_refinement_v1 as joint
    from . import new_late_set_repair_v1 as focus

NAME = 'new_late_set_before_native_context_v1'
_active = False


def _parameters(fraction, seconds, donor_limit, max_donors, position_limit, option_count, max_damage):
    if (any(type(x) is bool or not isfinite(x) or x<=0 for x in (fraction,seconds)) or fraction>=1
        or any(type(v) is not int or not 0<=v<=n for v,n in ((donor_limit,4),(max_donors,2)))
        or any(type(v) is not int or not 1<=v<=n for v,n in
            ((position_limit,256),(option_count,3),(max_damage,4))) or option_count>position_limit):
        raise ValueError('bounded finite focused-stage parameters required')
    return dict(focus_fraction=fraction,focus_max_seconds=seconds,focus_donor_limit=donor_limit,
        focus_max_donors=max_donors,focus_position_limit=position_limit,
        focus_option_count=option_count,focus_max_damage=max_damage)


@contextmanager
def focused_native_context(base_routes, *, parameters, statistics=None):
    """One nonnested callable replacement, always restoring actual identity."""
    global _active
    if _active:
        raise RuntimeError('new-late focused native context is not nestable')
    base=tuple(map(tuple,base_routes));module=joint.wrapper.polish
    original=module.polish_groups
    entered=perf_counter();receipt={} if statistics is None else statistics
    receipt.update(name=NAME,parameters=dict(parameters),base_routes_sha256=module.metrics._digest(base),
        alias_name='wrapper.polish.polish_groups',hook_identity=dict(installed=False,restored=False),
        restored=False,entered_at_perf_counter=entered,left_at_perf_counter=None,elapsed_seconds=None)
    def focused(problem,raw_routes,groups,**kwargs):
        raw=tuple(map(tuple,raw_routes));start=perf_counter();leaf=kwargs['deadline']
        subdeadline=min(leaf,start+min(parameters['focus_max_seconds'],
            max(0.,leaf-start)*parameters['focus_fraction']))
        outcome=focus.refine(problem,base,raw,groups,deadline=subdeadline,
            weights=kwargs['weights'],scales=kwargs['scales'],seed=kwargs['seed'],
            donor_limit=parameters['focus_donor_limit'],max_donors=parameters['focus_max_donors'],
            position_limit=parameters['focus_position_limit'],option_count=parameters['focus_option_count'],
            max_damage=parameters['focus_max_damage'],snapshot_limit=kwargs.get('snapshot_limit',6))
        fm=outcome['metadata'];checkpoint=fm.get('post_repair_snapshot')
        used=fm.get('returned_checkpoint_checked_within_deadline') is True
        if used:
            history=[fm.get('input_snapshot'),*fm.get('best_checkpoint_history',[])]
            if (outcome.get('evaluation') is None or not isinstance(checkpoint,dict)
                or not any(checkpoint is s for s in history if s is not None)
                or checkpoint.get('routes')!=[list(r) for r in outcome['routes']]
                or checkpoint.get('routes_sha256')!=module.metrics._digest(outcome['routes'])
                or checkpoint.get('deadline_perf_counter')!=subdeadline
                or checkpoint.get('full_verified_at_perf_counter',subdeadline)>=subdeadline):
                raise ValueError('focus return is not an existing matching predeadline complete checkpoint')
            native_input=tuple(map(tuple,outcome['routes']))
        else:
            if outcome.get('evaluation') is not None or checkpoint is not None:
                raise ValueError('expired focus fabricated a complete certificate')
            native_input=raw
        result=original(problem,native_input,groups,**kwargs)
        failed=result['metadata'].get('returned_checkpoint_checked_within_deadline') is not True
        if failed:
            if result.get('evaluation') is not None or result['metadata'].get('post_repair_snapshot') is not None:
                raise ValueError('unchecked native return fabricated a complete checkpoint')
            # The frozen outer wrapper must take its existing skip branch.
            # A real focus checkpoint is not a certificate from native init.
            result['routes']=raw
        result['metadata']['focused_stage']=dict(metadata=fm,
            base_routes_sha256=module.metrics._digest(base),original_raw_routes_sha256=module.metrics._digest(raw),
            focused_return_routes_sha256=module.metrics._digest(outcome['routes']),
            native_input_routes_sha256=module.metrics._digest(native_input),focus_used=used,
            native_initialization_failed=failed,focus_started_at_perf_counter=start,
            focus_deadline_perf_counter=subdeadline)
        return result
    _active=True
    try:
        module.polish_groups=focused
        receipt['hook_identity']['installed']=module.polish_groups is focused
        yield receipt
    finally:
        module.polish_groups=original
        receipt['hook_identity']['restored']=module.polish_groups is original
        receipt['restored']=receipt['hook_identity']['restored']
        _active=False
        left=perf_counter();receipt.update(left_at_perf_counter=left,elapsed_seconds=left-entered)
        if not receipt['restored']:
            raise RuntimeError('focused native callable did not restore')


def refine(problem,routes,*,deadline,weights,scales,seed,relay_enabled=True,
           raw_fraction=.4,max_groups=3,beam_width=4,max_families=16,
           source_option_limit=3,receiver_option_limit=3,position_limit=12,
           snapshot_limit=6,polish_max_iterations=1_000_000,
           polish_initial_temperature=.0025,polish_minimum_temperature=.00001,
           polish_cooling_rate=.97,alns_module=None,focus_fraction=.4,
           focus_max_seconds=20.,focus_donor_limit=4,focus_max_donors=2,
           focus_position_limit=12,focus_option_count=3,focus_max_damage=4):
    if relay_enabled is not True:
        raise ValueError('new-late-set native refinement is R-only')
    parameters=_parameters(focus_fraction,focus_max_seconds,focus_donor_limit,
        focus_max_donors,focus_position_limit,focus_option_count,focus_max_damage)
    with focused_native_context(routes,parameters=parameters) as extension:
        result=joint.refine(problem,routes,deadline=deadline,weights=weights,scales=scales,seed=seed,
            relay_enabled=relay_enabled,raw_fraction=raw_fraction,max_groups=max_groups,
            beam_width=beam_width,max_families=max_families,source_option_limit=source_option_limit,
            receiver_option_limit=receiver_option_limit,position_limit=position_limit,
            snapshot_limit=snapshot_limit,polish_max_iterations=polish_max_iterations,
            polish_initial_temperature=polish_initial_temperature,
            polish_minimum_temperature=polish_minimum_temperature,
            polish_cooling_rate=polish_cooling_rate,alns_module=alns_module)
    result['metadata']['damage_extension']=extension
    return result
