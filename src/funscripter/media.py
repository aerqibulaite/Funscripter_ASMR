import hashlib
import shutil
import subprocess
import wave
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_audio(source, destination, ffmpeg="ffmpeg"):
    executable = shutil.which(ffmpeg)
    if executable:
        version = subprocess.run([executable, "-version"], capture_output=True, check=True,
                                 text=True, encoding="utf-8", errors="replace").stdout.splitlines()[0]
        result = subprocess.run([executable, "-nostdin", "-v", "error", "-y", "-i", str(source),
            "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", str(destination)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise ValueError(f"FFmpeg decode failed: {result.stderr[-2000:]}")
        decoder = "ffmpeg"
    else:
        # Only wave-readable PCM is supported by the explicit fallback.
        try:
            with wave.open(str(source), "rb") as stream:
                if stream.getcomptype() != "NONE":
                    raise ValueError("only PCM WAV is supported without FFmpeg")
                with wave.open(str(destination), "wb") as target:
                    target.setparams(stream.getparams())
                    while data := stream.readframes(65536):
                        target.writeframesraw(data)
        except (wave.Error, EOFError) as exc:
            raise ValueError("FFmpeg unavailable: install FFmpeg for this audio format") from exc
        decoder, version = "python_wave_pcm_fallback", "stdlib wave"
    with wave.open(str(destination), "rb") as stream:
        frames, rate, channels, width = (stream.getnframes(), stream.getframerate(),
                                       stream.getnchannels(), stream.getsampwidth())
        if frames == 0:
            raise ValueError("audio is empty")
    return {"track_id": "main", "source_path": str(source), "source_sha256": sha256(source),
        "analysis_copy": "media/analysis.wav", "decoder": decoder, "decoder_version": version,
        "sample_rate": rate, "sample_count": frames, "channels": channels,
        "sample_width_bytes": width, "duration_ms": frames * 1000 // rate,
        "source_start_ms": 0, "analysis_start_ms": 0,
        "time_mapping": "audio_ms = sample_index * 1000 / sample_rate",
        "compressed_source_sync_verified": False, "auxiliary_tracks": []}
