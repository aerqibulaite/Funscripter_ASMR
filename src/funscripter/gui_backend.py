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

    def create(self, audio, subtitles, ffmpeg, encoding, offset, ffprobe="ffprobe", audio_stream="0"):
        values = {"audio": absolute(audio), "subtitles": {"path": absolute(subtitles),
                  "encoding": encoding, "offset_ms": int(offset)}, "ffmpeg": ffmpeg,
                  "ffprobe": ffprobe, "audio_stream": int(audio_stream)}
        config = Config.model_validate_json(json.dumps(values))
        for path in (config.audio, config.subtitles.path):
            if not Path(path).is_file():
                raise ValueError(f"输入文件不存在：{path}")
        config_path = self.fresh("input").with_suffix(".yaml")
        config_path.write_text(yaml.safe_dump(config.model_dump(mode="json"), allow_unicode=True), encoding="utf-8")
        return self.stage("create", config=str(config_path))

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
                "source_kind": media.get("source_kind", "audio"), "selected_audio_stream": media.get("selected_audio_stream", 0),
                "timeline_policy": media.get("timeline_policy", "decoded_audio_start_zero"),
                "cue_count": len(timeline.cues), "issues": timeline.issues, "validation": validation}
