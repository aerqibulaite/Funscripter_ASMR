"""Review-only utterance-end candidates from saved band envelopes and optional VAD evidence."""
import csv
import hashlib
import json
import math
import uuid
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Literal

import numpy as np
import yaml
from pydantic import Field, model_validator

from .models import Model, TimeRange
from .media import sha256
from .project import atomic_json, reopen_project
from .fuse_project import read_json, verify_bundle, verify_source
from .revision_workflow import stage_output, write_manifest


class BoundaryProfile(Model):
    bands: list[str] = Field(default_factory=lambda: ["mid"], min_length=1)
    energy_floor_dbfs: float = Field(default=-70.0, ge=-120, le=-10)
    relative_low_ratio: float = Field(default=0.2, gt=0, lt=1)
    min_active_ms: int = Field(default=120, ge=1)
    min_quiet_ms: int = Field(default=250, ge=1)
    min_delay_ms: int = Field(default=120, ge=0)
    merge_window_ms: int = Field(default=80, ge=0)

    @model_validator(mode="after")
    def unique_bands(self):
        if len(set(self.bands)) != len(self.bands) or any(not name.isidentifier() for name in self.bands):
            raise ValueError("boundary bands must be unique feature names")
        return self


class EndCandidate(Model):
    time_ms: int = Field(ge=0)
    method: Literal["empty_timestamp_hint", "subtitle_display_end", "band_energy_drop", "vad_end", "manual_profile", "manual_confirmation"]
    evidence_id: str = Field(min_length=1)
    status: Literal["unaccepted", "inferred_draft_boundary", "human_boundary"] = "unaccepted"
    reason: str | None = None
    score: float | None = Field(default=None, ge=0, le=1)
    score_kind: Literal["uncalibrated_rank"] = "uncalibrated_rank"
    support: TimeRange | None = None
    quality_flags: list[str] = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)


class CueCandidates(Model):
    cue_id: str
    utterance_end_candidates: list[EndCandidate]
    accepted_candidate: EndCandidate | None = None
    search_range: TimeRange | None = None
    audio_candidate_groups: list[list[str]] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)


class CandidateDocument(Model):
    schema_version: Literal["1.3"] = "1.3"
    cues: list[CueCandidates]
    components: dict[str, str] = Field(default_factory=dict)


class VadSpan(TimeRange):
    evidence_id: str = Field(min_length=1)


class VadEvidence(Model):
    schema_version: Literal["1"] = "1"
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_start_ms: Literal[0] = 0
    status: Literal["available", "failed", "disabled"]
    extractor: str = Field(min_length=1)
    version: str = Field(min_length=1)
    model_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    spans: list[VadSpan] = Field(default_factory=list)

    @model_validator(mode="after")
    def provenance(self):
        if self.status == "available" and (not self.model_sha256 or not self.config_sha256):
            raise ValueError("available VAD evidence needs model and config hashes")
        if self.status != "available" and self.spans:
            raise ValueError("failed/disabled VAD cannot provide successful intervals")
        if len({span.evidence_id for span in self.spans}) != len(self.spans):
            raise ValueError("VAD evidence IDs must be unique")
        return self


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def validate_document(document, timeline, duration):
    lookup = {cue.cue_id: cue for cue in timeline.cues}
    if len(document.cues) != len(lookup) or {row.cue_id for row in document.cues} != set(lookup):
        raise ValueError("boundary cue coverage is incomplete or duplicated")
    for row in document.cues:
        cue = lookup[row.cue_id]
        for candidate in [*row.utterance_end_candidates, *([row.accepted_candidate] if row.accepted_candidate else [])]:
            if cue.end_ms is None or not max(0, cue.start_ms) < candidate.time_ms <= min(cue.end_ms, duration):
                raise ValueError("boundary candidate is outside its cue search span")


