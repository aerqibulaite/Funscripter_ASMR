"""Reuse immutable acoustic bundles; always rebuild subtitle-dependent summaries."""
import hashlib
import json
import math
import shutil
import zipfile
from pathlib import Path

import numpy as np

from .media import sha256


def summarize_cues(matrix, hop, rate, count, timeline):
    summaries = []
    if timeline:
        hop_ms = hop * 1000 / rate
        for cue in timeline.cues:
            start, end = max(0, cue.start_ms), max(0, min(count * 1000 / rate, cue.end_ms or 0))
            first = max(0, math.ceil(start / hop_ms))
            stop = min(len(matrix), math.ceil(end / hop_ms))
            values = matrix[first:stop, 2]
            summaries.append({"cue_id": cue.cue_id, "start_ms": start, "end_ms": end,
                "status": "available" if len(values) else "no_covered_frames",
                "mean_rms": float(np.mean(values)) if len(values) else None,
                "peak_rms": float(np.max(values)) if len(values) else None})
    return summaries


def reuse_acoustics(source, output, profile, input_hash, media, timeline):
    from .audio import acoustic_signature
    source, output = Path(source).resolve(), Path(output)
    raw_manifest = (source / "analysis_manifest.json").read_bytes()
    manifest = json.loads(raw_manifest)
    if not isinstance(manifest, dict):
        raise ValueError("analysis cache manifest must be an object")
    report = {"source_analysis": str(source), "source_analysis_id": manifest.get("analysis_id"),
              "source_manifest_sha256": hashlib.sha256(raw_manifest).hexdigest()}
    artifacts = manifest.get("artifacts")
    required = {"audio_features.npz", "audio_evidence.json", "audio_summary.csv"}
    if not isinstance(artifacts, dict) or not required.issubset(artifacts):
        raise ValueError("analysis cache is missing required artifact hashes")
    for relative, expected in artifacts.items():
        path = (source / relative).resolve()
        if not path.is_relative_to(source) or sha256(path) != expected:
            raise ValueError("analysis cache artifact changed or escapes source: " + relative)
    evidence_bytes = (source / "audio_evidence.json").read_bytes()
    if hashlib.sha256(evidence_bytes).hexdigest() != artifacts["audio_evidence.json"]:
        raise ValueError("analysis cache evidence changed while reading")
    evidence = json.loads(evidence_bytes)
    if not isinstance(evidence, dict):
        raise ValueError("analysis cache evidence must be an object")
    fields = ("input_sha256", "config_sha256", "implementation_sha256", "numpy_version")
    if any(not isinstance(evidence.get(key), str) for key in fields):
        raise ValueError("analysis cache has invalid identity fields")
    key = hashlib.sha256("".join(evidence[field] for field in fields).encode()).hexdigest()
    config_hash = hashlib.sha256(json.dumps(evidence.get("profile"), sort_keys=True).encode()).hexdigest()
    if (manifest.get("schema_version") != "1.3" or evidence.get("schema_version") != "1.3"
            or manifest.get("acoustic_cache_key") != key or evidence.get("cache_key") != key
            or evidence["config_sha256"] != config_hash):
        raise ValueError("analysis cache identity is inconsistent")
    expected = acoustic_signature(profile, input_hash)
    reasons = [reason for field, reason in zip(fields, (
        "analysis_audio_changed", "analysis_config_changed", "extractor_implementation_changed", "numpy_version_changed"))
        if evidence[field] != expected[field]]
    if reasons:
        return None, {**report, "status": "miss", "reasons": reasons}
    for field in ("sample_rate", "sample_count", "channels"):
        if evidence.get(field) != media[field]:
            raise ValueError("analysis cache media metadata is inconsistent: " + field)
    for field in ("frame_samples", "hop_samples", "frame_count"):
        if type(evidence.get(field)) is not int or evidence[field] < 1:
            raise ValueError("analysis cache has invalid frame metadata")
    if (evidence["frame_samples"] != max(1, round(profile.frame_ms * media["sample_rate"] / 1000))
            or evidence["hop_samples"] != max(1, round(profile.hop_ms * media["sample_rate"] / 1000))):
        raise ValueError("analysis cache frame configuration is inconsistent")
    if evidence.get("track_id") != "main" or evidence.get("source_start_ms") != 0:
        raise ValueError("analysis cache time origin or track is unsupported")
    for field in ("onsets", "periodicity", "quality_flags"):
        if not isinstance(evidence.get(field), list):
            raise ValueError("analysis cache missing acoustic evidence: " + field)
    destination = output / "audio_features.npz"
    shutil.copyfile(source / "audio_features.npz", destination)
    if sha256(destination) != artifacts["audio_features.npz"]:
        raise ValueError("analysis cache features changed while copying")
    try:
        with np.load(destination, allow_pickle=False) as archive:
            matrix, columns = archive["features"], archive["columns"].tolist()
            rate, hop, count = media["sample_rate"], evidence["hop_samples"], media["sample_count"]
            expected_columns = ["start_sample", "valid_samples", "rms", "rms_dbfs", "relative_db",
                *[f"{name}_rms" for name in evidence["profile"]["bands_hz"]],
                *[f"channel_{i}_rms" for i in range(media["channels"])], "positive_rms_delta"]
            if (matrix.shape != (math.ceil(count / hop), len(expected_columns))
                    or evidence["frame_count"] != len(matrix) or matrix.dtype != np.dtype("float64")
                    or columns != expected_columns or archive["sample_rate"].item() != rate
                    or archive["hop_samples"].item() != hop):
                raise ValueError("analysis cache feature layout is inconsistent")
            for first in range(0, len(matrix), 65536):
                rows = matrix[first:first + 65536]
                starts = np.arange(first, first + len(rows)) * hop
                if (not np.all(np.isfinite(rows)) or not np.array_equal(rows[:, 0], starts)
                        or not np.array_equal(rows[:, 1], np.minimum(evidence["frame_samples"], count - starts))):
                    raise ValueError("analysis cache feature values/time grid are invalid")
            evidence["cue_summaries"] = summarize_cues(matrix, hop, rate, count, timeline)
    except (KeyError, TypeError, OSError, EOFError, zipfile.BadZipFile) as exc:
        raise ValueError("analysis cache feature archive is invalid") from exc
    return evidence, {**report, "status": "hit", "reasons": []}
