"""Versioned semantic evidence, coverage, and stable identity checks."""
import hashlib
import json
from .models import SemanticBatchV2

CORE_FIELDS = ["cue_id", "intent", "scope", "anchor", "pace_tier", "amplitude_tier", "motion_style", "evidence"]
V2_FIELDS = ["source_cue_ids", "source_evidence", "statement_kind"]


def normalize_batch_v2(result):
    """Repair review metadata only; never invent an action or its evidence."""
    changes = []
    assessments = {row.cue_id: row for row in result.cue_assessments}
    for event in result.events:
        assessment = assessments.get(event.cue_id)
        uncertain = (event.intent == "unknown" or event.scope == "uncertain"
                     or event.anchor != "cue_start" or event.statement_kind != "instruction"
                     or (assessment is not None and assessment.status == "ambiguous"))
        if uncertain and (not event.needs_review or not event.reason.strip()):
            event.needs_review = True
            if not event.reason.strip():
                event.reason = "系统标记复核：事件含未知意图、不确定范围、估计锚点或非直接指令；原模型未提供理由。"
            changes.append({"cue_id": event.cue_id, "kind": "review_metadata_completed"})
    anchored = {row.cue_id for row in [*result.events, *result.context_notes]}
    for assessment in result.cue_assessments:
        if assessment.status == "processed" and assessment.cue_id not in anchored:
            assessment.status = "ambiguous"
            assessment.reason += "；系统标记待复核：模型未提供对应事件或情境笔记，未补造动作。"
            changes.append({"cue_id": assessment.cue_id, "kind": "orphan_processed_marked_ambiguous"})
    return changes


def validated_prefix_v2(result, targets, context):
    """Retain a contiguous valid prefix, preserving chronological scene memory."""
    ids = [cue.cue_id for cue in targets]
    assessment_ids = [row.cue_id for row in result.cue_assessments]
    if len(assessment_ids) != len(ids) or set(assessment_ids) != set(ids):
        return None
    if any(row.cue_id not in ids for row in [*result.events, *result.context_notes]):
        return None
    accepted = []
    for cue in targets:
        one = result.model_copy(update={
            "cue_assessments": [row for row in result.cue_assessments if row.cue_id == cue.cue_id],
            "events": [row for row in result.events if row.cue_id == cue.cue_id],
            "context_notes": [row for row in result.context_notes if row.cue_id == cue.cue_id]})
        try:
            validate_batch_v2(one, [cue], [*context, *[c for c in targets if c.cue_id != cue.cue_id]])
        except ValueError:
            break
        accepted.append(cue.cue_id)
    if not accepted or len(accepted) == len(ids):
        return None
    return result.model_copy(update={
        "cue_assessments": [row for row in result.cue_assessments if row.cue_id in accepted],
        "events": [row for row in result.events if row.cue_id in accepted],
        "context_notes": [row for row in result.context_notes if row.cue_id in accepted]})


