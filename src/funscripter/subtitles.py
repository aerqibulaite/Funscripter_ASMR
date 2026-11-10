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
    suffix = Path(config.path).suffix.lower()
    return suffix[1:] if suffix in (".srt", ".vtt") else "lrc"


def timestamp(groups, line, format_name="SRT"):
    hour, minute, second, fraction = groups
    if int(minute) >= 60 or int(second) >= 60:
        raise ValueError(f"{format_name} line {line}: minutes and seconds must be below 60")
    value = (abs(int(hour)) * 3600 + int(minute) * 60 + int(second)) * 1000
    value += int(fraction.ljust(3, "0"))
    return -value if hour.startswith("-") else value


def blocks_with_lines(lines):
    blocks, first = [], 0
    for index in range(len(lines) + 1):
        if index == len(lines) or not lines[index].strip():
            if first < index:
                blocks.append((first + 1, lines[first:index]))
            first = index + 1
    return blocks


def parse_srt(text, duration_ms, config):
    entries, issues = [], []
    blocks = blocks_with_lines(text.lstrip("\ufeff").splitlines())
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
        body = "\n".join(block[time_index + 1:])
        entries.append((ordinal, line, number, None, raw_start, raw_end, body, match[9]))
    return timed_timeline(entries, duration_ms, config, "srt", issues)


VTT_TIMESTAMP = r"(?:(\d{2,}):)?(\d{2}):(\d{2})\.(\d{3})"
VTT_TIMING = re.compile(rf"^{VTT_TIMESTAMP}[ \t]+-->[ \t]+{VTT_TIMESTAMP}(?:[ \t]+(.+))?$")


def parse_vtt(text, duration_ms, config):
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or not re.fullmatch(r"WEBVTT(?:[ \t].*)?", lines[0]) or "-->" in lines[0]:
        raise ValueError("VTT line 1: missing or invalid WEBVTT header")
    entries, issues, header = [], [], []
    first = 1
    while first < len(lines) and lines[first].strip():
        if "-->" in lines[first]:
            raise ValueError("VTT header must be separated from cues by a blank line")
        header.append(lines[first])
        first += 1
    # HLS mapping uses a separate clock, so never silently treat it as local video time.
    if any(line.strip().startswith("X-TIMESTAMP-MAP") for line in header):
        raise ValueError("VTT X-TIMESTAMP-MAP requires conversion to the local media timeline before import")
    blocks = blocks_with_lines(lines[first:])
    for ordinal, (relative_line, block) in enumerate(blocks, 1):
        line = relative_line + first
        label = block[0].strip()
        if label == "NOTE" or label.startswith(("NOTE ", "NOTE\t")) or label in ("STYLE", "REGION"):
            issues.append({"reason": "vtt_non_cue_block_not_rendered", "kind": label.split()[0],
                           "source_line": line, "raw": "\n".join(block)})
            continue
        time_index = 0 if "-->" in block[0] else 1
        if time_index >= len(block) or "-->" not in block[time_index]:
            issues.append({"reason": "unparsed_vtt_block", "source_line": line})
            continue
        match = VTT_TIMING.fullmatch(block[time_index].strip())
        if not match:
            raise ValueError(f"VTT line {line + time_index}: invalid timestamp syntax")
        groups = [value if value is not None else "0" for value in match.groups()[:8]]
        raw_start = timestamp(groups[:4], line + time_index, "VTT")
        raw_end = timestamp(groups[4:8], line + time_index, "VTT")
        identifier = block[0] if time_index else None
        body = "\n".join(block[time_index + 1:])
        entries.append((ordinal, line, None, identifier, raw_start, raw_end, body, match[9]))
        if "<" in body or "&" in body:
            issues.append({"reason": "vtt_payload_markup_preserved", "source_line": line,
                           "cue_id": f"cue_{ordinal:06d}"})
    timeline = timed_timeline(entries, duration_ms, config, "vtt", issues)
    timeline.metadata.update(vtt_header=lines[0], vtt_header_metadata="\n".join(header))
    return timeline


def timed_timeline(entries, duration_ms, config, format_name, issues):
    cues, pending_markers = [], []
    previous, seen_numbers, seen_rows, seen_identifiers = None, set(), set(), set()
    for ordinal, line, number, identifier, raw_start, raw_end, body, settings in entries:
        start, end = raw_start + config.offset_ms, raw_end + config.offset_ms
        context = {"source_line": line, "time_ms": start, "cue_id": f"cue_{ordinal:06d}"}
        if previous is not None and start < previous:
            issues.append({"reason": "out_of_order", **context})
        previous = start
        if number is not None and number in seen_numbers:
            issues.append({"reason": "duplicate_srt_number", "source_number": number, **context})
        if number is not None:
            seen_numbers.add(number)
        if identifier is not None and identifier in seen_identifiers:
            issues.append({"reason": "duplicate_vtt_identifier", "source_identifier": identifier, **context})
        if identifier is not None:
            seen_identifiers.add(identifier)
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
        if settings:
            issues.append({"reason": f"{format_name}_positioning_not_applied", "settings": settings, **context})
        if body.strip():
            cues.append(Cue(cue_id=context["cue_id"], source_line=line, source_number=number,
                source_identifier=identifier, source_settings=settings,
                text_raw=body, text_for_analysis=body, original_start_ms=raw_start, original_end_ms=raw_end,
                start_ms=start, end_ms=end, end_inferred=False))
        else:
            pending_markers.append(Marker(marker_id=f"marker_{ordinal:06d}", source_line=line, source_number=number,
                source_identifier=identifier, source_settings=settings,
                original_start_ms=raw_start, original_end_ms=raw_end, start_ms=start, end_ms=end,
                policy=f"{format_name}_empty_entry", previous_cue_ids=[], next_cue_ids=[]))
    if not cues and not pending_markers:
        raise ValueError(f"{format_name.upper()} has no timestamped entries")
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
    return Timeline(metadata={"subtitle_format": format_name}, lrc_offset_ms=0,
                    effective_offset_ms=config.offset_ms, cues=cues, markers=pending_markers, issues=issues)


def parse_subtitles(text, duration_ms, config):
    return {"srt": parse_srt, "vtt": parse_vtt, "lrc": parse_lrc}[subtitle_format(config)](text, duration_ms, config)
