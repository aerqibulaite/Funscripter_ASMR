"""Local-only Whisper transcription bundles, independent of semantic planning."""
import json
import math
import os
import shutil
import tempfile
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path

import yaml
from pydantic import Field

from .media import decode_audio, sha256
from .models import Model
from .project import atomic_json


_CUDA_DLL_HANDLES = {}


def configure_windows_cuda_libraries():
    """Expose optional NVIDIA wheels installed in this Python environment."""
    if os.name != "nt":
        return
    for package in ("nvidia-cublas-cu12", "nvidia-cudnn-cu12", "nvidia-cuda-nvrtc-cu12"):
        try:
            installed = distribution(package)
        except PackageNotFoundError:
            continue
        directories = {Path(installed.locate_file(name)).parent.resolve()
                       for name in installed.files or [] if str(name).lower().endswith(".dll")}
        for directory in sorted(directories):
            if not directory.is_dir() or directory in _CUDA_DLL_HANDLES:
                continue
            _CUDA_DLL_HANDLES[directory] = os.add_dll_directory(str(directory))
            # CTranslate2 also loads CUDA libraries through native LoadLibrary.
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")


class TranscriptionProfile(Model):
    model_path: str
    language: str | None = "ja"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = Field(default=5, ge=1, le=20)
    word_timestamps: bool = True
    vad_filter: bool = False
    condition_on_previous_text: bool = False
    initial_prompt: str | None = None


def load_profile(path):
    path = Path(path).resolve()
    profile = TranscriptionProfile.model_validate_json(json.dumps(
        yaml.safe_load(path.read_text(encoding="utf-8-sig")), allow_nan=False))
    return profile.model_copy(update={"model_path": str((path.parent / profile.model_path).resolve())})


def timestamp(ms):
    seconds, milliseconds = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def load_local_model(profile, model_factory=None):
    model_path = Path(profile.model_path)
    if not model_path.is_dir() or not all((model_path / name).is_file() for name in
                                         ("model.bin", "config.json", "tokenizer.json")):
        raise ValueError("需要本地 CTranslate2 模型目录（model.bin、config.json、tokenizer.json）；.pt 和 Transformers 权重需先转换")
    if model_factory is None:
        configure_windows_cuda_libraries()
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ValueError('未安装可选转写依赖，请在项目环境运行 python -m pip install -e ".[whisper]"') from exc
        model_factory = WhisperModel
    return model_factory(str(model_path), device=profile.device,
                         compute_type=profile.compute_type, local_files_only=True)


def probe_whisper(profile, model_factory=None):
    model = load_local_model(profile, model_factory)
    del model
    return {"status": "load_test_passed", "model_path": profile.model_path,
            "device": profile.device, "compute_type": profile.compute_type,
            "note": "加载测试通过；未运行语音识别。转写时重新加载模型。"}


def milliseconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("nonfinite or invalid ASR timestamp")
    return round(value * 1000)


