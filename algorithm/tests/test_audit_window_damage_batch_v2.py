"""Known auxiliary-bug exception is narrower than a general telemetry repair."""
from copy import deepcopy
from pathlib import Path

import pytest

from algorithm.experiments import audit_window_damage_batch_v2 as supplementary
from algorithm.tests.test_audit_window_damage_repair import fixture, snapshot, W, S
from algorithm.tests.test_audit_deadline_window_repooling import descriptor


def direct_shared_fixture():
    tasks, topology, base, raw, move = fixture()
    raw = ((5,-5,3,-3),(6,-6,4,-4),(1,2,-1,-2,7,-7),raw[3])
    move = descriptor((tasks,topology,base,raw,move),raw,relay=False)
    return tasks,topology,base,raw,move


def known_bug_snapshot():
    data = direct_shared_fixture()
    saved = snapshot(data,data[3])
    proof = supplementary.strict_post(*data[:4],data[3],data[4],relay_enabled=False)
    owner = proof['pinned_final_owners']['1']
    whole = proof['core_reference']['route_records'][owner]['distance_km']
    assert whole != proof[supplementary.FIELD]
    saved[supplementary.FIELD] = whole
    return data,saved,proof


def replay(data,saved,corrections=None):
    return supplementary.audit_post_with_known_direct_bug(*data[:4],data[3],data[4],
        relay_enabled=data[4]['mode']=='RELAY',telemetry=saved,weights=W,scales=S,
        corrections=corrections)


def test_only_exact_known_direct_whole_route_bug_is_corrected_without_mutating_snapshot():
    data,saved,expected=known_bug_snapshot();original=deepcopy(saved);corrections=[]
    with pytest.raises(ValueError,match='current_shared_loaded_distance_km'):
        supplementary.strict_post(*data[:4],data[3],data[4],relay_enabled=False,
            telemetry=saved,weights=W,scales=S)
    proof=replay(data,saved,corrections)
    assert proof==expected and saved==original
    assert corrections[0]['changed_fields']==[supplementary.FIELD]
    assert corrections[0]['original_value']==saved[supplementary.FIELD]
    assert corrections[0]['expected_value']==expected[supplementary.FIELD]
    assert corrections[0]['owner_route']==list(data[3][2])
    assert corrections[0]['original_snapshot_mutated'] is False


@pytest.mark.parametrize('wrong',[0.,999.,float('nan'),-1.])
def test_arbitrary_direct_wrong_value_is_never_repaired(wrong):
    data,saved,_=known_bug_snapshot();saved[supplementary.FIELD]=wrong;corrections=[]
    with pytest.raises(ValueError):replay(data,saved,corrections)
    assert not corrections


@pytest.mark.parametrize('field',['post_global_score','event_times_min','key','current_classifications'])
def test_all_other_telemetry_stays_strict_even_with_known_bug(field):
    data,saved,_=known_bug_snapshot();corrections=[]
    if field=='post_global_score':saved[field]['distance_km']+=1.
    elif field=='event_times_min':saved[field][0][0]+=1.
    elif field=='key':saved[field][0]+=.1
    else:saved[field]['pooled_rescued_task_ids']=[999]
    with pytest.raises(ValueError):replay(data,saved,corrections)
    assert not corrections


def test_relay_whole_owner_route_wrong_value_is_never_corrected():
    data=fixture();saved=snapshot(data,data[3]);proof=supplementary.strict_post(
        *data[:4],data[3],data[4],relay_enabled=True)
    saved[supplementary.FIELD]=proof['core_reference']['route_records'][2]['distance_km']
    corrections=[]
    with pytest.raises(ValueError):replay(data,saved,corrections)
    assert not corrections


def test_direct_different_pin_owners_does_not_match_exception_and_correct_data_remains_strict():
    data=fixture(direct=True);saved=snapshot(data,data[3]);corrections=[]
    assert replay(data,saved,corrections)['passed'] and not corrections
    saved[supplementary.FIELD]=16.
    with pytest.raises(ValueError):replay(data,saved,corrections)
    assert not corrections


