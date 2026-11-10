import re
from bisect import bisect_right
from .models import Cue, Marker, Timeline

STAMP = re.compile(r"\[(-?\d+):(\d{2})(?:\.(\d{1,3}))?\]")
META = re.compile(r"^\[([A-Za-z][\w-]*):(.*)\]$")


def parse_lrc(text, duration_ms, config):
    metadata, rows, issues = {}, [], []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.lstrip("\ufeff")
        matches = []
        cursor = 0
        while match := STAMP.match(line, cursor):
            matches.append(match)
            cursor = match.end()
        if matches:
            body = line[cursor:]
            for match in matches:
                minute, second, fraction = match.groups()
                if int(second) >= 60:
                    raise ValueError(f"LRC line {number}: seconds must be below 60")
                value = abs(int(minute)) * 60000 + int(second) * 1000
                value += int((fraction or "").ljust(3, "0"))
                if minute.startswith("-"):
                    value = -value
                rows.append((number, value, body))
        elif match := META.fullmatch(line.strip()):
            metadata[match[1].lower()] = match[2].strip()
        elif line.strip():
            issues.append({"reason": "unparsed_line", "source_line": number})
    try:
        lrc_offset = int(metadata.get("offset", "0"))
    except ValueError as exc:
        raise ValueError("LRC offset must be integer milliseconds") from exc
    offset = config.offset_ms + (lrc_offset if config.apply_lrc_offset else 0)
    if not rows:
        raise ValueError("LRC has no timestamped lines")
    previous = None
    seen_time, seen_row = set(), set()
    for line, raw, body in rows:
        time = raw + offset
        if previous is not None and time < previous:
            issues.append({"reason": "out_of_order", "source_line": line})
        previous = time
        if time in seen_time:
            issues.append({"reason": "same_timestamp", "source_line": line, "time_ms": time})
        if (time, body) in seen_row:
            issues.append({"reason": "duplicate", "source_line": line, "time_ms": time})
        seen_time.add(time)
        seen_row.add((time, body))
        if not 0 <= time < duration_ms:
            issues.append({"reason": "outside_audio", "source_line": line, "time_ms": time})
    # Source order IDs survive sorting and offset changes; file changes need reconciliation.
    cues, empty = [], []
    for index, (line, raw, body) in enumerate(rows, 1):
        if body.strip():
            cues.append(Cue(cue_id=f"cue_{index:06d}", source_line=line,
                            text_raw=body, text_for_analysis=body,
                            original_start_ms=raw, start_ms=raw + offset, end_ms=None))
        else:
            empty.append((index, line, raw))
    cues.sort(key=lambda cue: cue.start_ms)
    starts = sorted(set(cue.start_ms for cue in cues))
    for cue in cues:
        next_index = bisect_right(starts, cue.start_ms)
        next_start = starts[next_index] if next_index < len(starts) else duration_ms
        end = min(next_start, duration_ms)
        cue.end_ms = end if end > cue.start_ms else None
    markers = []
    for index, line, raw in empty:
        time = raw + offset
        before = [cue for cue in cues if cue.start_ms <= time]
        after = [cue for cue in cues if cue.start_ms > time]
        markers.append(Marker(marker_id=f"marker_{index:06d}", source_line=line,
            original_start_ms=raw, start_ms=time, policy=config.empty_timestamp_policy,
            previous_cue_ids=[c.cue_id for c in before if c.start_ms == before[-1].start_ms],
            next_cue_ids=[c.cue_id for c in after if c.start_ms == after[0].start_ms]))
    return Timeline(metadata=metadata, lrc_offset_ms=lrc_offset,
                    effective_offset_ms=offset, cues=cues, markers=markers, issues=issues)


def candidate_document(timeline):
    result = {"schema_version": "1.3", "cues": [
        {"cue_id": cue.cue_id, "utterance_end_candidates": [
            {"time_ms": marker.start_ms, "method": "empty_timestamp_hint",
             "evidence_id": marker.marker_id, "status": "unaccepted",
             "reason": "subtitle_marker_is_not_confirmed_audio_end"}
            for marker in timeline.markers
            if cue.cue_id in marker.previous_cue_ids and marker.policy == "end_hint"
            and marker.start_ms > cue.start_ms and cue.end_ms is not None
            and marker.start_ms <= cue.end_ms], "accepted_candidate": None}
        for cue in timeline.cues]}
    outside = {issue.get("cue_id") for issue in timeline.issues if issue.get("reason") == "outside_audio"}
    for cue, row in zip(timeline.cues, result["cues"]):
        if (not cue.end_inferred and cue.end_ms is not None
                and cue.cue_id not in outside and max(0, cue.start_ms) < cue.end_ms):
            row["utterance_end_candidates"].append({"time_ms": cue.end_ms,
                "method": "subtitle_display_end", "evidence_id": "display_end_" + cue.cue_id,
                "status": "unaccepted", "reason": "subtitle_display_end_not_confirmed_utterance_end"})
    return result
