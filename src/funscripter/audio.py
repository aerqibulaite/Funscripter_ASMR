"""Deterministic acoustic evidence; no sound labels or motion decisions."""
import hashlib
import json
import math
import wave
from pathlib import Path
import numpy as np

ANALYZER_VERSION = "basic-acoustic-1"


def pcm_values(raw, width, channels):
    if width == 1:
        values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) / 128
    elif width in (2, 4):
        values = np.frombuffer(raw, dtype="<i2" if width == 2 else "<i4").astype(np.float64)
        values /= 2 ** (width * 8 - 1)
    elif width == 3:
        octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        values = ((values ^ 0x800000) - 0x800000).astype(np.float64) / 8388608
    else:
        raise ValueError("unsupported PCM sample width")
    return values.reshape(-1, channels)


def frames(stream, frame_size, hop, block_samples):
    channels = stream.getnchannels()
    buffer = np.empty((0, channels))
    start = 0
    while raw := stream.readframes(block_samples):
        buffer = np.concatenate((buffer, pcm_values(raw, stream.getsampwidth(), channels)))
        offset = 0
        while len(buffer) - offset >= frame_size:
            yield start + offset, buffer[offset:offset + frame_size]
            offset += hop
        buffer = buffer[offset:]
        start += offset
    offset = 0
    while offset < len(buffer):
        yield start + offset, buffer[offset:offset + frame_size]
        offset += hop


