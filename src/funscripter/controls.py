"""Generic, evidence-linked progress and finite repetition controls."""
from copy import deepcopy
from .planner import digest
from .motion import validate_actions


def prepare_controls(semantic, cues, profile, duration_ms):
    schedule, records = {}, []
    assessments = {row["cue_id"]: row["status"] for row in semantic.get("cue_assessments", [])}
    starts = {event["cue_id"] for event in semantic["events"] if event["intent"] in ("start", "resume", "continue")
              and event.get("statement_kind") == "instruction" and event["scope"] == "current_section"
              and event["anchor"] == "cue_start"}
    for note in semantic.get("context_notes", []):
        directive = note.get("control")
        if directive is None:
            continue
        record = {"control_id": "control_" + digest([note["cue_id"], directive])[:20],
                  "cue_id": note["cue_id"], "directive": deepcopy(directive), "status": "pending", "reasons": []}
        records.append(record)
        if note["kind"] == "rule_definition" and note["cue_id"] not in starts:
            record["status"] = "definition_only"
            continue
        cue = cues[note["cue_id"]]
        if cue.end_ms is None or not 0 <= cue.start_ms < cue.end_ms <= duration_ms:
            record.update(status="unresolved", reasons=["invalid_control_interval"])
            continue
        if assessments.get(cue.cue_id) == "ambiguous":
            record.update(status="unresolved", reasons=["ambiguous_control"])
            continue
        record["effective_time_ms"] = cue.start_ms
        record["execution_allowed"] = note["basis"] == "explicit" or profile.allow_inferred_controls
        schedule.setdefault(cue.start_ms, []).append(record)
    return schedule, records


def update_controls(active, records, profile, duration_ms):
    active = deepcopy(active)
    tasks = [row for row in records if row["directive"]["kind"] != "countdown"]
    if len({row["directive"]["kind"] for row in tasks}) > 1:
        for row in tasks:
            row.update(status="conflict", reasons=["simultaneous_different_task_controls"])
        active.pop("task", None)
        active["blocked_task"] = True
        records = [row for row in records if row["directive"]["kind"] == "countdown"]
    for kind in {row["directive"]["kind"] for row in records}:
        group = [row for row in records if row["directive"]["kind"] == kind]
        if len(group) > 1:
            for row in group:
                row.update(status="conflict", reasons=["simultaneous_controls_of_same_kind"])
            active.pop("progress" if kind == "countdown" else "task", None)
            if kind != "countdown":
                active["blocked_task"] = True
            continue
        row = group[0]
        directive = row["directive"]
        key = "progress" if kind == "countdown" else "task"
        if directive.get("status", "update") == "end":
            active.pop(key, None)
            if key == "task":
                active.pop("blocked_task", None)
            row["status"] = "ended"
            continue
        item = {**deepcopy(directive), "control_id": row["control_id"], "cue_id": row["cue_id"],
                "execution_allowed": row["execution_allowed"]}
        previous_task = active.get("task")
        if (kind != "countdown" and previous_task and previous_task["kind"] == kind
                and previous_task["sequence_cue_id"] == directive["sequence_cue_id"]
                and directive.get("status", "update") == "update"
                and previous_task.get("count") == directive.get("count")
                and previous_task["unit"] == directive["unit"] and row["execution_allowed"]):
            row.update(status="continued", applied_control_id=previous_task["control_id"])
            continue
        if kind == "countdown":
            values = directive["progress"]
            item["normalized"] = (values["start_value"] - values["current_value"]) / (values["start_value"] - values["end_value"])
            previous = active.get("progress")
            if (previous and previous["sequence_cue_id"] == item["sequence_cue_id"]
                    and item["normalized"] < previous["normalized"] and directive.get("status", "update") != "reset"):
                item["execution_allowed"] = False
                row["reasons"].append("progress_regressed_without_reset")
            active[key] = item
            row["status"] = "observed"
        elif not row["execution_allowed"]:
            active.pop(key, None)
            active["blocked_task"] = True
            row.update(status="unresolved", reasons=["inferred_control_not_enabled"])
        elif directive["unit"] == "unknown":
            active.pop(key, None)
            active["blocked_task"] = True
            row.update(status="unresolved", reasons=["control_unit_unknown"])
        elif kind == "rhythm_follow":
            anchors = profile.rhythm_anchors_ms.get(directive["sequence_cue_id"], [])
            if not anchors or anchors[0] <= row["effective_time_ms"] or anchors[-1] > duration_ms:
                active.pop(key, None)
                active["blocked_task"] = True
                row.update(status="unresolved", reasons=["confirmed_rhythm_anchors_missing_or_outside_interval"])
            elif directive["unit"] == "round_trip" and any(b-a < 2 for a, b in zip(
                    [row["effective_time_ms"], *anchors], anchors)):
                active.pop(key, None)
                active["blocked_task"] = True
                row.update(status="unresolved", reasons=["round_trip_anchor_interval_too_short"])
            else:
                item["anchors_ms"] = anchors
                item["start_ms"] = row["effective_time_ms"]
                active[key] = item
                active.pop("blocked_task", None)
                row["status"] = "scheduled"
        else:
            active[key] = item
            active.pop("blocked_task", None)
            row["status"] = "scheduled"
    return active


