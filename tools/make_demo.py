"""Generate a neutral, deterministic 12-second stereo PCM fixture."""
import math
import struct
import wave
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / "workspace/demo-input"
target.mkdir(parents=True, exist_ok=True)
with wave.open(str(target / "track.wav"), "wb") as stream:
    stream.setnchannels(2)
    stream.setsampwidth(2)
    stream.setframerate(22050)
    for second in range(12):
        frames = bytearray()
        for sample in range(22050):
            time = second + sample / 22050
            value = int(4000 * math.sin(2 * math.pi * 440 * time)) if 2 <= time < 10 else 0
            frames.extend(struct.pack("<hh", value, -value))
        stream.writeframes(frames)
(target / "track.lrc").write_text(
    "[ti:中性测试素材]\n[offset:0]\n[00:00.500]准备开始\n[00:02.00]测试第一段 / Test one\n"
    "[00:04.050]\n[00:05.5][00:07.50]继续测试\n[00:10.000]测试结束\n", encoding="utf-8-sig")
(target / "config.yaml").write_text('''schema_version: "1.3"
audio: track.wav
subtitles:
  path: track.lrc
  encoding: utf-8-sig
  offset_ms: 0
  apply_lrc_offset: true
  empty_timestamp_policy: end_hint
generation:
  initial_position: 50
  gap_policy: hold_position
  presets:
    example:
      low: 40
      high: 60
      period_ms: 2000
      up_time_ratio: 0.5
  segments:
    - segment_id: manual_001
      start_ms: 2000
      end_ms: 5000
      preset_id: example
    - segment_id: manual_002
      start_ms: 7000
      end_ms: 10000
      preset_id: example
# Device limits intentionally unresolved: this is an offline draft.
device:
  allowed_position_range: null
  max_speed_pos_per_sec: null
  final_interpolation: linear
''', encoding="utf-8")
print(target)