def setup_supplementary_files(path):
    ledger=supplementary.original_batch.ledger
    manifest=dict(execution_sources={
        'audit_window_damage_batch.py':dict(sha256=ledger.sha(Path(supplementary.original_batch.__file__))),
        'audit_window_damage_repair.py':dict(sha256=ledger.sha(Path(supplementary.strict_post.__globals__['__file__'])))})
    ledger.write(path/'manifest.json',manifest);ledger.write(path/'runs.json',[])
    return ledger


def test_orchestration_captures_old_write_restores_hooks_and_never_calls_original_pass(tmp_path,monkeypatch):
    ledger=setup_supplementary_files(tmp_path);data,saved,_=known_bug_snapshot()
    before={name:ledger.sha(tmp_path/name) for name in ('manifest.json','runs.json')}
    oldpost=supplementary.original_batch.audit_window_damage_repair_proposal;oldwrite=ledger.write
    def fake_original(output):
        for _ in range(2):
            supplementary.original_batch.audit_window_damage_repair_proposal(*data[:4],data[3],data[4],
                relay_enabled=False,telemetry=saved,weights=W,scales=S)
        result=dict(passed=True,results=[dict(entity='tiny-only')])
        ledger.write(Path(output)/'audit.json',result)
        return result
    monkeypatch.setattr(supplementary.original_batch,'audit',fake_original)
    result=supplementary.audit(tmp_path)
    assert result['strict_original_telemetry_passed'] is False
    assert result['original_complete_audit_passed'] is False
    assert result['entity_physics_and_other_telemetry_passed'] is True
    assert 'passed' not in result and 'passed' not in result['corrected_memory_full_replay']
    assert result['correction_replay_calls']==2 and result['unique_corrected_snapshots']==1
    assert result['corrections'][0]['replay_calls']==2
    assert not (tmp_path/'audit.json').exists()
    assert (tmp_path/'supplementary_audit_v2.json').exists()
    assert all(ledger.sha(tmp_path/name)==value for name,value in before.items())
    assert supplementary.original_batch.audit_window_damage_repair_proposal is oldpost and ledger.write is oldwrite
    assert not hasattr(supplementary.original_batch,'_supplementary_v2_active')


@pytest.mark.parametrize('failure',['exception','unexpected_write','nested'])
def test_exception_unexpected_write_and_nested_calls_restore_every_hook(tmp_path,monkeypatch,failure):
    ledger=setup_supplementary_files(tmp_path)
    oldpost=supplementary.original_batch.audit_window_damage_repair_proposal;oldwrite=ledger.write
    def fake_original(output):
        if failure=='exception':raise RuntimeError('fixture error')
        if failure=='unexpected_write':ledger.write(Path(output)/'unexpected.json',{})
        else:supplementary.audit(output)
    monkeypatch.setattr(supplementary.original_batch,'audit',fake_original)
    with pytest.raises((RuntimeError,ValueError)):supplementary.audit(tmp_path)
    assert supplementary.original_batch.audit_window_damage_repair_proposal is oldpost and ledger.write is oldwrite
    assert not hasattr(supplementary.original_batch,'_supplementary_v2_active')
    assert not any((tmp_path/name).exists() for name in ('audit.json','unexpected.json','supplementary_audit_v2.json'))


def test_original_audit_json_and_frozen_hash_mismatch_are_not_bypassed(tmp_path):
    ledger=setup_supplementary_files(tmp_path)
    ledger.write(tmp_path/'audit.json',{'passed':False})
    with pytest.raises(ValueError,match='remain absent'):supplementary.audit(tmp_path)
    (tmp_path/'audit.json').unlink()
    manifest=ledger.read(tmp_path/'manifest.json');manifest['execution_sources']['audit_window_damage_batch.py']['sha256']='wrong'
    ledger.write(tmp_path/'manifest.json',manifest)
    with pytest.raises(ValueError,match='differs from frozen030'):supplementary.audit(tmp_path)