def periodic_candidates(values, hop_ms, config, energy_floor):
    if len(values) < 3 * math.ceil(config.max_period_ms / hop_ms):
        return [], "insufficient_duration"
    if float(np.sqrt(np.mean(values ** 2))) <= energy_floor:
        return [], "low_energy"
    mean = float(np.mean(values))
    if float(np.std(values)) / max(mean, 1e-12) < config.min_modulation_ratio:
        return [], "weak_modulation"
    centered = values - mean
    minimum = max(1, math.ceil(config.min_period_ms / hop_ms))
    maximum = min(len(values) // 3, math.floor(config.max_period_ms / hop_ms))
    scores = {}
    for lag in range(max(1, minimum - 1), maximum + 2):
        left, right = centered[:-lag], centered[lag:]
        norm = np.linalg.norm(left) * np.linalg.norm(right)
        scores[lag] = float(np.dot(left, right) / norm) if norm > 1e-15 else 0.0
    peaks = [(lag, score) for lag, score in scores.items()
             if minimum <= lag <= maximum and score >= config.min_periodicity_score
             and score >= scores.get(lag - 1, -1) and score >= scores.get(lag + 1, -1)]
    peaks.sort(key=lambda pair: (-pair[1], pair[0]))
    return [{"period_ms": round(lag * hop_ms, 6), "score": round(score, 6)}
            for lag, score in peaks[:3]], "candidates" if peaks else "no_stable_period"


def extract_audio(path, output, config, input_hash, timeline=None):
    output = Path(output)
    # Caller owns a staging directory; only the successful analysis is published.
    profile = config.model_dump(mode="json")
    config_hash = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    implementation_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    cache_key = hashlib.sha256((input_hash + config_hash + implementation_hash + np.__version__).encode()).hexdigest()
    matrix = None
    temporary = output / "features-working.npy"
    try:
        with wave.open(str(path), "rb") as stream:
            rate, count, channels = stream.getframerate(), stream.getnframes(), stream.getnchannels()
            frame_size = max(1, round(config.frame_ms * rate / 1000))
            hop = max(1, round(config.hop_ms * rate / 1000))
            if frame_size > config.block_samples:
                raise ValueError("block_samples must cover one analysis frame")
            if count < 1:
                raise ValueError("empty audio")
            frequencies = np.fft.rfftfreq(frame_size, d=1 / rate)
            masks = {}
            for name, (low, high) in config.bands_hz.items():
                if high > rate / 2:
                    raise ValueError(f"band {name} exceeds Nyquist frequency; adjust bands_hz")
                masks[name] = (frequencies >= low) & (frequencies < high)
                if not np.any(masks[name]):
                    raise ValueError(f"band {name} has no FFT bins; increase frame_ms or adjust bands_hz")
            weights = np.full(len(frequencies), 2.0)
            weights[0] = 1.0
            if frame_size % 2 == 0:
                weights[-1] = 1.0
            columns = ["start_sample", "valid_samples", "rms", "rms_dbfs", "relative_db", *[f"{name}_rms" for name in masks],
                       *[f"channel_{i}_rms" for i in range(channels)], "positive_rms_delta"]
            temporary = output / "features-working.npy"
            matrix = np.lib.format.open_memmap(temporary, mode="w+", dtype="float64",
                shape=(math.ceil(count / hop), len(columns)))
            previous_rms = 0.0
            for index, (start, samples) in enumerate(frames(stream, frame_size, hop, config.block_samples)):
                channel_rms = np.sqrt(np.mean(samples ** 2, axis=0))
                rms = float(np.sqrt(np.mean(channel_rms ** 2)))
                spectrum = np.fft.rfft(samples, n=frame_size, axis=0)
                power = np.mean(np.abs(spectrum) ** 2, axis=1) * weights / (frame_size * len(samples))
                bands = [float(np.sqrt(np.sum(power[mask]))) for mask in masks.values()]
                matrix[index] = [start, len(samples), rms, 0.0, 0.0, *bands, *channel_rms,
                                 max(0.0, rms - previous_rms)]
                previous_rms = rms
            matrix.flush()
        floor = 10 ** (config.silence_dbfs / 20)
        peak = float(np.max(matrix[:, 2]))
        for first in range(0, len(matrix), 65536):
            stop = min(len(matrix), first + 65536)
            safe = np.maximum(matrix[first:stop, 2], 1e-12)
            matrix[first:stop, 3] = 20 * np.log10(safe)
            matrix[first:stop, 4] = 20 * np.log10(safe / max(peak, 1e-12))
        onsets = []
        threshold = max(floor, peak * config.onset_relative_threshold)
        last_sample = -rate
        for row in matrix:
            start = int(row[0])
            if row[-1] >= threshold and row[2] > floor and start - last_sample >= rate / 10:
                onsets.append({"evidence_id": f"onset_{start}", "time_ms": start * 1000 // rate,
                               "sample_index": start, "support_end_ms": min(count, start + int(row[1])) * 1000 // rate,
                               "strength": float(row[-1]),
                               "method": "positive_rms_delta", "needs_review": True})
                last_sample = start
        hop_ms = hop * 1000 / rate
        window_frames = max(1, round(config.periodicity_window_ms / hop_ms))
        step_frames = max(1, round(config.periodicity_step_ms / hop_ms))
        periodicity = []
        # Only emit one incomplete trailing window, not repeated copies of the same tail.
        for first in range(0, len(matrix), step_frames):
            stop = min(len(matrix), first + window_frames)
            features = {}
            for feature in ["rms", *[f"{name}_rms" for name in masks]]:
                values = matrix[first:stop, columns.index(feature)]
                candidates, status = periodic_candidates(values, hop_ms, config, floor)
                features[feature] = {"status": status, "candidates": candidates}
            periodicity.append({"evidence_id": f"period_{int(matrix[first, 0])}",
                "start_ms": int(matrix[first, 0]) * 1000 // rate,
                "end_ms": min(count, int(matrix[stop - 1, 0]) + hop) * 1000 // rate,
                "features": features, "needs_review": True})
            if stop == len(matrix):
                break
        summaries = []
        if timeline:
            for cue in timeline.cues:
                start, end = max(0, cue.start_ms), min(count * 1000 / rate, cue.end_ms or 0)
                # Select frame starts on the same global grid; frames can extend across cue boundaries.
                first = max(0, math.ceil(start / hop_ms))
                stop = min(len(matrix), math.ceil(end / hop_ms))
                values = matrix[first:stop, 2]
                summaries.append({"cue_id": cue.cue_id, "start_ms": start, "end_ms": end,
                    "status": "available" if len(values) else "no_covered_frames",
                    "mean_rms": float(np.mean(values)) if len(values) else None,
                    "peak_rms": float(np.max(values)) if len(values) else None})
        np.savez_compressed(output / "audio_features.npz", features=matrix,
                            columns=np.asarray(columns), sample_rate=np.asarray(rate), hop_samples=np.asarray(hop))
        frame_count = len(matrix)
        return {"schema_version": "1.3", "extractor": ANALYZER_VERSION, "track_id": "main",
            "input_sha256": input_hash, "config_sha256": config_hash, "cache_key": cache_key,
            "numpy_version": np.__version__, "implementation_sha256": implementation_hash, "profile": profile,
            "sample_rate": rate, "sample_count": count, "channels": channels,
            "frame_count": frame_count, "frame_samples": frame_size, "hop_samples": hop,
            "source_start_ms": 0, "frame_time_reference": "left_edge_no_centering",
            "frame_support": "[start_sample, min(start_sample + frame_samples, sample_count))",
            "tail_policy": "zero_pad_fft_normalize_by_valid_samples",
            "channel_fusion": "sqrt(mean(channel_rms_squared)); no waveform mixing",
            "band_method": "rectangular_frame_rfft_parseval; half-open frequency bins",
            "amplitude_unit": "normalized_digital_full_scale_rms",
            "db_reference": "dBFS = 20*log10(max(rms, 1e-12)); not sound pressure level",
            "relative_db_reference_rms": peak, "normalization_scope": "whole_track_peak_rms",
            "relative_db_formula": "20*log10(max(rms,1e-12)/max(reference_rms,1e-12))",
            "quality_flags": ["thresholds_not_calibrated_on_real_material", "frame_support_crosses_cue_boundaries",
                              "fft_band_leakage", "last_frames_partial", "period_scores_not_probabilities"],
            "optional_components": {"vad": "disabled", "panns": "disabled"},
            "onsets": onsets, "periodicity": periodicity, "cue_summaries": summaries,
            "status": "acoustic_candidates_need_review"}

    finally:
        if matrix is not None:
            matrix._mmap.close()
        temporary.unlink(missing_ok=True)
