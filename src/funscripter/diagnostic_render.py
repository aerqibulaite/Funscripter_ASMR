"""Pillow overlays keep subtitle text out of FFmpeg filter/command syntax."""
import bisect
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

REASONS = {
    "unknown_semantics": "语义不明确", "uncertain_anchor": "生效时间待确认",
    "uncertain_scope": "作用范围待确认", "inferred_utterance_boundary": "使用推断句尾",
    "model_requested_review": "模型标记需复核", "unresolved_state_hold_position": "状态未知，保持位置",
    "audio_evidence_missing": "缺少音频证据", "explicit_pause_or_hold": "暂停或保持",
    "audio_preset_period_difference_review": "音频周期与预设不同", "manual_segment_priority": "采用人工段落",
    "audio_period_candidate_during_hold": "保持期间存在音频周期候选", "section_initial_state": "章节初态",
    "simultaneous_events_need_review": "同时刻事件冲突", "subtitle_input_issues": "字幕输入存在异常",
    "no_subtitle_coverage": "此处无字幕覆盖"}


def wrap(draw, text, font, width, max_lines):
    lines, current = [], ""
    for char in text.replace("\r", "").replace("\t", "    "):
        if char == "\n" or (current and draw.textlength(current + char, font=font) > width):
            lines.append(current)
            current = "" if char == "\n" else char
        else:
            current += char
    if current or not lines:
        lines.append(current)
    truncated = len(lines) > max_lines
    lines = lines[:max_lines]
    if truncated:
        lines[-1] = lines[-1][:-1] + "…"
    return lines, truncated


class DiagnosticRenderer:
    def __init__(self, timeline, plan, preview_path, profile, revision_id):
        path = Path(profile.font_path)
        if not path.is_file():
            raise ValueError("diagnostic font_path does not exist; select a Chinese-capable font")
        self.fonts = {size: ImageFont.truetype(str(path), size) for size in (18,20,22,26,30)}
        if bytes(self.fonts[26].getmask("中")) == bytes(self.fonts[26].getmask("文")):
            raise ValueError("diagnostic font lacks distinct Chinese glyphs")
        self.timeline, self.plan, self.profile, self.revision = timeline, plan, profile, revision_id
        self.starts = [segment["start_ms"] for segment in plan["segments"]]
        with Image.open(preview_path) as image:
            self.curve = image.convert("RGB").crop((100,590,1540,805)).resize((1200,155))
        self.duration_ms = plan["segments"][-1]["end_ms"]

    def frame(self, frame_index):
        time_ms = frame_index * 1000 // self.profile.fps
        segment = self.plan["segments"][max(0,bisect.bisect_right(self.starts,time_ms)-1)]
        active = []
        for cue in self.timeline.cues:
            end = cue.end_ms or cue.start_ms
            clears = [marker.start_ms for marker in self.timeline.markers if marker.policy == "display_clear" and cue.start_ms <= marker.start_ms < end]
            if clears:
                end = min(clears)
            if cue.start_ms <= time_ms < end:
                active.append(cue)
        image = Image.new("RGB", (1280,720), "#f2f5fa")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0,0,1280,90), fill="#12233c")
        draw.text((24,12), "生成草稿依据 · 外部修订后本视频不会自动更新", font=self.fonts[26], fill="white")
        draw.text((24,53), f"修订 {self.revision[:12]}  |  时间 {time_ms/1000:.3f}s  |  视觉刷新 {1000/self.profile.fps:.1f}ms", font=self.fonts[18], fill="#c1d0e4")
        draw.rounded_rectangle((20,105,1260,315), radius=12, fill="white")
        text = "\n".join((f"[{cue.speaker_label}] " if cue.speaker_label else "") + cue.text_raw for cue in active) or "（当前没有译文字幕）"
        lines, truncated = wrap(draw, text, self.fonts[30], 1190, 4)
        draw.text((40,116), "译文：" + ", ".join(cue.cue_id for cue in active), font=self.fonts[18], fill="#67758a")
        for index, line in enumerate(lines):
            draw.text((40,147+index*39), line, font=self.fonts[30], fill="#172d48")
        draw.rounded_rectangle((20,329,1260,493), radius=12, fill="white")
        state = segment["state"]
        pace = {"slow":"慢速","medium":"中速","fast":"快速","unknown":"未知"}.get(state["pace_tier"],state["pace_tier"])
        amplitude = {"small":"小幅","medium":"中幅","large":"大幅","unknown":"未知"}.get(state["amplitude_tier"],state["amplitude_tier"])
        style = {"rhythmic":"规律","smooth":"平滑","irregular":"不规则","hold":"保持","unknown":"未知"}.get(state["motion_style"],state["motion_style"])
        status = "保持位置" if segment["mode"] == "hold_position" else "周期草稿"
        label = segment["preset_id"] or "保持当前位置"
        draw.text((40,340), f"段落 {segment['segment_id']}  |  {label}", font=self.fonts[22], fill="#21466e")
        draw.text((40,374), f"档位：{pace} / {amplitude} / {style}  |  计划：{status}", font=self.fonts[20], fill="#3c526b")
        evidence = f"依据：字幕 {','.join(segment['cue_ids']) or '无'}；事件 {len(segment['event_ids'])}；音频候选 {','.join(segment['audio_evidence_ids'][:2]) or '无'}"
        draw.text((40,405), evidence[:105], font=self.fonts[18], fill="#566b82")
        reasons = "复核：" + "；".join(REASONS.get(reason,reason) for reason in segment["review_reasons"])
        reason_lines, reason_truncated = wrap(draw, reasons, self.fonts[18], 1180, 2)
        for index, line in enumerate(reason_lines):
            draw.text((40,434+index*25), line, font=self.fonts[18], fill="#976626")
        draw.text((40,501), "导出草稿曲线（原始生成修订）", font=self.fonts[20], fill="#263e5c")
        image.paste(self.curve, (40,535))
        xx = 40 + min(1,max(0,time_ms/self.duration_ms))*1200
        draw.line((xx,535,xx,690), fill="#d4534b", width=3)
        draw.text((40,694), "需要人工试听复核；图像帧率不改变量化到毫秒的脚本时间轴。", font=self.fonts[18], fill="#637188")
        if image.size != (self.profile.width,self.profile.height):
            image = image.resize((self.profile.width,self.profile.height),Image.Resampling.LANCZOS)
        return image, {"frame_index":frame_index,"time_ms":time_ms,"cue_ids":[cue.cue_id for cue in active],
                       "segment_id":segment["segment_id"],"subtitle_display_truncated":truncated,
                       "reason_display_truncated":reason_truncated}
