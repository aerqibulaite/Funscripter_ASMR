"""Exact point differences and conservative support under confirmed linear interpolation."""
import bisect
from fractions import Fraction
from .revisions import validate_external


def merged_ranges(ranges):
    result = []
    for start,end in sorted(ranges):
        if start>=end:
            continue
        if result and start<=result[-1][1]:
            result[-1][1] = max(result[-1][1],end)
        else:
            result.append([start,end])
    return result


def complement(ranges,duration):
    result,cursor = [],0
    for start,end in merged_ranges(ranges):
        if cursor<start:
            result.append([cursor,start])
        cursor=max(cursor,end)
    if cursor<duration:
        result.append([cursor,duration])
    return result


def value_at(actions,time):
    times=[point["at"] for point in actions]
    return value_on_grid(actions,times,time)


def value_on_grid(actions,times,time):
    index=bisect.bisect_right(times,time)
    if index==0:
        return Fraction(actions[0]["pos"])
    if index==len(actions):
        return Fraction(actions[-1]["pos"])
    left,right=actions[index-1],actions[index]
    return Fraction(left["pos"])+Fraction((right["pos"]-left["pos"])*(time-left["at"]),right["at"]-left["at"])


def equal_on_ranges(left,right,ranges):
    lt,rt=[point["at"] for point in left],[point["at"] for point in right]
    for start,end in ranges:
        knots={start,end,*[t for t in lt if start<t<end],*[t for t in rt if start<t<end]}
        if any(value_on_grid(left,lt,time)!=value_on_grid(right,rt,time) for time in knots):
            return False
    return True


def affine(left,right):
    if len(left)!=len(right) or len(left)<3:
        return None
    pivot=next((i for i,value in enumerate(left) if value!=left[0]),None)
    if pivot is None:
        return {"scale":"1","offset":str(right[0]-left[0])} if len(set(right))==1 else None
    scale=Fraction(right[pivot]-right[0],left[pivot]-left[0])
    offset=right[0]-scale*left[0]
    if all(scale*a+offset==b for a,b in zip(left,right)):
        return {"scale":str(scale),"offset":str(offset)}
    return None


def support(times,time,duration):
    first,last=bisect.bisect_left(times,time),bisect.bisect_right(times,time)
    return (times[first-1] if first else 0,times[last] if last<len(times) else duration)


def compare_scripts(baseline_raw,external_raw,duration,device,profile,baseline_known=True):
    left_report,left=validate_external(baseline_raw,duration,device)
    right_report,right=validate_external(external_raw,duration,device)
    result={"schema_version":"1.3","baseline_known":baseline_known,"comparison_profile":profile.model_dump(mode="json"),
        "baseline_validation":left_report,"external_validation":right_report,"raw_changes":[],"affected_ranges":[],
        "protected_ranges":[],"confirmed_unaffected_ranges":[],"automatic_merge_allowed":False,
        "interpolation_support":"linear with held endpoints, conditional on explicit comparison confirmation",
        "time_tolerance_behavior":"diagnostic only; exact timestamp changes remain protected","human_intent_inferred":False}
    reason=None
    if not baseline_known:
        result["classification"]="unknown_baseline"
        reason="baseline_not_confirmed"
    elif not left_report["format_valid"] or not right_report["format_valid"]:
        result["classification"]="invalid_input"
        reason="invalid_input_preserve_entire_revision"
    else:
        a,b=left["actions"],right["actions"]
        amap,bmap={p["at"]:p for p in a},{p["at"]:p for p in b}
        changed=sorted(time for time in set(amap)|set(bmap) if amap.get(time)!=bmap.get(time))
        result["raw_changes"]=[{"time_ms":time,"baseline_point":amap.get(time),"external_point":bmap.get(time)} for time in changed]
        metadata_a={key:value for key,value in left.items() if key!="actions"}
        metadata_b={key:value for key,value in right.items() if key!="actions"}
        result["format_fields_changed"]=metadata_a!=metadata_b
        timing=None
        positions=None
        if len(a)==len(b) and [p["pos"] for p in a]==[p["pos"] for p in b]:
            timing=affine([p["at"] for p in a],[p["at"] for p in b])
        if len(a)==len(b) and [p["at"] for p in a]==[p["at"] for p in b]:
            positions=affine([p["pos"] for p in a],[p["pos"] for p in b])
        if metadata_a!=metadata_b:
            result["classification"]="format_fields_changed"
            reason="format_or_unknown_metadata_changed"
        elif not changed:
            result["classification"]="byte_identical" if baseline_raw==external_raw else "point_data_identical"
        elif timing and timing!={"scale":"1","offset":"0"}:
            result.update(classification="global_time_offset" if timing["scale"]=="1" else "global_time_transform",transformation=timing)
            reason="global_time_transform"
        elif positions and positions!={"scale":"1","offset":"0"}:
            result.update(classification="global_position_transform",transformation=positions)
            reason="global_position_transform"
        elif len(a)!=len(b) and equal_on_ranges(a,b,[[0,duration]]):
            result["classification"]="resampled_equivalent_curve_candidate"
            reason="resampling_is_not_proof_of_unchanged_edit_intent"
        else:
            tuples_a={(p["at"],p["pos"]) for p in a}
            tuples_b={(p["at"],p["pos"]) for p in b}
            fraction=len(tuples_a^tuples_b)/max(1,len(a)+len(b))
            result["changed_point_fraction"]=fraction
            if fraction>=profile.max_local_change_fraction:
                result["classification"]="widespread_or_unclassified_change"
                reason="cannot_bound_changes_reliably"
            else:
                result["classification"]="local_point_changes"
        ranges=[]
        atimes,btimes=sorted(amap),sorted(bmap)
        for time in changed:
            aa,ab=support(atimes,time,duration)
            ba,bb=support(btimes,time,duration)
            ranges.append((min(aa,ba,time),max(ab,bb,time)))
        result["affected_ranges"]=[{"start_ms":start,"end_ms":end,"guard_times_ms":[start,end],"reason":"union_of_both_curves_neighbor_support"}
                                   for start,end in merged_ranges(ranges)]
        if len(a)==len(b) and [p["pos"] for p in a]==[p["pos"] for p in b]:
            result["timing_differences_within_configured_tolerance"]=bool(changed) and all(abs(x["at"]-y["at"])<=profile.time_tolerance_ms for x,y in zip(a,b))
        if not profile.external_interpolation_confirmed or profile.interpolation!="linear":
            reason=reason or "external_interpolation_or_endpoint_behavior_unconfirmed"
    if reason:
        result["protected_ranges"]=[{"start_ms":0,"end_ms":duration,"guard_times_ms":[0,duration],"reason":reason}]
    else:
        result["protected_ranges"]=result["affected_ranges"]
        result["confirmed_unaffected_ranges"]=[{"start_ms":start,"end_ms":end} for start,end in
            complement([(r["start_ms"],r["end_ms"]) for r in result["protected_ranges"]],duration)]
        result["automatic_merge_allowed"]=bool(result["confirmed_unaffected_ranges"])
    result["protection_scope"]="entire_revision" if reason else "local_support"
    return result
