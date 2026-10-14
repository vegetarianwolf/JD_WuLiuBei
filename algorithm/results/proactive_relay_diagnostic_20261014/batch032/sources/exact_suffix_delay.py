"""Conditional native N/L for one nonnegative uniform suffix delay.

Pure standard library: no search, model, physical route evaluator or runner.
Exact N refers to the declared float expression ``raw_lateness + shift``.
Near a deadline, even ``(clock + shift) - due`` can classify differently;
flags require the future caller to replay its actual physical route. Nothing
here certifies a whole solution, a noncontiguous insertion or a global bound.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import fsum,isfinite,ulp
from time import perf_counter

EPSILON_MIN=1e-9
DEFAULT_SUM_TOLERANCE_MIN=1e-10
DEFAULT_BOUNDARY_TOLERANCE_MIN=1e-10


class Deadline(Exception):
    """Optional primitive deadline; this module is not integrated with a solver."""


def _check(deadline):
    if deadline is not None:
        if type(deadline) is bool or not isfinite(deadline):raise ValueError('finite absolute deadline or None required')
        if perf_counter()>=deadline:raise Deadline


def _number(value,label,*,nonnegative=False):
    if type(value) not in (int,float):raise ValueError('finite '+label+' required')
    try:converted=float(value)
    except (OverflowError,ValueError):raise ValueError('finite '+label+' required') from None
    if not isfinite(converted) or nonnegative and converted<0:
        raise ValueError('finite '+('nonnegative ' if nonnegative else '')+label+' required')
    return converted


def _sum(values,label):
    try:result=fsum(values)
    except (OverflowError,ValueError):raise ValueError('nonfinite '+label) from None
    if not isfinite(result):raise ValueError('nonfinite '+label)
    return result


@dataclass(frozen=True,slots=True)
class LatenessIndex:
    raw_lateness_min:tuple[float,...]
    suffix_sums_min:tuple[float|None,...]
    suffix_sum_error_bounds_min:tuple[float,...]


@dataclass(frozen=True,slots=True)
class DirectSuffixIndex:
    suffix:LatenessIndex
    suffix_deliveries:tuple[tuple[int,float,float],...]
    prefix_late_count:int
    prefix_lateness_min:float
    gap:int
    route_event_count:int
    load_at_gap:int
    original_final_delivery_count:int
    epsilon_min:float
    maximum_clock_min:float
    maximum_due_min:float


def build_lateness_index(raw_lateness,*,deadline=None):
    """Sort finite raw x and precompute O(n) conservative summed-tail errors."""
    _check(deadline);values=[]
    for value in raw_lateness:
        _check(deadline);values.append(_number(value,'raw lateness'))
    values.sort();_check(deadline)
    sums=[0.]*(len(values)+1);errors=[0.]*(len(values)+1)
    for k in range(len(values)-1,-1,-1):
        _check(deadline)
        if sums[k+1] is None:
            sums[k]=None;errors[k]=float('inf');continue
        try:value=fsum((values[k],sums[k+1]))
        except (OverflowError,ValueError):value=None
        if value is None or not isfinite(value):
            sums[k]=None;errors[k]=float('inf')
        else:
            sums[k]=value
            # One ulp per rounded addition is conservative, including a
            # possible double-rounding implementation of math.fsum.
            errors[k]=errors[k+1]+ulp(value)
    result=LatenessIndex(tuple(values),tuple(sums),tuple(errors));_check(deadline)
    return result


def shifted_lateness(index,shift_min,*,epsilon_min=EPSILON_MIN,
                     boundary_tolerance_min=DEFAULT_BOUNDARY_TOLERANCE_MIN,
                     sum_tolerance_min=DEFAULT_SUM_TOLERANCE_MIN,deadline=None):
    """O(log n) strict-EPS count; a declared-tolerance sum with safe fallback.

    Binary search deliberately compares x+s, not x>epsilon-s. The returned
    count is for that float expression only. Flags do not prove equivalence
    to future physical route clocks or license a hard feasibility/J prune.
    """
    _check(deadline)
    if not isinstance(index,LatenessIndex):raise ValueError('LatenessIndex required')
    shift=_number(shift_min,'shift',nonnegative=True);eps=_number(epsilon_min,'epsilon',nonnegative=True)
    boundary=_number(boundary_tolerance_min,'boundary tolerance',nonnegative=True)
    tolerance=_number(sum_tolerance_min,'sum tolerance',nonnegative=True)
    xs=index.raw_lateness_min;n=len(xs)
    if xs and not isfinite(xs[-1]+shift):raise ValueError('nonfinite shifted lateness')
    low,high=0,n
    while low<high:
        _check(deadline);middle=(low+high)//2
        if xs[middle]+shift>eps:high=middle
        else:low=middle+1
    first=low;count=n-first
    neighbors=[xs[k]+shift for k in (first-1,first) if 0<=k<n]
    near=any(abs(value-eps)<=boundary for value in neighbors)
    fallback=False;error=0.;total=0.
    if count:
        raw=index.suffix_sums_min[first];product=count*shift
        magnitude=max(abs(xs[first]),abs(xs[-1]))+shift
        if raw is None or not isfinite(product) or not isfinite(magnitude):
            error=float('inf')
        else:
            total=raw+product
            error=(index.suffix_sum_error_bounds_min[first]+ulp(product)+
                   (ulp(total) if isfinite(total) else float('inf'))+count*ulp(magnitude))
        if not isfinite(total) or total<0 or error>tolerance:
            _check(deadline)
            total=_sum((x+shift for x in xs[first:]),'shifted lateness sum')
            error=ulp(total);fallback=True
    result=dict(late_count=count,total_lateness_min=total,shift_min=shift,epsilon_min=eps,
        suffix_task_count=n,first_strictly_late_index=first,boundary_sensitive=near,
        boundary_tolerance_min=boundary,sum_tolerance_min=tolerance,
        lateness_sum_error_bound_min=error,sum_within_declared_tolerance=error<=tolerance,
        summation_fallback_used=fallback,needs_exact_route_replay=near or error>tolerance,
        classification_arithmetic='(raw_lateness + shift) > epsilon',
        scoring_scope='single_uniform_shift_lateness_only',complete_solution=False,
        global_objective_bound=False)
    _check(deadline);return result


def _time_map(values,label):
    if not isinstance(values,Mapping):raise ValueError(label+' mapping required')
    result={}
    for key,value in values.items():
        if type(key) is int:task=key
        elif type(key) is str and key.isdigit() and str(int(key))==key:task=int(key)
        else:raise ValueError('positive canonical task IDs required')
        if task<1 or task in result:raise ValueError('distinct positive task IDs required')
        result[task]=_number(value,label,nonnegative=True)
    return result


def index_direct_suffix(record,deadlines,gap,*,epsilon_min=EPSILON_MIN,deadline=None):
    """Index one gap of a caller-verified, whole-DIRECT native route record.

    Validates paired native events/load/clocks, not geometry/home correctness.
    C callers must first remove their two tasks and re-time that skeleton.
    Suffix means delivery-event position >= gap, including already-held cargo.
    """
    _check(deadline)
    if not isinstance(record,Mapping):raise ValueError('real DIRECT route record required')
    eps=_number(epsilon_min,'epsilon',nonnegative=True)
    due=_time_map(deadlines,'deadline');delivered=_time_map(record.get('delivery_times_min'),'delivery clock')
    route=tuple(record.get('route',()));times=tuple(record.get('event_times_min',()))
    if type(gap) is not int or not 0<=gap<=len(route) or len(times)!=len(route):raise ValueError('valid event gap and all event clocks required')
    held=set();seen=set();closed=set();loads=[0];rows=[];previous=0.
    for position,(visit,value) in enumerate(zip(route,times)):
        _check(deadline)
        if type(visit) is not int or visit==0 or abs(visit) not in due:raise ValueError('whole known DIRECT task visits required')
        clock=_number(value,'event clock',nonnegative=True)
        if clock<previous:raise ValueError('monotone true event clocks required')
        task=abs(visit)
        if visit>0:
            if task in seen:raise ValueError('unique DIRECT pickup required')
            held.add(task);seen.add(task)
            if len(held)>2:raise ValueError('native route exceeds capacity2')
        else:
            if task not in held or task in closed:raise ValueError('paired DIRECT pickup before delivery required')
            held.remove(task);closed.add(task)
            if task not in delivered or delivered[task]!=clock:raise ValueError('true delivery clock differs from its negative event')
            rows.append((task,position,clock,due[task]))
        loads.append(len(held));previous=clock
    if held or seen!=closed or set(delivered)!=closed:raise ValueError('complete native whole-DIRECT event pairs required')
    prefix=[clock-due for _,position,clock,due in rows if position<gap and clock-due>eps]
    suffix=tuple((task,clock,due) for task,position,clock,due in rows if position>=gap)
    result=DirectSuffixIndex(build_lateness_index((clock-due for _,clock,due in suffix),deadline=deadline),suffix,
        len(prefix),_sum(prefix,'prefix lateness'),gap,len(route),loads[gap],len(rows),eps,
        max((clock for _,clock,_ in suffix),default=0.),max((due for _,_,due in suffix),default=0.))
    _check(deadline);return result


def shift_direct_suffix(index,shift_min,*,insertion_kind='uniform',
                        boundary_tolerance_min=DEFAULT_BOUNDARY_TOLERANCE_MIN,
                        sum_tolerance_min=DEFAULT_SUM_TOLERANCE_MIN,deadline=None):
    """Native-only N/L; selected deliveries and flight D are never added here.

    Near a boundary, report the explicit (original_clock+s)-due float model,
    while retaining the raw-x result and requiring actual-route replay.
    """
    _check(deadline)
    if not isinstance(index,DirectSuffixIndex):raise ValueError('DirectSuffixIndex required')
    if insertion_kind not in ('uniform','source_IN','receiver_OUT'):raise ValueError('declared uniform insertion kind required')
    if insertion_kind=='source_IN' and index.load_at_gap>1:raise ValueError('single IN requires native load0 or1')
    if insertion_kind=='receiver_OUT' and index.load_at_gap!=0:raise ValueError('double OUT requires native load0')
    shift=_number(shift_min,'shift',nonnegative=True)
    boundary=_number(boundary_tolerance_min,'boundary tolerance',nonnegative=True)
    shifted_scale=index.maximum_clock_min+shift
    if not isfinite(shifted_scale):raise ValueError('nonfinite shifted clock')
    margin=boundary+4*(ulp(index.maximum_clock_min)+ulp(index.maximum_due_min)+ulp(shifted_scale))
    raw=shifted_lateness(index.suffix,shift,epsilon_min=index.epsilon_min,boundary_tolerance_min=margin,
        sum_tolerance_min=sum_tolerance_min,deadline=deadline)
    count,total=raw['late_count'],raw['total_lateness_min'];fallback=raw['boundary_sensitive']
    if fallback:
        values=[]
        for _,clock,due in index.suffix_deliveries:
            _check(deadline);late=(clock+shift)-due
            if late>index.epsilon_min:values.append(late)
        count=len(values);total=_sum(values,'clock-reassociated suffix lateness')
    combined=_sum((index.prefix_lateness_min,total),'whole native lateness')
    error=ulp(combined)+ulp(index.prefix_lateness_min)+(ulp(total) if fallback else raw['lateness_sum_error_bound_min'])
    result=dict(late_count=index.prefix_late_count+count,total_lateness_min=combined,
        prefix_late_count=index.prefix_late_count,prefix_lateness_min=index.prefix_lateness_min,
        shifted_suffix_late_count=count,shifted_suffix_lateness_min=total,
        suffix_task_ids=[task for task,_,_ in index.suffix_deliveries],shift_min=shift,gap=index.gap,
        load_at_gap=index.load_at_gap,original_final_delivery_count=index.original_final_delivery_count,
        source_IN_adds_final_delivery_count=0,selected_OUT_deliveries_included=False,distance_included=False,
        boundary_sensitive=raw['boundary_sensitive'],clock_reassociation_fallback_used=fallback,
        needs_exact_route_replay=raw['needs_exact_route_replay'] or error>sum_tolerance_min,
        lateness_sum_error_bound_min=error,sum_within_declared_tolerance=error<=sum_tolerance_min,
        raw_uniform_result=raw,classification_arithmetic='prefix original; suffix (clock + shift) - due on boundary fallback',
        boundary_margin_min=margin,boundary_margin_scope='clock-reassociation scale estimate; no bitwise physical-route equivalence theorem',
        insertion_kind=insertion_kind,scoring_scope='original_native_tasks_at_one_uniform_event_gap_only',
        complete_solution=False,global_objective_bound=False)
    _check(deadline);return result