def event_identity(event):
    fields = CORE_FIELDS + (V2_FIELDS if "source_cue_ids" in event else [])
    payload = {key: event[key] for key in fields}
    if "source_cue_ids" in event:
        payload["source_cue_ids"] = sorted(event["source_cue_ids"])
        payload["source_evidence"] = [{"cue_id": cue_id, "quote": quote} for cue_id, quote in
            sorted({(row["cue_id"], row["quote"]) for row in event["source_evidence"]})]
    return "event_" + hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def validate_batch_v2(result, targets, context=(), final=False):
    targets = {cue.cue_id: cue for cue in targets}
    lookup = {cue.cue_id: cue for cue in [*context, *targets.values()]}
    assessments = {row.cue_id: row for row in result.cue_assessments}
    if len(assessments) != len(result.cue_assessments) or set(assessments) != set(targets):
        raise ValueError("semantic_assessment_coverage_missing_duplicate_or_unowned")
    if any(not row.reason.strip() for row in result.cue_assessments):
        raise ValueError("semantic_assessment_reason_empty")
    if not final and any(row.status in ("excluded", "failed") for row in result.cue_assessments):
        raise ValueError("semantic_model_cannot_exclude_or_fail_targets")
    anchored = {}
    identities = set()
    note_anchors = set()
    for note in result.context_notes:
        # A non-executable scene memo may restate ongoing context without a new
        # control instruction. Keep stricter ownership/status rules for controls.
        allowed = {"processed", "ambiguous"}
        if note.control is None and note.kind in ("scene_inference", "state_continuation"):
            allowed.add("no_new_information")
        if note.cue_id not in targets or assessments[note.cue_id].status not in allowed:
            status = assessments[note.cue_id].status if note.cue_id in assessments else "not_target"
            raise ValueError(f"semantic_note_anchor_or_assessment_invalid: cue_id={note.cue_id}; assessment={status}; rule/control notes require processed or ambiguous; only non-executable scene/state memos may use no_new_information")
        sources = note.source_cue_ids
        if len(sources) != len(set(sources)) or note.cue_id not in sources or set(sources) - set(lookup):
            raise ValueError("semantic_note_source_ids_invalid")
        quoted = set()
        for evidence in note.source_evidence:
            if evidence.cue_id not in sources or not evidence.quote.strip() or evidence.quote not in lookup[evidence.cue_id].text_for_analysis:
                raise ValueError(f"semantic_note_evidence_not_in_source: note={note.cue_id}; source={evidence.cue_id}; quote original input verbatim, without translation or correction")
            quoted.add(evidence.cue_id)
        if quoted != set(sources):
            raise ValueError(f"semantic_note_evidence_coverage_missing: note={note.cue_id}; missing={sorted(set(sources)-quoted)}; provide original quotes for every declared source")
        if note.control is not None and note.control.sequence_cue_id not in sources:
            raise ValueError("semantic_control_sequence_requires_source_evidence: note=" + note.cue_id
                + "; sequence_cue_id=" + note.control.sequence_cue_id
                + "; include this sequence cue in source_cue_ids and quote its provided original text in source_evidence")
        note_anchors.add(note.cue_id)
    for event in result.events:
        if event.cue_id not in targets:
            raise ValueError("semantic_event_anchor_not_owned_by_target")
        if assessments[event.cue_id].status not in ("processed", "ambiguous"):
            raise ValueError("semantic_event_conflicts_with_assessment")
        sources = event.source_cue_ids
        if len(sources) != len(set(sources)) or event.cue_id not in sources or set(sources) - set(lookup):
            raise ValueError("semantic_event_source_ids_invalid")
        quoted = set()
        for evidence in event.source_evidence:
            if evidence.cue_id not in sources or not evidence.quote.strip() or evidence.quote not in lookup[evidence.cue_id].text_for_analysis:
                raise ValueError(f"semantic_source_evidence_not_in_source_cue: event={event.cue_id}; source={evidence.cue_id}; quote original input verbatim, without translation or correction")
            quoted.add(evidence.cue_id)
        if quoted != set(sources):
            raise ValueError(f"semantic_source_evidence_coverage_missing: event={event.cue_id}; missing={sorted(set(sources)-quoted)}; provide original quotes for every declared source")
        explicit = any(getattr(event, field) != "unknown" for field in
                       ("intent", "pace_tier", "amplitude_tier", "motion_style"))
        if (explicit and not event.evidence.strip()) or (event.evidence and event.evidence not in targets[event.cue_id].text_for_analysis):
            raise ValueError(f"semantic_evidence_not_in_source_cue: event={event.cue_id}; evidence must be a verbatim substring of this target cue, in its original language")
        uncertain = (event.intent == "unknown" or event.scope == "uncertain"
                     or event.anchor != "cue_start" or event.statement_kind != "instruction"
                     or assessments[event.cue_id].status == "ambiguous")
        if uncertain and (not event.needs_review or not event.reason.strip()):
            raise ValueError("semantic_uncertainty_requires_review_reason")
        identity = event_identity(event.model_dump(mode="json"))
        if identity in identities:
            raise ValueError("semantic_duplicate_event_identity")
        identities.add(identity)
        anchored.setdefault(event.cue_id, []).append(event)
    for cue_id, assessment in assessments.items():
        if assessment.status == "processed" and cue_id not in anchored and cue_id not in note_anchors:
            raise ValueError(f"semantic_processed_assessment_without_event: cue_id={cue_id}; processed requires an event or context_note anchored to this cue; if neither exists use no_new_information")
    indices = {cue_id: index for index, cue_id in enumerate(targets)}
    result.events.sort(key=lambda event: indices[event.cue_id])
    result.context_notes.sort(key=lambda note: indices[note.cue_id])


def parse_saved_v2(data, cues, eligible_ids):
    raw = []
    for event in data["events"]:
        payload = {key: value for key, value in event.items() if key not in ("event_id", "review_status")}
        if event.get("event_id") != event_identity(payload):
            raise ValueError("semantic event identity mismatch")
        raw.append(payload)
    result = SemanticBatchV2.model_validate({"schema_version": "1.4", "events": raw,
                                            "cue_assessments": data["cue_assessments"],
                                            "context_notes": data.get("context_notes", [])})
    validate_batch_v2(result, cues, final=True)
    excluded = {row.cue_id for row in result.cue_assessments if row.status == "excluded"}
    if excluded != {cue.cue_id for cue in cues} - set(eligible_ids):
        raise ValueError("semantic_exclusions_do_not_match_input_eligibility")
    failed = [row["cue_id"] for row in data["failures"]]
    expected = {row.cue_id for row in result.cue_assessments if row.status == "failed"}
    if len(failed) != len(set(failed)) or set(failed) != expected or any(
            not isinstance(row.get("reason"), str) or not row["reason"].strip() for row in data["failures"]):
        raise ValueError("semantic_failure_assessment_mismatch")
    if any(set(event.source_cue_ids) - set(eligible_ids) for event in result.events):
        raise ValueError("semantic_event_uses_excluded_source")
    if any(set(note.source_cue_ids) - set(eligible_ids) for note in result.context_notes):
        raise ValueError("semantic_note_uses_excluded_source")
    return result
