"""The optimized traversal must prove the same real entities and reject corruption."""
from copy import deepcopy
import pytest
from algorithm.tests.test_conditional_donor_refinement_v1 import tiny,W,S
from algorithm.experiments import audit_conditional_donor_repair_v1 as original
from algorithm.experiments import audit_conditional_donor_metadata_v2 as fast


def test_request_local_registry_keeps_exact_original_independent_proof(tiny):
    p,base,graph,tasks,outcomes=tiny
    for relay,result in outcomes.items():
        for entry,seed in zip(result['metadata']['repair_records'],result['metadata']['seed_records']):
            args=(tasks,graph,base,seed['raw_snapshot']['routes'],entry['metadata'])
            kw=dict(seed_move=seed['seed_move'],focus_task_ids=seed['focus_task_ids'],relay_enabled=relay,
                weights=W,scales=S,deadline=entry['deadline_perf_counter'])
            assert fast.audit_metadata(*args,**kw)==original.audit_metadata(*args,**kw)


@pytest.mark.parametrize('tamper',['clock','donor','best'])
def test_both_independent_traversals_reject_same_bad_entity(tiny,tamper):
    p,base,graph,tasks,outcomes=tiny;meta=outcomes[True]['metadata'];seed=meta['seed_records'][0];entry=meta['repair_records'][0]
    damaged=deepcopy(entry['metadata'])
    if tamper=='clock':damaged['input_snapshot']['event_times_min'][0][0]+=1
    elif tamper=='donor':damaged['steps'][0]['parent_records'][0]['catalogue']['records'][0]['b']=99
    else:damaged['best_key'][0]-=1
    kw=dict(seed_move=seed['seed_move'],focus_task_ids=seed['focus_task_ids'],relay_enabled=True,
        weights=W,scales=S,deadline=entry['deadline_perf_counter'])
    for proof in (original,fast):
        with pytest.raises((AssertionError,ValueError,KeyError)):
            proof.audit_metadata(tasks,graph,base,seed['raw_snapshot']['routes'],damaged,**kw)