def transcribe_audio(audio, output, profile_path, ffmpeg="ffmpeg", ffprobe="ffprobe",
                     audio_stream=0, model_factory=None):
    source, output = Path(audio).resolve(), Path(output).resolve()
    profile = load_profile(profile_path)
    model_path = Path(profile.model_path)
    if output.exists():
        raise ValueError("transcription output already exists; choose a new directory")
    if not source.is_file():
        raise ValueError("audio input does not exist")
    if not model_path.is_dir() or not all((model_path / name).is_file() for name in
                                         ("model.bin", "config.json", "tokenizer.json")):
        raise ValueError("需要本地 CTranslate2 模型目录（model.bin、config.json、tokenizer.json）；.pt 和 Transformers 权重需先转换")
    if output.is_relative_to(model_path):
        raise ValueError("transcription output must be outside the model directory")
    source_hash = sha256(source)
    model_hashes = {str(path.relative_to(model_path)): sha256(path)
                    for path in sorted(model_path.rglob("*")) if path.is_file()}
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".funscripter-transcribe-", dir=output.parent))
    try:
        media = decode_audio(source, stage / "analysis.wav", ffmpeg, ffprobe, audio_stream)
        duration = media["duration_ms"]
        model = load_local_model(profile, model_factory)
        segments, info = model.transcribe(str(stage / "analysis.wav"), task="transcribe",
            language=profile.language, beam_size=profile.beam_size,
            word_timestamps=profile.word_timestamps, vad_filter=profile.vad_filter,
            condition_on_previous_text=profile.condition_on_previous_text,
            initial_prompt=profile.initial_prompt)
        rows, srt, issues = [], [], []
        previous_start = -1
        for index, segment in enumerate(segments, 1):
            start, end = milliseconds(segment.start), milliseconds(segment.end)
            if not 0 <= start < end <= duration or start < previous_start:
                raise ValueError(f"invalid ASR segment interval: {index}")
            previous_start = start
            raw_text = segment.text
            words = [{"start_ms": milliseconds(word.start), "end_ms": milliseconds(word.end),
                      "text": word.word, "probability": word.probability}
                     for word in (segment.words or [])]
            if any(not start <= word["start_ms"] <= word["end_ms"] <= end for word in words):
                raise ValueError(f"ASR word timestamp outside segment: {index}")
            rows.append({"segment_id": index, "start_ms": start, "end_ms": end,
                         "text": raw_text, "words": words,
                         "avg_logprob": segment.avg_logprob, "no_speech_prob": segment.no_speech_prob})
            if not raw_text.strip():
                issues.append({"segment_id": index, "reason": "empty_recognition_segment"})
                continue
            if len(rows) > 1 and start < rows[-2]["end_ms"]:
                issues.append({"segment_id": index, "reason": "overlapping_asr_segments"})
            # Retain exact recognition in JSON; prevent blank lines splitting SRT blocks.
            text = " ".join(raw_text.splitlines()).strip()
            srt.append(f"{len(srt)+1}\n{timestamp(start)} --> {timestamp(end)}\n{text}\n")
        if not srt:
            raise ValueError("Whisper 未识别到非空文本；未创建项目")
        if sha256(source) != source_hash or any(sha256(model_path / name) != value for name, value in model_hashes.items()):
            raise ValueError("source audio or model changed during transcription")
        atomic_json(stage / "transcript.json", {"schema_version": "1", "language": info.language,
            "segments": rows, "issues": issues, "timing_status": "asr_estimated_not_human_confirmed"})
        (stage / "subtitles.srt").write_text("\n".join(srt), encoding="utf-8")
        atomic_json(stage / "media_manifest.json", media)
        try:
            runtime_version = version("faster-whisper")
        except PackageNotFoundError:
            runtime_version = "test_factory" if model_factory else "unknown"
        manifest = {"schema_version": "1", "input_kind": "whisper", "status": "needs_review",
            "source_sha256": source_hash, "audio_stream": audio_stream,
            "profile": profile.model_dump(mode="json"), "backend": "faster_whisper",
            "backend_version": runtime_version, "model_hashes": model_hashes,
            "language": info.language, "duration_ms": duration, "segment_count": len(srt),
            "artifact_hashes": {name: sha256(stage / name) for name in
                ("transcript.json", "subtitles.srt", "analysis.wav", "media_manifest.json")}}
        atomic_json(stage / "transcription_manifest.json", manifest)
        os.replace(stage, output)
        return {**manifest, "output": str(output)}
    except BaseException:
        shutil.rmtree(stage)
        raise


def verify_transcription(manifest_path, audio, subtitles, audio_stream):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    names = {"transcript.json", "subtitles.srt", "analysis.wav", "media_manifest.json"}
    if manifest.get("schema_version") != "1" or manifest.get("input_kind") != "whisper" or set(manifest.get("artifact_hashes", {})) != names:
        raise ValueError("invalid transcription manifest")
    for name, expected in manifest["artifact_hashes"].items():
        if sha256(manifest_path.parent / name) != expected:
            raise ValueError("transcription artifact changed: " + name)
    if (sha256(audio) != manifest["source_sha256"] or audio_stream != manifest["audio_stream"]
            or sha256(subtitles) != manifest["artifact_hashes"]["subtitles.srt"]):
        raise ValueError("transcription source/subtitle/audio stream mismatch")
    return manifest
