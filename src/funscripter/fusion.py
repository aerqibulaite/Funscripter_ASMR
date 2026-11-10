"""Deterministic, provenance-preserving single-axis fusion."""
from copy import deepcopy
from .models import QualitativeState, GenerationConfig, Segment
from .motion import generate
from .planner import digest
from .semantic_contracts import CORE_FIELDS, event_identity
from .controls import prepare_controls, update_controls, curve_preset, generate_controlled

PACE = ["slow", "medium", "fast"]


def unknown_state():
    return QualitativeState().model_dump()


def transition(state, event, unknown_policy):
    """Apply an absolute target once, or one bounded relative step. Never generate numbers."""
    before = deepcopy(state)
    after = deepcopy(state)
    reasons = []
    explicit = any(event[key] != "unknown" for key in ["pace_tier", "amplitude_tier", "motion_style"])
    intent, pace = event["intent"], event["pace_tier"]
    if intent == "unknown" and not explicit:
        reasons.append("unknown_semantics")
        return (unknown_state() if unknown_policy == "hold_position" else before), reasons
    if intent == "pause" and pace not in ("stop", "unknown"):
        return before, ["pause_absolute_pace_conflict"]
    if intent in ("start", "continue", "resume", "increase_rate", "decrease_rate") and pace == "stop":
        return before, ["intent_stop_conflict"]
    if intent in ("increase_rate", "decrease_rate"):
        direction = 1 if intent == "increase_rate" else -1
        if before["pace_tier"] == "unknown":
            if pace == "unknown":
                return before, ["relative_change_unknown_initial_state"]
            reasons.append("relative_direction_unverifiable_absolute_target_used")
        else:
            current = PACE.index(before["pace_tier"])
            if pace in PACE:
                delta = PACE.index(pace) - current
                if delta * direction < 0:
                    return before, ["relative_absolute_direction_conflict"]
                if delta == 0:
                    reasons.append("absolute_target_already_reached")
                # Explicit absolute advice sets a target; do not also apply a step.
            else:
                target = max(0, min(len(PACE)-1, current + direction))
                after["pace_tier"] = PACE[target]
                if target == current:
                    reasons.append("relative_change_at_tier_limit")
    if pace in PACE:
        after["pace_tier"] = pace
    for key in ["amplitude_tier", "motion_style"]:
        if event[key] != "unknown":
            if key == "motion_style" and event[key] == "hold" and (intent == "pause" or pace == "stop"):
                # Pausing is orthogonal to the active style needed for a later resume.
                continue
            after[key] = event[key]
    if intent == "pause" or pace == "stop":
        after["paused"] = True
    elif intent in ("start", "resume", "continue"):
        if "unknown" in (after["pace_tier"], after["amplitude_tier"], after["motion_style"]) or after["motion_style"] == "hold":
            return before, ["resume_target_unknown"]
        after["paused"] = False
    return after, reasons


def combine_compatible_events(incoming):
    """Combine one action with complementary maintain fields, without inventing order."""
    events = [event for event, _ in incoming]
    if any(event is None for event in events) or len({event["cue_id"] for event in events}) != 1:
        return None
    principal = [index for index, event in enumerate(events) if event["intent"] not in ("maintain", "unknown")]
    # Multiple relative changes, pause/resume, etc. need ordering we do not have.
    if len(principal) > 1:
        return None
    index = principal[0] if principal else 0
    merged = deepcopy(events[index])
    for key in ("pace_tier", "amplitude_tier", "motion_style"):
        values = {event[key] for event in events if event[key] != "unknown"}
        if len(values) > 1:
            return None
        if values:
            merged[key] = values.pop()
    merged["needs_review"] = any(event["needs_review"] for event in events)
    return merged, incoming[index][1]


