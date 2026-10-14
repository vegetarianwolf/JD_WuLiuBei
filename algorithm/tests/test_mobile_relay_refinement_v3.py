"""Physical integration tests for geometry-ranked deadline-region mobile search."""
import json
from time import perf_counter

import pytest

from algorithm.experiments import mobile_relay_refinement as v1
from algorithm.experiments import mobile_relay_refinement_v3 as v3
from algorithm.experiments import mobile_deadline_geometry as region
from algorithm.experiments.audit_mobile_relay import audit_mobile_routes, _bind_meetings
from algorithm.tests.test_mobile_relay_refinement import fixture
from algorithm.tests.test_mobile_relay_refinement_v2 import position_fixture
from uav_dispatch.validation import evaluate_solution


def run(problem, routes, **options):
    return v3.refine(problem, routes, deadline=perf_counter()+2.,
        weights=(.5,.25,.25), scales=(len(problem.tasks),100.,100.),
        max_pairs=16,pool_size=64,**options)


def test_ranked_search_keeps_real_cross_gain_and_independent_synchronization():
    problem,routes=fixture()
    initial=evaluate_solution(problem,routes,_force_full=True)
    result=run(problem,routes)
    assert result['evaluation'].valid
    assert result['evaluation'].score.distance_km==20.<initial.score.distance_km
    assert result['metadata']['final_cross_uav_count']==2
    assert result['metadata']['geometry_bounds_evaluated']>0
    assert result['problem'].tasks==problem.tasks
    assert result['problem'].home_points()==problem.home_points()
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    independent=audit_mobile_routes(tasks,v1.topology(problem),v1.topology(result['problem']),result['routes'])
    _bind_meetings(result['metadata']['mobile_meetings'],independent['meetings'])
    json.dumps(result['metadata'],allow_nan=False)


def test_deadline_region_seeds_enter_complete_feasible_final_search():
    problem,routes,_,_=position_fixture()
    before=evaluate_solution(problem,routes,_force_full=True)
    result=run(problem,routes,pattern_rounds=2,probe_limit=40)
    assert result['evaluation'].valid
    assert result['evaluation'].score.late_count==0
    assert v1.weighted_key(result['evaluation'].score,(.5,.25,.25),(4.,100.,100.)) < v1.weighted_key(before.score,(.5,.25,.25),(4.,100.,100.))
    assert result['metadata']['deadline_seed_searches']
    assert any(s['metadata']['point_records'] for s in result['metadata']['deadline_seed_searches'])
    for search in result['metadata']['deadline_seed_searches']:
        assert all(p['bounds_verified'] for p in search['metadata']['point_records'])


def test_full_confirmation_finishing_after_deadline_does_not_adopt(monkeypatch):
    problem,routes=fixture()
    clock=[0.]
    for module in (v1,v3,region):
        monkeypatch.setattr(module,'perf_counter',lambda:clock[0])
    original,calls=v3.evaluate_solution,[0]
    def check(*args,**kwargs):
        value=original(*args,**kwargs);calls[0]+=1
        if calls[0]>1:clock[0]=2.
        return value
    monkeypatch.setattr(v3,'evaluate_solution',check)
    result=v3.refine(problem,routes,deadline=1.,weights=(.5,.25,.25),scales=(4.,100.,100.),max_pairs=16,pool_size=64)
    assert calls[0]>1
    assert result['routes']==routes and result['problem'] is problem
    assert result['metadata']['deadline_reached'] and result['metadata']['accepted_improvements']==0


def test_geometry_and_seed_limits_validate_and_expired_call_retains_incumbent():
    problem,routes=fixture()
    for options in ({'deadline_tasks':0},{'deadline_iterations':False},{'deadline_seeds':0}):
        with pytest.raises(ValueError):run(problem,routes,**options)
    with pytest.raises(ValueError,match='pool'):
        v3.refine(problem,routes,deadline=perf_counter()+1.,weights=(.5,.25,.25),scales=(4.,100.,100.),max_pairs=32,pool_size=16)
    result=v3.refine(problem,routes,deadline=perf_counter()-1.,weights=(.5,.25,.25),scales=(4.,100.,100.))
    assert result['routes']==routes and result['metadata']['accepted_improvements']==0
