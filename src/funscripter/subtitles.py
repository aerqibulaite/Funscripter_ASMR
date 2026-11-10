"""Format-based subtitle import. Text remains opaque source data."""
import re
from bisect import bisect_left, bisect_right
from pathlib import Path

from .lrc import parse_lrc
from .models import Cue, Marker, Timeline

TIME = r"(-?\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
SRT_TIME = re.compile(rf"^{TIME}\s*-->\s*{TIME}(?:\s+(.+))?$")


def subtitle_format(config):
    if config.format != "auto":
        return config.format
    return "srt" if Path(config.path).suffix.lower() == ".srt" else "lrc"


def timestamp(groups, line):
    hour, minute, second, fraction = groups
    if int(minute) >= 60 or int(second) >= 60:
        raise ValueError(f"SRT line {line}: minutes and seconds must be below 60")
    value = (abs(int(hour)) * 3600 + int(minute) * 60 + int(second)) * 1000
    value += int(fraction.ljust(3, "0"))
    return -value if hour.startswith("-") else value


def parse_srt(text, duration_ms, config):
    lines = text.lstrip("\ufeff").splitlines()
    blocks, first = [], 0
    for index in range(len(lines) + 1):
        if index == len(lines) or not lines[index].strip():
            if first < index:
                blocks.append((first + 1, lines[first:index]))
            first = index + 1
    cues, pending_markers, issues = [], [], []
    previous, seen_numbers, seen_rows = None, set(), set()
    for ordinal, (line, block) in enumerate(blocks, 1):
        time_index = 1 if block[0].strip().isdigit() else 0
        number = int(block[0].strip()) if time_index else None
        if time_index >= len(block) or "-->" not in block[time_index]:
            issues.append({"reason": "unparsed_srt_block", "source_line": line})
            continue
        match = SRT_TIME.fullmatch(block[time_index].strip())
        if not match:
            raise ValueError(f"SRT line {line + time_index}: invalid timestamp syntax")
        raw_start = timestamp(match.groups()[:4], line + time_index)
        raw_end = timestamp(match.groups()[4:8], line + time_index)
        start, end = raw_start + config.offset_ms, raw_end + config.offset_ms
        body = "\n".join(block[time_index + 1:])
        context = {"source_line": line, "time_ms": start, "cue_id": f"cue_{ordinal:06d}"}
        if previous is not None and start < previous:
            issues.append({"reason": "out_of_order", **context})
        previous = start
        if number is not None and number in seen_numbers:
            issues.append({"reason": "duplicate_srt_number", "source_number": number, **context})
        if number is not None:
            seen_numbers.add(number)
        if (start, end, body) in seen_rows:
            issues.append({"reason": "duplicate", **context})
        seen_rows.add((start, end, body))
        if end == start:
            issues.append({"reason": "zero_duration", **context})
        elif end < start:
            issues.append({"reason": "reversed_interval", **context})
        if not 0 <= start < duration_ms or not 0 <= end <= duration_ms:
            issues.append({"reason": "outside_audio", "end_ms": end, **context})
        if "\ufffd" in body:
            issues.append({"reason": "replacement_character_in_source", "count": body.count("\ufffd"), **context})
        if match[9]:
            issues.append({"reason": "srt_positioning_not_applied", "settings": match[9], **context})
        if body.strip():
            cues.append(Cue(cue_id=context["cue_id"], source_line=line, source_number=number,
                text_raw=body, text_for_analysis=body, original_start_ms=raw_start, original_end_ms=raw_end,
                start_ms=start, end_ms=end, end_inferred=False))
        else:
            pending_markers.append(Marker(marker_id=f"marker_{ordinal:06d}", source_line=line, source_number=number,
                original_start_ms=raw_start, original_end_ms=raw_end, start_ms=start, end_ms=end,
                policy="srt_empty_entry", previous_cue_ids=[], next_cue_ids=[]))
    if not cues and not pending_markers:
        raise ValueError("SRT has no timestamped entries")
    cues.sort(key=lambda cue: cue.start_ms)
    # Diagnose overlap without deleting or shortening source intervals.
    active = []
    for cue in cues:
        if cue.end_ms <= cue.start_ms:
            continue
        active = [other for other in active if other.end_ms > cue.start_ms]
        for other in active:
            issues.append({"reason": "overlapping_subtitles", "cue_id": cue.cue_id,
                "other_cue_id": other.cue_id, "source_line": cue.source_line,
                "time_ms": cue.start_ms, "overlap_ms": min(other.end_ms, cue.end_ms) - cue.start_ms})
        active.append(cue)
    starts = [cue.start_ms for cue in cues]
    for marker in pending_markers:
        before, after = bisect_right(starts, marker.start_ms), bisect_right(starts, marker.start_ms)
        if before:
            first_equal = bisect_left(starts, starts[before - 1])
            marker.previous_cue_ids = [cue.cue_id for cue in cues[first_equal:before]]
        if after < len(cues):
            last_equal = bisect_right(starts, starts[after])
            marker.next_cue_ids = [cue.cue_id for cue in cues[after:last_equal]]
    return Timeline(metadata={"subtitle_format": "srt"}, lrc_offset_ms=0,
                    effective_offset_ms=config.offset_ms, cues=cues, markers=pending_markers, issues=issues)


def parse_subtitles(text, duration_ms, config):
    return (parse_srt if subtitle_format(config) == "srt" else parse_lrc)(text, duration_ms, config)