def curve_preset(control, profile):
    progress = control.get("progress")
    if not progress or not progress["execution_allowed"] or not profile.progress_curve:
        return None
    return next(point.preset_id for point in reversed(profile.progress_curve) if point.at <= progress["normalized"])


def generate_controlled(config, segments, duration_ms):
    """Render generic count/anchor tasks; preserve task state through paused spans."""
    current = config.generation.initial_position
    points, tasks, results = {0: current}, {}, {}
    baseline = None
    for segment in segments:
        start, end = segment["start_ms"], segment["end_ms"]
        task = (segment.get("control") or {}).get("task")
        identity = task["control_id"] if task else None
        if identity and identity not in tasks:
            tasks[identity] = {"origin": current, "completed_legs": 0, "target": None, "remaining_ms": 0,
                               "anchor_index": 0, "beat_interrupted": False}
            results[identity] = {"control_id": identity, "kind": task["kind"], "unit": task["unit"],
                                 "completed": 0, "requested": task.get("count") if task["kind"] == "repetition" else len(task["anchors_ms"]),
                                 "status": "pending"}
        state = tasks.get(identity)
        if segment["mode"] != "preset":
            points[start] = round(current)
            points[end] = round(current)
            if state:
                state["beat_interrupted"] = True
            baseline = None
            continue
        preset = config.generation.presets[segment["preset_id"]]
        points[start] = round(current)
        time = start
        if not task:
            if baseline is None or baseline["preset_id"] != segment["preset_id"]:
                target = preset.high if current < preset.high else preset.low
                baseline = {"preset_id": segment["preset_id"], "target": target,
                            "remaining_ms": round(preset.period_ms * (preset.up_time_ratio if target == preset.high else 1-preset.up_time_ratio))}
            state = baseline
        else:
            baseline = None
        while time < end:
            if task and task["kind"] == "repetition":
                limit = task["count"] * (2 if task["unit"] == "round_trip" else 1)
                if state["completed_legs"] >= limit:
                    results[identity]["status"] = "completed"
                    break
            if task and task["kind"] == "rhythm_follow":
                anchors = task["anchors_ms"]
                # Past anchors are missed, never replayed after a pause.
                while state["anchor_index"] < len(anchors) and anchors[state["anchor_index"]] <= time:
                    state["anchor_index"] += 1
                    state["target"] = None
                if state["anchor_index"] >= len(anchors):
                    break
                anchor = anchors[state["anchor_index"]]
                if state["target"] is None:
                    state["origin"] = current
                    state["beat_interrupted"] = False
                    state["returning"] = False
                    state["target"] = preset.high if current < preset.high else preset.low
                    state["target_ms"] = (time + (anchor-time)//2) if task["unit"] == "round_trip" else anchor
                # A pause may leave an intermediate point behind the resumed timestamp.
                if state["target_ms"] <= time:
                    state["target_ms"] = anchor
                    state["target"] = state["origin"] if task["unit"] == "round_trip" else state["target"]
                    state["returning"] = True
                remaining = state["target_ms"] - time
                if remaining <= 0:
                    state["anchor_index"] += 1
                    state["target"] = None
                    continue
            else:
                if state["target"] is None:
                    returning = task and task["unit"] == "round_trip" and state["completed_legs"] % 2 == 1
                    state["target"] = state["origin"] if returning else (preset.high if current < preset.high else preset.low)
                    fraction = preset.up_time_ratio if state["target"] > current else 1-preset.up_time_ratio
                    state["remaining_ms"] = max(1, round(preset.period_ms * fraction))
                remaining = state["remaining_ms"]
            elapsed = min(remaining, end-time)
            current = current + (state["target"]-current) * elapsed / remaining
            time += elapsed
            points[time] = round(current)
            state["remaining_ms"] = remaining - elapsed
            if elapsed == remaining:
                if task and task["kind"] == "rhythm_follow":
                    if task["unit"] == "round_trip" and not state["returning"]:
                        state["target"] = state["origin"]
                        state["target_ms"] = anchor
                        state["returning"] = True
                    else:
                        if not state["beat_interrupted"]:
                            results[identity]["completed"] += 1
                        state["anchor_index"] += 1
                        state["target"] = None
                else:
                    if task:
                        state["completed_legs"] += 1
                        results[identity]["completed"] = state["completed_legs"] // (2 if task["unit"] == "round_trip" else 1)
                        state["target"] = None
                    else:
                        state["target"] = preset.low if state["target"] == preset.high else preset.high
                        state["remaining_ms"] = max(1, round(preset.period_ms * (
                            preset.up_time_ratio if state["target"] == preset.high else 1-preset.up_time_ratio)))
        points[end] = round(current)
    points[duration_ms] = round(current)
    for identity, result in results.items():
        if result["requested"] is not None and result["completed"] == result["requested"]:
            result["status"] = "completed"
        elif result["kind"] == "rhythm_follow":
            result["status"] = "anchors_exhausted" if tasks[identity]["anchor_index"] == result["requested"] else "incomplete"
        else:
            result["status"] = "incomplete"
    script = {"version": "1.0", "inverted": False, "range": 100,
              "actions": [{"at": time, "pos": pos} for time, pos in sorted(points.items())]}
    return script, validate_actions(script["actions"], duration_ms, config.device), list(results.values())
