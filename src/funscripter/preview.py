"""Static, shared-time waveform/plan/curve PNGs without a GUI dependency."""
import bisect
import wave
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from .audio import pcm_values


def font(size):
    for path in ["C:/Windows/Fonts/segoeui.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def waveform(path, start_ms, end_ms, width):
    low, high = np.zeros(width), np.zeros(width)
    with wave.open(str(path), "rb") as stream:
        rate, total = stream.getframerate(), stream.getnframes()
        first, stop = start_ms * rate // 1000, min(total, (end_ms * rate + 999) // 1000)
        stream.setpos(min(first, total))
        offset = first
        while offset < stop:
            raw = stream.readframes(min(65536, stop-offset))
            if not raw:
                break
            values = pcm_values(raw, stream.getsampwidth(), stream.getnchannels())
            indices = np.minimum(width-1, ((np.arange(len(values)) + offset-first)*width // max(1, stop-first))).astype(int)
            np.minimum.at(low, indices, values.min(axis=1))
            np.maximum.at(high, indices, values.max(axis=1))
            offset += len(values)
    return low, high


def render_page(path, wave_path, plan, actions, onsets, start_ms, end_ms, revision_id, subtitle_count, export_blocked=False):
    image = Image.new("RGB", (1600, 920), "#f4f6fa")
    draw = ImageDraw.Draw(image)
    regular, small, title = font(20), font(16), font(32)
    left, right = 100, 1540
    width = right-left
    span = max(1, end_ms-start_ms)
    x = lambda time: left + (time-start_ms)*width/span
    draw.rectangle((0, 0, 1600, 105), fill="#12233c")
    draw.text((40, 21), "Single-axis fusion draft", font=title, fill="white")
    draw.text((42, 66), f"{start_ms/1000:.3f} - {end_ms/1000:.3f} s  |  revision {revision_id[:12]}  |  {subtitle_count} cues", font=small, fill="#b9c9e0")
    draw.text((1080, 36), "EXPORT BLOCKED: CONSTRAINT FAILURE" if export_blocked else "HUMAN REVIEW REQUIRED", font=small, fill="#ff9f93" if export_blocked else "#ffd58c")
    panels = [(165, 345, "All-channel waveform envelope", "Normalized amplitude"),
              (420, 495, "Segment plan", ""), (590, 805, "Candidate curve (export blocked)" if export_blocked else "Exported offline draft curve", "Position (0-100)")]
    for top, bottom, label, unit in panels:
        draw.rounded_rectangle((30, top-45, 1570, bottom+40), radius=12, fill="white")
        draw.text((left, top-36), label, font=regular, fill="#142a47")
        if unit:
            draw.text((1160, top-34), unit, font=small, fill="#67758a")
        for tick in range(7):
            time = start_ms + span*tick/6
            xx = x(time)
            draw.line((xx, top, xx, bottom), fill="#e1e6ee")
            draw.text((xx-15, bottom+8), f"{time/1000:.2f}", font=small, fill="#637188")
    lows, highs = waveform(wave_path, start_ms, end_ms, width)
    for index, (low, high) in enumerate(zip(lows, highs)):
        draw.line((left+index, 255-high*80, left+index, 255-low*80), fill="#5a7b9b")
    for onset in onsets:
        if start_ms <= onset["time_ms"] <= end_ms:
            xx = x(onset["time_ms"])
            draw.line((xx, 165, xx, 180), fill="#ce625a", width=2)
    colors = {"preset": "#bfd8f5", "hold_position": "#e0e4ea"}
    for segment in plan["segments"]:
        first, last = max(start_ms, segment["start_ms"]), min(end_ms, segment["end_ms"])
        if first >= last:
            continue
        xx, yy = x(first), x(last)
        color = "#c1e4d4" if segment["manual_source_id"] else colors[segment["mode"]]
        draw.rectangle((xx, 425, yy, 485), fill=color, outline="#d6a349", width=2)
        if yy-xx > 95:
            label = segment["preset_id"] or "HOLD"
            if len(label) > int((yy-xx)/9):
                label = label[:max(4,int((yy-xx)/9)-2)] + ".."
            draw.text((xx+5, 440), label, font=small, fill="#233c58")
    times = [point["at"] for point in actions]
    positions = [point["pos"] for point in actions]
    first, last = bisect.bisect_left(times, start_ms), bisect.bisect_right(times, end_ms)
    plot_times = [start_ms] + times[first:last] + [end_ms]
    plot_positions = np.interp(plot_times, times, positions)
    for pos in [0, 25, 50, 75, 100]:
        yy = 805-pos*2.15
        draw.line((left, yy, right, yy), fill="#e1e6ee")
        draw.text((55, yy-10), str(pos), font=small, fill="#637188")
    draw.line([(x(time), 805-pos*2.15) for time, pos in zip(plot_times, plot_positions)], fill="#2872b6", width=3)
    draw.text((100, 865), "Blue: generated preset    Gray: explicit hold    Green: manual preset    Amber outline: review pending", font=small, fill="#42556d")
    draw.text((100, 891), "Shared audio time in seconds. Onset ticks are acoustic candidates. Device / player compatibility is not established by this plot.", font=small, fill="#637188")
    image.save(path)


def render_previews(output, wave_path, plan, script, audio, duration_ms, page_ms, revision_id, subtitle_count, export_blocked=False):
    output = Path(output)
    onsets = (audio or {}).get("onsets", [])
    render_page(output / "preview.png", wave_path, plan, script["actions"], onsets, 0, duration_ms, revision_id, subtitle_count, export_blocked)
    paths = ["preview.png"]
    if duration_ms > page_ms:
        (output / "preview_pages").mkdir()
        for index, start in enumerate(range(0, duration_ms, page_ms), 1):
            name = f"preview_pages/page-{index:04d}.png"
            render_page(output / name, wave_path, plan, script["actions"], onsets,
                        start, min(duration_ms,start+page_ms), revision_id, subtitle_count, export_blocked)
            paths.append(name)
    return paths
