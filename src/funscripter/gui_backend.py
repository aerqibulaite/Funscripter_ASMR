"""Small desktop controller; all generation uses the existing task pipeline."""
import json
import uuid
from datetime import datetime
from pathlib import Path

import yaml

from .cli import execute_stage
from .llm import chat
from .models import Config, LLMConfig
from .project import reopen_project
from .tasks import FIELDS, run_task


def absolute(value):
    return str(Path(value.strip().strip('"')).expanduser().resolve())


def format_stage_failure(result):
    """Show an actionable reason, keeping the full manifest in its saved files."""
    output = Path(result["output"]) if result.get("output") else None
    reason, stage = None, None
    if output is not None:
        try:
            run = json.loads((output / "planning_run.json").read_text(encoding="utf-8"))
            reason = run.get("error")
            stage = run.get("global_input_strategy")
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            log = json.loads((output / "request_log.json").read_text(encoding="utf-8"))
            failed = next((row for row in reversed(log) if row.get("status") == "failed" and row.get("error")), None)
            if failed:
                reason = reason or failed["error"]
                stage = stage or failed.get("task")
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    reason = str(reason or result.get("error") or "阶段未完成，请查看保存的任务记录。")[:600]
    explanation = next((text for code, text in (
        ("outline_incomplete_coverage", "大纲遗漏了部分字幕，未覆盖完整范围。"),
        ("outline_evidence_outside_phase", "大纲的证据引用超出了所属章节。"),
        ("outline_gap_overlap_or_unknown_id", "大纲章节存在缺口、重叠或未知字幕编号。")) if code in reason), "")
    lines = ["任务已取消" if result.get("execution_status") == "cancelled" else "任务失败",
             "原因：" + explanation + reason]
    if stage:
        lines.append("阶段：" + str(stage)[:100])
    if output is not None:
        lines.extend(["输出目录：" + str(output), "请求记录：" + str(output / "request_log.json")])
    if result.get("task_file"):
        lines.append("任务记录：" + str(result["task_file"]))
    return "\n".join(lines)


def load_model(path):
    from .model_profiles import load_model_profile
    return load_model_profile(path)


def save_model(path, values):
    model = LLMConfig.model_validate_json(json.dumps(values))
    Path(path).write_text(yaml.safe_dump(model.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    return model


def probe_model(values, transport=chat):
    config = LLMConfig.model_validate_json(json.dumps(values))
    # A real, short inference verifies the configured adapter, not just an open port.
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
              "required": ["ok"], "additionalProperties": False}
    probe = config.model_copy(update={"max_output_tokens": 128, "timeout_seconds": min(config.timeout_seconds, 30.0)})
    content, metadata = transport(probe, [{"role": "user", "content": 'Return only this JSON object: {"ok": true}'}], schema)
    payload = json.loads(content)
    if payload != {"ok": True} or type(payload.get("ok")) is not bool:
        raise ValueError("模型已响应，但未返回预期的结构化 JSON")
    return {"status": "connected", "model_id": config.model_id, "metadata": metadata,
            "note": "连接及短 JSON 推理通过；不代表两遍规划已通过"}


class DesktopController:
    def __init__(self, workspace, executor=execute_stage):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.executor = executor

    def fresh(self, label):
        return self.workspace / (datetime.now().strftime("%Y%m%d-%H%M%S-") + label + "-" + uuid.uuid4().hex[:8])

    def create(self, audio, subtitles, ffmpeg, encoding, offset, ffprobe="ffprobe", audio_stream="0",
               source_mode="导入字幕", whisper_profile="", transcription_directory=""):
        transcription = None
        if source_mode in ("本地音频转写", "本地 Whisper 转写"):
            from .transcription import transcribe_audio
            if int(offset) != 0:
                raise ValueError("音频转写使用音频原时间轴，请将字幕偏移设为 0")
            if transcription_directory.strip():
                transcription = {"output": absolute(transcription_directory)}
            elif not whisper_profile.strip():
                raise ValueError("请选择语音模型配置 YAML")
            else:
                transcription = self.transcribe(audio, whisper_profile, ffmpeg, ffprobe, audio_stream)
            subtitles = str(Path(transcription["output"]) / "subtitles.srt")
            encoding = "utf-8"
        elif source_mode != "导入字幕":
            raise ValueError("unknown text input mode")
        values = {"audio": absolute(audio), "subtitles": {"path": absolute(subtitles),
                  "encoding": encoding, "offset_ms": int(offset)}, "ffmpeg": ffmpeg,
                  "ffprobe": ffprobe, "audio_stream": int(audio_stream)}
        if transcription:
            values["subtitles"].update(source_kind="whisper", transcription_manifest=str(
                Path(transcription["output"]) / "transcription_manifest.json"))
        config = Config.model_validate_json(json.dumps(values))
        for path in (config.audio, config.subtitles.path):
            if not Path(path).is_file():
                raise ValueError(f"输入文件不存在：{path}")
        config_path = self.fresh("input").with_suffix(".yaml")
        config_path.write_text(yaml.safe_dump(config.model_dump(mode="json"), allow_unicode=True), encoding="utf-8")
        result = self.stage("create", config=str(config_path))
        if transcription:
            result["transcription_output"] = transcription["output"]
        return result

    def transcribe(self, audio, profile, ffmpeg="ffmpeg", ffprobe="ffprobe", audio_stream="0"):
        from .transcription import transcribe_audio
        return transcribe_audio(absolute(audio), self.fresh("transcribe"), absolute(profile),
                                ffmpeg, ffprobe, int(audio_stream))

    def preview(self, project, draft, planning=None):
        from .interactive_preview import create_interactive_preview
        return create_interactive_preview(project, draft, self.fresh("preview"), planning)

    def stage(self, command, on_output=None, **values):
        if command not in {"create", "analyze", "plan", "utterance-candidates", "fuse", "diagnostic"}:
            raise ValueError("unsupported desktop stage")
        arguments = {key: None for key in FIELDS[command]}
        if command == "plan":
            arguments["resume"] = False
        if set(values) - set(arguments):
            raise ValueError("unknown stage argument")
        arguments.update(values)
        output = self.fresh(command)
        arguments["output"] = str(output)
        if on_output:
            on_output(str(output))
        result = run_task(command, arguments, self.executor)
        return {**result, "output": str(output)}

    @staticmethod
    def inspect(project):
        saved, timeline, media, validation = reopen_project(project)
        return {"project_id": saved.project_id, "duration_ms": media["duration_ms"],
                "text_input_kind": saved.config.subtitles.source_kind,
                "source_kind": media.get("source_kind", "audio"), "selected_audio_stream": media.get("selected_audio_stream", 0),
                "timeline_policy": media.get("timeline_policy", "decoded_audio_start_zero"),
                "cue_count": len(timeline.cues), "issues": timeline.issues, "validation": validation}
