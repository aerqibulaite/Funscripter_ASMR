"""Versioned semantic evidence, coverage, and stable identity checks."""
import hashlib
import json
from .models import SemanticBatchV2

CORE_FIELDS = ["cue_id", "intent", "scope", "anchor", "pace_tier", "amplitude_tier", "motion_style", "evidence"]
V2_FIELDS = ["source_cue_ids", "source_evidence", "statement_kind"]


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
        if note.cue_id not in targets or assessments[note.cue_id].status not in ("processed", "ambiguous"):
            raise ValueError("semantic_note_anchor_or_assessment_invalid")
        sources = note.source_cue_ids
        if len(sources) != len(set(sources)) or note.cue_id not in sources or set(sources) - set(lookup):
            raise ValueError("semantic_note_source_ids_invalid")
        quoted = set()
        for evidence in note.source_evidence:
            if evidence.cue_id not in sources or not evidence.quote.strip() or evidence.quote not in lookup[evidence.cue_id].text_for_analysis:
                raise ValueError("semantic_note_evidence_not_in_source")
            quoted.add(evidence.cue_id)
        if quoted != set(sources):
            raise ValueError("semantic_note_evidence_coverage_missing")
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
                raise ValueError("semantic_source_evidence_not_in_source_cue")
            quoted.add(evidence.cue_id)
        if quoted != set(sources):
            raise ValueError("semantic_source_evidence_coverage_missing")
        explicit = any(getattr(event, field) != "unknown" for field in
                       ("intent", "pace_tier", "amplitude_tier", "motion_style"))
        if (explicit and not event.evidence.strip()) or (event.evidence and event.evidence not in targets[event.cue_id].text_for_analysis):
            raise ValueError("semantic_evidence_not_in_source_cue")
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
            raise ValueError("semantic_processed_assessment_without_event")
    indices = {cue_id: index for index, cue_id in enumerate(targets)}
    result.events.sort(key=lambda event: indices[event.cue_id])


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
