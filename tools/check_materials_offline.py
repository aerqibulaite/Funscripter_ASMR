"""Validate real media inputs and semantic plumbing with NO model or semantic claims."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import yaml

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from funscripter.fuse_project import load_semantics
from funscripter.fusion import build_plan
from funscripter.lrc import candidate_document
from funscripter.media import sha256
from funscripter.models import FusionProfile
from funscripter.project import atomic_json, create_project, plan_project, reopen_project


def fixture(config, messages, schema):
    """All valid text is deliberately AMBIGUOUS, never interpreted as an action."""
    data = json.loads(messages[-1]["content"])
    if data["task"] == "semantic":
        result = {"schema_version": "1.4", "events": [], "cue_assessments": [
            {"cue_id": cue["cue_id"], "status": "ambiguous", "reason": "OFFLINE_PROTOCOL_FIXTURE: no model; meaning not evaluated"}
            for cue in data["target_cues"]]}
    elif data["task"] == "merge_outline":
        phases = [phase for partial in data["partial_outlines"] for phase in partial["phases"]]
        result = {"schema_version": "1.3", "phases": [{**phases[0],
            "end_cue_id_inclusive": phases[-1]["end_cue_id_inclusive"]}]}
    else:
        cues = data["cues"]
        result = {"schema_version": "1.3", "phases": [{"phase_id": "protocol_fixture",
            "start_cue_id": cues[0]["cue_id"], "end_cue_id_inclusive": cues[-1]["cue_id"],
            "summary": "OFFLINE_PROTOCOL_FIXTURE: coverage only, not a scene interpretation",
            "trend_hint": "unknown", "evidence_cue_ids": [cues[0]["cue_id"]], "needs_review": True}]}
    return json.dumps(result, ensure_ascii=False), {"mode": "OFFLINE_PROTOCOL_FIXTURE", "model_used": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", default=str(root / "workspace/tools/ffmpeg-binaries/ffmpeg.exe"))
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if output.exists() or output == source or output.is_relative_to(source):
        parser.error("use a new output directory outside source")
    subtitles = sorted(source.glob("*.srt"))
    if not subtitles:
        parser.error("source has no SRT files")
    output.mkdir(parents=True)
    files = sorted([*source.glob("*.srt"), *source.glob("*.mp3")])
    before = {str(path): sha256(path) for path in files}
    model_profile = output / "protocol-fixture.yaml"
    model_profile.write_text(yaml.safe_dump({"backend": "ollama", "base_url": "http://127.0.0.1:1",
        "model_id": "OFFLINE_PROTOCOL_FIXTURE", "max_retries": 0}), encoding="utf-8")
    summary = {"mode": "OFFLINE_PROTOCOL_FIXTURE", "model_used": False, "semantic_accuracy_evaluated": False,
               "hardware_used": False, "source": str(source), "tracks": []}
    profile = FusionProfile(preset_version="offline-hold-only-fixture", presets={}, tier_map={}, unknown_policy="hold_position")
    for index, subtitle in enumerate(subtitles):
        candidates = [path for path in source.glob("*.mp3") if path.name[:2] == subtitle.name[:2]]
        if len(candidates) != 1:
            raise ValueError("expected exactly one matching MP3 for " + subtitle.name)
        folder = output / f"track-{index+1:02d}"
        folder.mkdir()
        config = folder / "input.yaml"
        config.write_text(yaml.safe_dump({"audio": str(candidates[0]), "subtitles": {"path": str(subtitle)},
            "ffmpeg": str(Path(args.ffmpeg).resolve())}, allow_unicode=True), encoding="utf-8")
        print("Checking " + subtitle.name[:2] + "...", flush=True)
        create_project(config, folder / "project")
        project, timeline, media, _ = reopen_project(folder / "project")
        run = plan_project(folder / "project", folder / "planning", model_profile, transport=fixture)
        semantic, outline, _ = load_semantics(folder / "planning", project, timeline, media["duration_ms"])
        plan, _, _, validation = build_plan(project, timeline, media["duration_ms"], semantic, outline,
            None, candidate_document(timeline), profile)
        atomic_json(folder / "offline_plan_check.json", plan)
        input_view = json.loads((folder / "planning/planning_inputs.json").read_text(encoding="utf-8"))
        row = {"track": subtitle.name[:2], "duration_ms": media["duration_ms"], "subtitle_count": len(timeline.cues),
            "eligible_cue_count": run["eligible_cue_count"], "excluded_cue_count": run["excluded_cue_count"],
            "assessment_count": len(semantic["cue_assessments"]), "event_count": len(semantic["events"]),
            "assessment_statuses": dict(Counter(item["status"] for item in semantic["cue_assessments"])),
            "source_issues": dict(Counter(item["reason"] for item in timeline.issues)),
            "excluded": [item for item in input_view["cues"] if item["status"] == "excluded"],
            "quality_flag_counts": dict(Counter(flag for item in input_view["cues"] for flag in item["quality_flags"])),
            "planning_status": run["status"], "fusion_read_passed": True, "format_valid": validation["format_valid"],
            "motion_exported_by_this_check": False}
        assert row["assessment_count"] == row["subtitle_count"]
        assert row["event_count"] == 0 and all(segment["mode"] == "hold_position" for segment in plan["segments"])
        summary["tracks"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    after = {str(path): sha256(path) for path in files}
    if before != after:
        raise ValueError("source files changed during validation")
    summary["source_hashes_unchanged"] = True
    summary["source_hashes"] = before
    atomic_json(output / "report.json", summary)
    lines = ["# 无模型素材回归结果", "", "此运行只验证输入、覆盖、缓存契约和融合读取；全部有效台词由固定桩标记为 ambiguous。",
        "没有运行语言模型、解释台词、执行设备动作或导出用于评价真实效果的动作脚本。项目创建时保存的默认静止基线不代表语义结果。", "",
        "| 轨道 | 字幕 | 有效规划目标 | 隔离 | 处理记录 | 桩事件 | 融合读取 |",
        "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in summary["tracks"]:
        lines.append(f'| {row["track"]} | {row["subtitle_count"]} | {row["eligible_cue_count"]} | {row["excluded_cue_count"]} | {row["assessment_count"]} | {row["event_count"]} | 通过 |')
    lines.extend(["", "已确认：05 轨零时长条目保留复核记录并从模型目标隔离；全部原 cue 均有处理结果；无事件输出可被新融合路径读取。",
        "", "跨句证据、同句多事件、无新信息延续、歧义处置和旧版读取另由中性自动化测试验证。",
        "", "无法判断：拟声词或特殊语境的识别准确性、句内事件的真实时间、动作真实感、设备效果。",
        "", "原资料的 MP3/SRT SHA-256 在运行前后相同。完整数据见 report.json。", ""])
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