def resolve_boundaries(timeline, candidate_doc, profile, duration_ms):
    candidates = deepcopy(candidate_doc)
    lookup = {cue.cue_id: cue for cue in timeline.cues}
    if set(profile.manual_boundaries_ms) - set(lookup):
        raise ValueError("manual boundary references an unknown cue")
    records = {row["cue_id"]: row for row in candidates["cues"]}
    adopted = {}
    for cue in timeline.cues:
        row = records.setdefault(cue.cue_id, {"cue_id": cue.cue_id, "utterance_end_candidates": []})
        saved = row.get("accepted_candidate")
        row["accepted_candidate"] = None
        manual = profile.manual_boundaries_ms.get(cue.cue_id)
        if saved and saved.get("status") == "human_boundary":
            time = saved.get("time_ms")
            if type(time) is not int or cue.end_ms is None or not max(0, cue.start_ms) < time <= min(cue.end_ms, duration_ms):
                raise ValueError("saved human boundary is outside its cue span")
            if manual is not None and manual != time:
                raise ValueError("manual profile conflicts with saved human boundary")
            boundary = dict(saved)
        elif manual is not None:
            if cue.end_ms is None or not max(0, cue.start_ms) < manual <= min(cue.end_ms, duration_ms):
                raise ValueError("manual boundary must be after cue start and within its candidate span")
            boundary = {"time_ms": manual, "method": "manual_profile", "status": "human_boundary",
                        "evidence_id": "manual_end_" + cue.cue_id}
        elif profile.use_empty_timestamp_hints:
            usable = [item for item in row["utterance_end_candidates"]
                      if item["method"] == "empty_timestamp_hint" and cue.end_ms is not None
                      and max(0, cue.start_ms) < item["time_ms"] <= min(cue.end_ms, duration_ms)]
            unique = {item["time_ms"] for item in usable}
            boundary = (dict(usable[0], status="inferred_draft_boundary") if len(unique) == 1 else None)
        else:
            boundary = None
        if boundary:
            row["accepted_candidate"] = boundary
            adopted[cue.cue_id] = boundary
    candidates["cues"] = list(records.values())
    return candidates, adopted