def load_features(directory, project, media, profile):
    manifest = read_json(directory / "analysis_manifest.json")
    verify_source(manifest, project, "summary_subtitle_sha256")
    verify_bundle(directory, manifest, "artifacts")
    if not {"audio_features.npz", "audio_evidence.json"}.issubset(manifest["artifacts"]):
        raise ValueError("boundary analysis needs hashed feature and evidence files")
    evidence = read_json(directory / "audio_evidence.json")
    verify_source(evidence, project, "subtitle_sha256")
    if evidence.get("input_sha256") != project.artifact_hashes["analysis_audio"] or any(
            evidence.get(key) != media[key] for key in ("sample_rate", "sample_count", "channels")):
        raise ValueError("boundary analysis does not match project audio")
    if (evidence.get("source_start_ms") != 0 or evidence.get("track_id") != "main"
            or evidence.get("frame_time_reference") != "left_edge_no_centering"):
        raise ValueError("boundary analysis uses an unsupported time reference")
    for key in ("hop_samples", "frame_samples", "frame_count"):
        if type(evidence.get(key)) is not int or evidence[key] < 1:
            raise ValueError("invalid analysis frame metadata")
    with np.load(directory / "audio_features.npz", allow_pickle=False) as archive:
        matrix, columns = archive["features"], archive["columns"].tolist()
        rate, hop, count = media["sample_rate"], evidence["hop_samples"], media["sample_count"]
        if (matrix.ndim != 2 or matrix.shape != (math.ceil(count / hop), len(columns))
                or len(matrix) != evidence["frame_count"] or matrix.dtype != np.dtype("float64")
                or columns[:2] != ["start_sample", "valid_samples"] or len(set(columns)) != len(columns)
                or archive["sample_rate"].item() != rate or archive["hop_samples"].item() != hop):
            raise ValueError("invalid boundary feature layout")
        if any(f"{band}_rms" not in columns for band in profile.bands):
            raise ValueError("requested boundary band is missing from analysis")
        for first in range(0, len(matrix), 65536):
            rows = matrix[first:first + 65536]
            starts = np.arange(first, first + len(rows)) * hop
            if (not np.all(np.isfinite(rows)) or not np.array_equal(rows[:, 0], starts)
                    or not np.array_equal(rows[:, 1], np.minimum(evidence["frame_samples"], count - starts))):
                raise ValueError("invalid boundary feature values/time grid")
            if any(np.any(rows[:, columns.index(f"{band}_rms")] < 0) for band in profile.bands):
                raise ValueError("negative RMS values in boundary features")
    return matrix, columns, evidence


def energy_candidates(matrix, columns, cue, end, profile, evidence):
    rate, hop, frame = evidence["sample_rate"], evidence["hop_samples"], evidence["frame_samples"]
    first = max(0, math.ceil(max(0, cue.start_ms) * rate / (1000 * hop)))
    stop = min(len(matrix), math.ceil(end * rate / (1000 * hop)))
    candidates, reasons = [], []
    if first >= stop:
        return candidates, ["no_covered_frames"]
    floor = 10 ** (profile.energy_floor_dbfs / 20)
    for band in profile.bands:
        values = matrix[first:stop, columns.index(f"{band}_rms")]
        peak = float(np.max(values))
        if peak <= floor:
            reasons.append("insufficient_band_activity:" + band)
            continue
        threshold = max(floor, peak * profile.relative_low_ratio)
        active = values >= threshold
        edges = [0, *np.flatnonzero(active[1:] != active[:-1]) + 1, len(active)]
        if active[-1]:
            reasons.append("activity_reaches_search_end:" + band)
        for index in range(1, len(edges) - 1):
            a, q, tail = edges[index - 1:index + 2]
            if active[q] or not active[a] or (q - a) * hop * 1000 < profile.min_active_ms * rate:
                continue
            sample = int(matrix[first + q, 0])
            quiet_end = min(evidence["sample_count"], math.floor(end * rate / 1000),
                int(matrix[first + tail, 0]) if tail < len(active)
                else int(matrix[first + tail - 1, 0] + matrix[first + tail - 1, 1]))
            time = sample * 1000 // rate
            if ((quiet_end - sample) * 1000 < profile.min_quiet_ms * rate
                    or time <= max(0, cue.start_ms) + profile.min_delay_ms or time > end):
                continue
            support_start = int(matrix[first + q - 1, 0]) * 1000 // rate
            support_end = min(end, math.ceil(min(evidence["sample_count"], sample + frame) * 1000 / rate))
            flags = ["band_energy_is_not_speech", "frame_resolution_uncertainty", "threshold_uncalibrated"]
            if np.any(active[tail:]):
                flags.append("later_activity_within_search_span")
            candidates.append(EndCandidate(time_ms=time, method="band_energy_drop",
                evidence_id="band_end_" + identity([evidence["input_sha256"], cue.cue_id, band, sample, profile.model_dump()]),
                score=max(0.0, min(1.0, 1 - float(np.mean(values[q:tail])) / max(peak, floor))),
                support=TimeRange(start_ms=support_start, end_ms=support_end), quality_flags=flags,
                evidence={"band": band, "sample_index": sample, "threshold_rms": threshold,
                    "peak_rms": peak, "quiet_duration_ms": (quiet_end - sample) * 1000 / rate,
                    "analysis_cache_key": evidence["cache_key"]}))
    return candidates, sorted(set(reasons))


