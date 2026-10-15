"""Joint-source typed structure on the frozen paid multi-relay pipeline.

Only three process-local module aliases change.  Construction, native ALNS,
canonical snapshots, core clocks and strict original-input adoption are the
frozen implementation; the extension records its own setup/teardown interval.
"""
from __future__ import annotations

from contextlib import contextmanager
from time import perf_counter

try:
    import multi_relay_native_refinement_v1 as wrapper
    import joint_source_relay_structure_v1 as joint_structure
except ImportError:
    from . import multi_relay_native_refinement_v1 as wrapper
    from . import joint_source_relay_structure_v1 as joint_structure

NAME = 'joint_source_pair_structure_context_v1'
SOURCE_POLICY = 'joint_pair_only_new_groups_mixed_parent_groups_supported'
ALIAS_NAMES = ('raw.structure', 'polish.structure', 'wrapper.structure')
_active = False


def _aliases():
    return (wrapper.raw_method, wrapper.polish, wrapper)


@contextmanager
def joint_structure_context(*, statistics=None):
    """Install the typed structure once, restoring actual originals on exit.

    The yielded dictionary remains live until after ``finally`` so a caller
    can attach the completed identity/timing receipt without cloning metadata.
    No core clock or absolute deadline is replaced or extended.
    """
    global _active
    if _active:
        raise RuntimeError('joint-source structure context is not nestable')
    modules = _aliases()
    originals = tuple(module.structure for module in modules)
    # Frozen callers catch their already-bound Deadline class; changing that
    # class would turn ordinary expiry into an uncaught error.
    if any(joint_structure.Deadline is not module.Deadline for module in modules):
        raise RuntimeError('typed structure must preserve the frozen Deadline identity')
    entered = perf_counter()
    receipt = {} if statistics is None else statistics
    receipt.update(name=NAME, source_policy=SOURCE_POLICY,
        alias_names=list(ALIAS_NAMES), hook_identity={name: dict(installed=False, restored=False)
            for name in ALIAS_NAMES}, restored=False,
        entered_at_perf_counter=entered, left_at_perf_counter=None, elapsed_seconds=None)
    _active = True
    try:
        for name, module in zip(ALIAS_NAMES, modules):
            module.structure = joint_structure
            receipt['hook_identity'][name]['installed'] = module.structure is joint_structure
        yield receipt
    finally:
        for name, module, original in reversed(tuple(zip(ALIAS_NAMES, modules, originals))):
            module.structure = original
            receipt['hook_identity'][name]['restored'] = module.structure is original
        receipt['restored'] = all(item['restored'] for item in receipt['hook_identity'].values())
        _active = False
        left = perf_counter()
        receipt.update(left_at_perf_counter=left, elapsed_seconds=left-entered)
        if not receipt['restored']:
            raise RuntimeError('joint-source structure aliases did not restore')


def refine(problem, routes, *, deadline, weights, scales, seed, relay_enabled=True,
           raw_fraction=.4, max_groups=3, beam_width=4, max_families=16,
           source_option_limit=3, receiver_option_limit=3, position_limit=12,
           snapshot_limit=6, polish_max_iterations=1_000_000,
           polish_initial_temperature=.0025, polish_minimum_temperature=.00001,
           polish_cooling_rate=.97, alns_module=None):
    """Run the existing paid pipeline using joint-only new group proposals."""
    if relay_enabled is not True:
        raise ValueError('joint-source native refinement is R-only')
    with joint_structure_context() as extension:
        result = wrapper.refine(problem, routes, deadline=deadline, weights=weights,
            scales=scales, seed=seed, relay_enabled=relay_enabled,
            raw_fraction=raw_fraction, max_groups=max_groups, beam_width=beam_width,
            max_families=max_families, source_option_limit=source_option_limit,
            receiver_option_limit=receiver_option_limit, position_limit=position_limit,
            snapshot_limit=snapshot_limit, polish_max_iterations=polish_max_iterations,
            polish_initial_temperature=polish_initial_temperature,
            polish_minimum_temperature=polish_minimum_temperature,
            polish_cooling_rate=polish_cooling_rate, alns_module=alns_module)
    result['metadata']['extension'] = extension
    return result