def build_plan(project, timeline, duration_ms, semantic, outline, audio, candidate_doc, profile):
    version2 = semantic.get("schema_version") == "1.4"
    assessments = {row["cue_id"]: row for row in semantic.get("cue_assessments", [])}
    cues = {cue.cue_id: cue for cue in timeline.cues}
    if set(profile.rhythm_anchors_ms) - set(cues):
        raise ValueError("rhythm anchors reference an unknown cue")
    control_schedule, control_dispositions = prepare_controls(semantic, cues, profile, duration_ms)
    candidate_doc, boundaries = resolve_boundaries(timeline, candidate_doc, profile, duration_ms)
    end_rows = {row["cue_id"]: row for row in candidate_doc["cues"]}
    end_anchors = {event["cue_id"] for event in semantic["events"] if event["anchor"] == "after_utterance"}
    phases = []
    if outline:
        for phase in outline.phases:
            first, last = cues[phase.start_cue_id], cues[phase.end_cue_id_inclusive]
            start, end = max(0, first.start_ms), min(duration_ms, last.end_ms or duration_ms)
            if start < end:
                phases.append({"phase_id": phase.phase_id, "start_ms": start, "end_ms": end})
    if not phases:
        phases = [{"phase_id": "phase_unresolved", "start_ms": 0, "end_ms": duration_ms}]
    if version2:
        for left, right in zip(phases, phases[1:]):
            if left["end_ms"] > right["start_ms"]:
                if right["start_ms"] <= left["start_ms"]:
                    raise ValueError("chapter boundary has no positive span; review simultaneous starts")
                left["end_ms"] = right["start_ms"]
                left["boundary_review_reason"] = "overlapping_subtitle_chapter_boundary_clipped"
    if any(left["end_ms"] > right["start_ms"] for left, right in zip(phases, phases[1:])):
        raise ValueError("chapter time spans overlap; review simultaneous subtitle boundaries")
    cue_phase = {}
    if outline:
        ordered = [cue.cue_id for cue in timeline.cues]
        for index, phase in enumerate(outline.phases):
            first, last = ordered.index(phase.start_cue_id), ordered.index(phase.end_cue_id_inclusive)
            # Reject a collapsed/out-of-range phase rather than incorrectly matching its index.
            if index >= len(phases) or phases[index]["phase_id"] != phase.phase_id:
                raise ValueError("outline has a phase with no valid audio span")
            for cue_id in ordered[first:last+1]:
                cue_phase[cue_id] = index
    else:
        cue_phase = {cue_id: 0 for cue_id in cues}
    schedule, dispositions = {}, []
    seen = set()
    for event in semantic["events"]:
        identity = event_identity(event)
        record = {"event_id": identity, "cue_id": event["cue_id"], "status": "pending", "reasons": []}
        dispositions.append(record)
        if identity in seen:
            record.update(status="duplicate_skipped", reasons=["event_already_seen"])
            continue
        seen.add(identity)
        cue = cues[event["cue_id"]]
        if (cue.end_ms is None or not 0 <= cue.start_ms < cue.end_ms <= duration_ms):
            record.update(status="unresolved", reasons=["invalid_subtitle_interval"])
            if 0 <= cue.start_ms < duration_ms:
                schedule.setdefault(cue.start_ms, []).append((None, record))
            continue
        if version2 and (event["statement_kind"] != "instruction" or assessments[event["cue_id"]]["status"] == "ambiguous"):
            record.update(status="unresolved", reasons=["non_instruction_or_ambiguous_event"])
            schedule.setdefault(cue.start_ms, []).append((None, record))
            continue
        index = cue_phase[cue.cue_id]
        phase = phases[index]
        time = None
        if event["scope"] == "uncertain":
            record["reasons"].append("uncertain_scope")
        elif event["scope"] == "next_section":
            if index + 1 >= len(phases):
                record["reasons"].append("next_section_missing")
            elif event["anchor"] != "cue_start":
                record["reasons"].append("next_section_anchor_conflict")
            else:
                phase = phases[index + 1]
                time = phase["start_ms"]
                record["anchor_source"] = "next_section_start"
        elif event["anchor"] == "cue_start":
            time = cue.start_ms
            record["anchor_source"] = "subtitle_cue_start"
        elif event["anchor"] == "after_utterance" and cue.cue_id in boundaries:
            boundary = boundaries[cue.cue_id]
            time = boundary["time_ms"]
            record["anchor_source"] = boundary["status"]
            record["boundary_evidence_id"] = boundary["evidence_id"]
        else:
            record["reasons"].append("uncertain_anchor")
        if time is not None and not phase["start_ms"] <= time < phase["end_ms"]:
            record["reasons"].append("anchor_outside_section")
            time = None
        if time is None:
            record["status"] = "unresolved"
            # This timestamp is an uncertainty marker, NOT the claimed effective event time.
            if 0 <= cue.start_ms < duration_ms:
                schedule.setdefault(cue.start_ms, []).append((None, record))
        else:
            record["effective_time_ms"] = time
            record["phase_id"] = phase["phase_id"]
            schedule.setdefault(time, []).append((event, record))
    event_cues = {event["cue_id"] for event in semantic["events"]}
    for assessment in assessments.values():
        cue = cues[assessment["cue_id"]]
        status = assessment["status"]
        if status in ("processed", "failed") or cue.cue_id in event_cues:
            continue
        record = {"cue_id": cue.cue_id, "event_id": None,
                  "status": "context_only" if status == "no_new_information" else status,
                  "reasons": ["assessment:" + status, assessment["reason"]]}
        dispositions.append(record)
        if status == "ambiguous" and 0 <= cue.start_ms < duration_ms:
            schedule.setdefault(cue.start_ms, []).append((None, record))
    for failure in semantic["failures"]:
        cue = cues[failure["cue_id"]]
        record = {"cue_id": cue.cue_id, "event_id": None, "status": "failed_semantics",
                  "reasons": [failure["reason"]]}
        dispositions.append(record)
        if 0 <= cue.start_ms < duration_ms:
            schedule.setdefault(cue.start_ms, []).append((None, record))
    manual = project.config.generation.segments
    profile_presets = dict(profile.presets)
    for key, preset in project.config.generation.presets.items():
        if key in profile_presets and profile_presets[key] != preset:
            raise ValueError("fusion/manual preset IDs conflict")
        profile_presets[key] = preset
    edges = {0, duration_ms, *schedule, *control_schedule}
    for phase in phases:
        edges.update([phase["start_ms"], phase["end_ms"]])
    for segment in manual:
        edges.update([segment.start_ms, segment.end_ms])
    for boundary in boundaries.values():
        edges.add(boundary["time_ms"])
    edges = sorted(edge for edge in edges if 0 <= edge <= duration_ms)
    state = profile.initial_state.model_dump()
    state_sources, active_reasons = [], ["configured_initial_state"]
    state_history, segments = [], []
    active_control, explicit_parameters = {}, False
    resets = {phase["start_ms"]: phase["phase_id"] for phase in phases}
    for start, end in zip(edges, edges[1:]):
        if start in resets:
            if not version2:
                state, state_sources = profile.initial_state.model_dump(), []
                active_reasons = ["section_initial_state"]
            else:
                active_reasons = list(active_reasons) + ["summary_boundary_state_preserved"]
        incoming = schedule.get(start, [])
        controls_here = control_schedule.get(start, [])
        old_sequence = (active_control.get("progress") or {}).get("sequence_cue_id")
        active_control = update_controls(active_control, controls_here, profile, duration_ms)
        new_progress = active_control.get("progress") or {}
        if new_progress.get("sequence_cue_id") != old_sequence or any(
                row["directive"].get("status") == "reset" for row in controls_here):
            explicit_parameters = False
        context_reasons = []
        actionable = [(event, record) for event, record in incoming if event is not None and
                      (event["intent"] != "unknown" or any(event[key] != "unknown" for key in ["pace_tier", "amplitude_tier", "motion_style"]))]
        if actionable:
            selected = []
            for event, record in incoming:
                if event is not None and event["intent"] == "unknown" and all(event[key] == "unknown" for key in ["pace_tier", "amplitude_tier", "motion_style"]):
                    record["status"] = "context_only"
                    record["reasons"].append("unknown_context_at_explicit_event")
                    context_reasons.append("unknown_context_at_explicit_event")
                else:
                    selected.append((event, record))
            incoming = selected
        if incoming:
            before = deepcopy(state)
            previous_sources = list(state_sources)
            original_incoming = incoming
            combined = combine_compatible_events(incoming) if len(incoming) > 1 else None
            if combined is not None:
                incoming = [combined]
            if len(incoming) > 1:
                active_reasons = ["simultaneous_events_need_review"]
                if profile.unknown_policy == "hold_position":
                    state = unknown_state()
                for _, record in incoming:
                    record["status"] = "conflict"
                    record["reasons"].extend(active_reasons)
                state_sources = [record["event_id"] for _, record in incoming if record["event_id"]]
            else:
                event, record = incoming[0]
                if event is None:
                    active_reasons = record["reasons"] or ["unresolved_event"]
                    if profile.unknown_policy == "hold_position":
                        state = unknown_state()
                else:
                    transition_input = deepcopy(state)
                    default_fields = []
                    if profile.action_defaults is not None and event["intent"] in ("start", "resume", "continue"):
                        defaults = profile.action_defaults.model_dump()
                        for key in ("pace_tier", "amplitude_tier", "motion_style"):
                            if transition_input[key] == "unknown" and event[key] == "unknown" and defaults[key] != "unknown":
                                transition_input[key] = defaults[key]
                                default_fields.append(key)
                    candidate, active_reasons = transition(transition_input, event, profile.unknown_policy)
                    fatal = any(reason in active_reasons for reason in ["pause_absolute_pace_conflict",
                        "intent_stop_conflict", "relative_change_unknown_initial_state",
                        "relative_absolute_direction_conflict", "resume_target_unknown"])
                    if fatal:
                        record["status"] = "conflict"
                        if profile.unknown_policy == "hold_position":
                            state = unknown_state()
                    else:
                        state = candidate
                        record["status"] = "applied_to_draft"
                        if event["intent"] in ("increase_rate", "decrease_rate") or any(
                                event[key] != "unknown" for key in ("pace_tier", "amplitude_tier")):
                            explicit_parameters = True
                        if event["intent"] in ("start", "resume", "continue") and not any(
                                row["directive"]["kind"] != "countdown" for row in controls_here):
                            active_control.pop("blocked_task", None)
                            if event["intent"] == "start":
                                active_control.pop("task", None)
                        if default_fields:
                            record["configured_default_fields"] = default_fields
                            active_reasons.append("configured_action_defaults:" + ",".join(default_fields))
                    record["reasons"].extend(active_reasons)
                    if record.get("anchor_source") == "inferred_draft_boundary":
                        active_reasons = active_reasons + ["inferred_utterance_boundary"]
                    if event["needs_review"]:
                        active_reasons = active_reasons + ["model_requested_review"]
                if combined is not None:
                    active_reasons.append("compatible_same_cue_events_combined")
                    record["reasons"].append("compatible_same_cue_events_combined")
                    for _, other in original_incoming:
                        if other is not record:
                            other["status"] = record["status"]
                            other["reasons"].extend(record["reasons"])
                state_sources = [row["event_id"] for _, row in original_incoming if row["event_id"]]
            if state == unknown_state():
                previous_sources = []
            state_sources = list(dict.fromkeys(previous_sources + state_sources))
            state_history.append({"time_ms": start, "input_state": before, "output_state": deepcopy(state),
                                  "event_ids": list(state_sources), "input_event_ids": previous_sources, "reasons": list(active_reasons)})
        reasons = list(active_reasons) + context_reasons
        reasons.extend(phase["boundary_review_reason"] for phase in phases
                       if phase.get("boundary_review_reason") and phase["start_ms"] <= start < phase["end_ms"])
        preset_id, mode = None, "hold_position"
        override = next((segment for segment in manual if segment.start_ms <= start and end <= segment.end_ms), None)
        if override:
            preset_id, mode = override.preset_id, "preset"
            active_control.pop("task", None)
            reasons.append("manual_segment_priority")
        elif active_control.get("blocked_task"):
            reasons.append("unresolved_control_hold_position")
        elif state["paused"] or state["motion_style"] == "hold":
            reasons.append("explicit_pause_or_hold")
        elif "unknown" in (state["pace_tier"], state["amplitude_tier"], state["motion_style"]):
            reasons.append("unresolved_state_hold_position")
        else:
            key = "/".join(state[field] for field in ["pace_tier", "amplitude_tier", "motion_style"])
            preset_id = profile.tier_map.get(key)
            if preset_id:
                mode = "preset"
            else:
                reasons.append("unmapped_qualitative_state_hold_position")
        if not override and mode == "preset" and not explicit_parameters:
            selected = curve_preset(active_control, profile)
            if selected:
                preset_id = selected
                reasons.append("configured_progress_curve")
        if active_control.get("progress"):
            reasons.append("script_progress_not_physiological_feedback")
        audio_ids, periods = [], []
        for window in (audio or {}).get("periodicity", []):
            if window["start_ms"] < end and window["end_ms"] > start:
                found = [candidate for feature in window["features"].values() for candidate in feature["candidates"]]
                if found:
                    audio_ids.append(window["evidence_id"])
                    periods.extend(candidate["period_ms"] for candidate in found)
        if not audio:
            reasons.append("audio_evidence_missing")
        elif periods and mode == "hold_position":
            reasons.append("audio_period_candidate_during_hold")
        elif periods and preset_id:
            period = profile_presets[preset_id].period_ms
            if min(abs(value-period)/period for value in periods) > profile.audio_period_tolerance:
                reasons.append("audio_preset_period_difference_review")
        cue_ids = [cue.cue_id for cue in timeline.cues if cue.start_ms < end and (cue.end_ms or cue.start_ms) > start]
        regions = []
        for cue_id in cue_ids:
            boundary = boundaries.get(cue_id)
            end_row = end_rows.get(cue_id, {})
            if cue_id in end_anchors and boundary is None:
                reasons.extend("utterance_end:" + reason for reason in end_row.get("review_reasons", []))
            regions.append({"cue_id": cue_id, "region": ("inter_cue_gap" if start >= boundary["time_ms"] else "utterance_candidate") if boundary else "unsplit_cue",
                            "boundary_status": boundary["status"] if boundary else "unknown_utterance_end",
                            "end_candidate_ids": [item["evidence_id"] for item in end_row.get("utterance_end_candidates", [])]})
        if len(cue_ids) > 1:
            reasons.append("overlapping_subtitle_coverage")
        if not cue_ids:
            reasons.append("no_subtitle_coverage")
        if timeline.issues:
            reasons.append("subtitle_input_issues")
        segments.append({"segment_id": "segment_" + digest([start, end, mode, preset_id])[:16],
            "start_ms": start, "end_ms": end, "mode": mode, "preset_id": preset_id,
            "state": deepcopy(state), "cue_ids": cue_ids, "subtitle_regions": regions, "event_ids": list(state_sources),
            "audio_evidence_ids": sorted(set(audio_ids)), "needs_review": True,
            "review_reasons": sorted(set(reasons)), "manual_source_id": override.segment_id if override else None,
            "boundary_status": "manual_segment" if override else "generated_draft"})
        segments[-1]["control"] = deepcopy(active_control) or None
    # Coalesce identical active presets before calling the generator: evidence-only splits
    # must not reset phase. Static spans remain explicit holds between generated segments.
    generated_segments = []
    for segment in segments:
        if segment["mode"] != "preset":
            continue
        if generated_segments and generated_segments[-1].end_ms == segment["start_ms"] and generated_segments[-1].preset_id == segment["preset_id"]:
            generated_segments[-1].end_ms = segment["end_ms"]
        else:
            generated_segments.append(Segment(segment_id=segment["segment_id"], start_ms=segment["start_ms"],
                end_ms=segment["end_ms"], preset_id=segment["preset_id"]))
    config = project.config.model_copy(deep=True)
    config.generation = GenerationConfig(initial_position=project.config.generation.initial_position,
        presets=profile_presets, segments=generated_segments)
    script, validation = generate(config, duration_ms, allow_constraint_errors=True)
    control_execution = []
    if any((segment.get("control") or {}).get("task") for segment in segments):
        script, validation, control_execution = generate_controlled(config, segments, duration_ms)
    validation.update(reviewed_export=False, human_review_required=True)
    return {"schema_version": "1.3", "profile": profile.model_dump(mode="json"), "phases": phases,
            "segments": segments, "event_dispositions": dispositions, "state_transitions": state_history,
            "context_notes": deepcopy(semantic.get("context_notes", [])),
            "control_dispositions": control_dispositions, "control_execution": control_execution,
            "status": "needs_review"}, candidate_doc, script, validation
