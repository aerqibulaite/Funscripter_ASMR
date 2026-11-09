"""Offline M3 demo with synthetic audio and SCRIPTED semantics, not a model evaluation."""
import argparse
import json
import sys
import wave
from pathlib import Path
import numpy as np
import yaml
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from funscripter.project import create_project, analyze_project, plan_project
from funscripter.fuse_project import fuse_project


def scripted_response(config, messages, schema):
    data = json.loads(messages[-1]["content"])
    if data["task"] != "semantic":
        cues = data["cues"]
        result = {"schema_version": "1.3", "phases": [{"phase_id": "demo",
            "start_cue_id": cues[0]["cue_id"], "end_cue_id_inclusive": cues[-1]["cue_id"],
            "summary": "脚本化演示，不代表模型实际判断", "trend_hint": "unknown",
            "evidence_cue_ids": [cues[0]["cue_id"]], "needs_review": True}]}
    else:
        events = []
        for cue in data["target_cues"]:
            text = cue["text"]
            event = {"cue_id": cue["cue_id"], "intent": "maintain", "scope": "current_section", "anchor": "cue_start",
                "pace_tier": "unknown", "amplitude_tier": "unknown", "motion_style": "unknown",
                "evidence": text, "needs_review": True, "reason": "脚本化中性测试，不是模型响应"}
            if text.startswith("采用"):
                event.update(pace_tier="slow" if "慢速" in text else "medium", amplitude_tier="small", motion_style="rhythmic")
            elif text == "提高一档":
                event["intent"] = "increase_rate"
            elif text == "暂停":
                event["intent"] = "pause"
            elif text == "恢复":
                event["intent"] = "resume"
            elif text == "这一句没有控制提示":
                event.update(intent="unknown", evidence="")
            events.append(event)
        result = {"schema_version": "1.3", "events": events}
    return json.dumps(result, ensure_ascii=False), {"model": "SCRIPTED_DEMO_ONLY"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(root / "workspace/m3-demo"))
    target = Path(parser.parse_args().output).resolve()
    if target.exists():
        parser.error("output already exists; choose a new directory")
    inputs = target / "inputs"
    inputs.mkdir(parents=True)
    rate = 22050
    with wave.open(str(inputs / "track.wav"), "wb") as stream:
        stream.setparams((2, 2, rate, 0, "NONE", "not compressed"))
        for second in range(16):
            time = second + np.arange(rate) / rate
            envelope = 0.25 + 0.2*np.sin(2*np.pi*time/0.5)
            signal = (8000*envelope*np.sin(2*np.pi*700*time)).astype("<i2")
            stream.writeframes(np.column_stack([signal,-signal]).astype("<i2").tobytes())
    lines = ["采用慢速小幅节奏", "保持当前设置", "提高一档", "暂停", "恢复", "这一句没有控制提示", "采用中速小幅节奏", "保持当前设置"]
    (inputs / "track.lrc").write_text("\n".join(f"[00:{i*2:02d}.000]{text}" for i,text in enumerate(lines)), encoding="utf-8")
    (inputs / "config.yaml").write_text(yaml.safe_dump({"audio":"track.wav", "subtitles":{"path":"track.lrc"}}), encoding="utf-8")
    (inputs / "llm.yaml").write_text(yaml.safe_dump({"backend":"llama_cpp", "base_url":"http://127.0.0.1:1",
        "model_id":"SCRIPTED_DEMO_ONLY", "max_retries":0}), encoding="utf-8")
    create_project(inputs / "config.yaml", target / "project")
    analyze_project(target / "project", target / "analysis")
    plan_project(target / "project", target / "planning", inputs / "llm.yaml", transport=scripted_response)
    manifest = fuse_project(target / "project", target / "planning", target / "fusion", root / "config/fusion-example.yaml", target / "analysis")
    print(json.dumps({"demo_mode":"SCRIPTED_NOT_REAL_MODEL", "preview":str(target / "fusion/preview.png"),
                      "script_exported":manifest["script_exported"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