def build_candidates(matrix, columns, timeline, duration, profile, evidence, base_document, vad=None):
    document = CandidateDocument.model_validate(deepcopy(base_document))
    validate_document(document, timeline, duration)
    if vad:
        if vad.input_sha256 != evidence["input_sha256"] or any(span.end_ms > duration for span in vad.spans):
            raise ValueError("VAD evidence audio/time mapping mismatch")
    lookup = {row.cue_id: row for row in document.cues}
    for cue in timeline.cues:
        row = lookup[cue.cue_id]
        start, end = max(0, cue.start_ms), min(duration, cue.end_ms or 0)
        # Rebuild machine candidates without changing manual confirmation or subtitle hints.
        row.utterance_end_candidates = [item for item in row.utterance_end_candidates
            if item.method not in ("band_energy_drop", "vad_end")]
        row.review_reasons, row.audio_candidate_groups = [], []
        if start >= end:
            row.search_range = None
            row.review_reasons = ["invalid_or_outside_search_span"]
            continue
        row.search_range = TimeRange(start_ms=start, end_ms=end)
        machine, reasons = energy_candidates(matrix, columns, cue, end, profile, evidence)
        if timeline.issues:
            reasons.append("subtitle_time_anomalies")
        if sum(other.start_ms == cue.start_ms for other in timeline.cues) > 1:
            reasons.append("simultaneous_cue_starts")
        if vad and vad.status == "available":
            for span in vad.spans:
                if start + profile.min_delay_ms < span.end_ms <= end and span.end_ms - span.start_ms >= profile.min_active_ms:
                    flags = ["vad_end_is_not_sentence_end", "vad_thresholds_not_calibrated"]
                    if any(span.start_ms < other.end_ms and other.start_ms < span.end_ms
                           for other in vad.spans if other.evidence_id != span.evidence_id):
                        flags.append("overlapping_vad_intervals")
                    if any(span.end_ms < other.start_ms < end for other in vad.spans):
                        flags.append("later_activity_within_search_span")
                    if sum(max(0, other.start_ms) < span.end_ms and (other.end_ms or 0) > span.start_ms
                           for other in timeline.cues) > 1:
                        flags.append("vad_interval_overlaps_multiple_cues")
                    machine.append(EndCandidate(time_ms=span.end_ms, method="vad_end",
                        evidence_id="vad_end_" + identity([vad.input_sha256, vad.model_sha256, vad.config_sha256, span.evidence_id, cue.cue_id]),
                        support=TimeRange(start_ms=span.start_ms, end_ms=span.end_ms), quality_flags=flags,
                        evidence={"source_evidence_id": span.evidence_id, "extractor": vad.extractor, "version": vad.version}))
            if not vad.spans:
                reasons.append("vad_returned_no_intervals_not_proof_of_silence")
        elif vad:
            reasons.append("vad_" + vad.status)
        machine.sort(key=lambda item: (item.time_ms, item.evidence_id))
        group_start = None
        for candidate in machine:
            if group_start is None or candidate.time_ms - group_start > profile.merge_window_ms:
                row.audio_candidate_groups.append([])
                group_start = candidate.time_ms
            row.audio_candidate_groups[-1].append(candidate.evidence_id)
        reasons.append("multiple_audio_end_candidates" if len(row.audio_candidate_groups) > 1
                       else "audio_candidate_requires_review" if machine else "no_sustained_end_evidence")
        if any("later_activity_within_search_span" in item.quality_flags for item in machine):
            reasons.append("possible_internal_pause")
        row.utterance_end_candidates.extend(machine)
        row.review_reasons = sorted(set(reasons))
    document.components = {"band_energy": "available", "vad": vad.status if vad else "disabled"}
    validate_document(document, timeline, duration)
    return document


