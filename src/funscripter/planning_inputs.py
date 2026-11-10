"""Derived planning eligibility; never modifies the imported subtitle timeline."""


def planning_inputs(timeline, duration_ms):
    issues = {}
    by_line = {}
    for cue in timeline.cues:
        by_line.setdefault(cue.source_line, []).append(cue.cue_id)
    for issue in timeline.issues:
        if issue.get("cue_id"):
            issues.setdefault(issue["cue_id"], set()).add(issue["reason"])
        if issue.get("other_cue_id"):
            issues.setdefault(issue["other_cue_id"], set()).add(issue["reason"])
        if not issue.get("cue_id"):
            for cue_id in by_line.get(issue.get("source_line"), []):
                issues.setdefault(cue_id, set()).add(issue["reason"])
    eligible, rows = [], []
    for cue in timeline.cues:
        flags = set(issues.get(cue.cue_id, []))
        excluded = []
        if cue.end_ms is None or cue.end_ms <= cue.start_ms:
            excluded.append("invalid_subtitle_interval")
        if cue.start_ms < 0 or cue.start_ms >= duration_ms or (cue.end_ms is not None and cue.end_ms > duration_ms):
            excluded.append("outside_audio_interval")
        if "\ufffd" in cue.text_for_analysis:
            flags.add("replacement_character_in_source")
        if cue.end_ms is not None and cue.end_ms - cue.start_ms > 15000:
            flags.add("long_subtitle_span")
        rows.append({"cue_id": cue.cue_id, "status": "excluded" if excluded else "eligible",
                     "reasons": excluded, "quality_flags": sorted(flags)})
        if not excluded:
            eligible.append(cue)
    return eligible, {"schema_version": "1", "duration_ms": duration_ms, "cues": rows,
                      "eligible_count": len(eligible), "excluded_count": len(rows) - len(eligible)}


def cue_payload(cue, quality_flags=()):
    return {"cue_id": cue.cue_id, "text": cue.text_for_analysis, "speaker_label": cue.speaker_label,
            "start_ms": cue.start_ms, "end_ms": cue.end_ms,
            "end_time_source": "inferred_candidate" if cue.end_inferred else "subtitle_display_end",
            "quality_flags": list(quality_flags)}