def create_boundary_candidates(directory, analysis, output, profile_path=None, vad_evidence=None):
    directory, analysis = Path(directory).resolve(), Path(analysis).resolve()
    project, timeline, media, _ = reopen_project(directory)
    profile = BoundaryProfile()
    if profile_path:
        try:
            raw = yaml.safe_load(Path(profile_path).read_text(encoding="utf-8-sig"))
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid boundary YAML: {exc}") from None
        profile = BoundaryProfile.model_validate_json(json.dumps(raw, allow_nan=False))
    try:
        matrix, columns, evidence = load_features(analysis, project, media, profile)
    except (KeyError, TypeError, EOFError, zipfile.BadZipFile) as exc:
        raise ValueError("invalid boundary analysis archive") from exc
    vad = VadEvidence.model_validate_json(Path(vad_evidence).read_text(encoding="utf-8-sig")) if vad_evidence else None
    document = build_candidates(matrix, columns, timeline, media["duration_ms"], profile, evidence,
        read_json(directory / project.artifacts["candidates"]), vad)
    sources = [directory, analysis, *([Path(profile_path).resolve()] if profile_path else []),
               *([Path(vad_evidence).resolve()] if vad_evidence else [])]
    with stage_output(output, sources) as stage:
        atomic_json(stage / "utterance_candidates.json", document.model_dump(mode="json"))
        atomic_json(stage / "profile.resolved.json", profile.model_dump(mode="json"))
        with (stage / "review.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["cue_id", "time_ms", "method", "evidence_id", "score_uncalibrated", "quality_flags", "review_reasons"])
            for row in document.cues:
                for item in row.utterance_end_candidates or [None]:
                    writer.writerow([row.cue_id, item.time_ms if item else "", item.method if item else "",
                        item.evidence_id if item else "", item.score if item else "",
                        "|".join(item.quality_flags) if item else "", "|".join(row.review_reasons)])
        return write_manifest(stage, "boundary_manifest.json", {"schema_version": "1.3", "status": "needs_review",
            "boundary_id": str(uuid.uuid4()),
            "project_id": project.project_id, "source_revision_id": project.revision_id,
            "subtitle_sha256": project.artifact_hashes["subtitles"], "analysis_audio_sha256": evidence["input_sha256"],
            "analysis_manifest_sha256": sha256(analysis / "analysis_manifest.json"),
            "feature_sha256": sha256(analysis / "audio_features.npz"), "vad_sha256": sha256(vad_evidence) if vad_evidence else None,
            "boundary_implementation_sha256": sha256(__file__), "components": document.components,
            "automatic_acceptance": False, "score_is_probability": False,
            "candidate_count": sum(len(row.utterance_end_candidates) for row in document.cues)})
