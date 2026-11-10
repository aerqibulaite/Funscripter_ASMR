import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".mts", ".m2ts", ".mpeg", ".mpg", ".ogv", ".flv"}


def finite_seconds(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def probe_input(source, executable):
    try:
        result = subprocess.run([executable, "-v", "error", "-show_entries",
            "format=start_time,duration:stream=index,codec_type,codec_name,start_time,duration,sample_rate,channels:stream_disposition=attached_pic:stream_tags=language,title",
            "-of", "json", str(source)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        raise ValueError("FFprobe timed out while inspecting the source") from None
    if result.returncode:
        raise ValueError(f"FFprobe failed: {result.stderr[-1500:]}")
    return json.loads(result.stdout)


def packet_duration(source, executable, origin, streams):
    """Resolve nonzero-origin container durations without loading packet lists."""
    end, count = None, 0
    required = {str(stream["index"]) for stream in streams if stream.get("codec_type") in ("audio", "video")
                and not stream.get("disposition", {}).get("attached_pic")}
    observed = set()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([executable, "-v", "error", "-show_packets", "-show_entries",
            "packet=stream_index,pts_time,duration_time", "-of", "compact=p=0:nk=0", str(source)],
            stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8", errors="replace")
        timer = threading.Timer(120, process.kill)
        timer.daemon = True
        timer.start()
        try:
            for line in process.stdout:
                values = dict(field.split("=", 1) for field in line.strip().split("|") if "=" in field)
                if values.get("stream_index") not in required:
                    continue
                pts, duration = finite_seconds(values.get("pts_time")), finite_seconds(values.get("duration_time"))
                if pts is not None and duration is not None and duration > 0:
                    end = max(end if end is not None else pts, pts + duration)
                    count += 1
                    observed.add(values["stream_index"])
            code = process.wait()
        finally:
            timer.cancel()
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        if code or end is None or end <= origin or observed != required:
            raise ValueError("cannot establish nonzero-origin media packet timeline; normalize the source before import")
    return end - origin, count


def decode_audio(source, destination, ffmpeg="ffmpeg", ffprobe="ffprobe", audio_stream=0):
    if type(audio_stream) is not int or audio_stream < 0:
        raise ValueError("audio_stream must be a nonnegative audio stream ordinal")
    source = Path(source)
    media_info, video, duration_seconds, origin = None, False, None, 0.0
    executable = shutil.which(ffmpeg)
    if executable:
        sibling = Path(executable).with_name("ffprobe.exe" if Path(executable).suffix.lower() == ".exe" else "ffprobe")
        probe = str(sibling) if ffprobe == "ffprobe" and sibling.is_file() else shutil.which(ffprobe)
        if probe:
            media_info = probe_input(source, probe)
            streams = media_info.get("streams", [])
            audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
            if audio_stream >= len(audio):
                raise ValueError(f"audio stream {audio_stream} does not exist; available audio streams: {len(audio)}")
            video = any(stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic") for stream in streams)
            if video:
                info = media_info.get("format", {})
                origin = finite_seconds(info.get("start_time"))
                duration_seconds = finite_seconds(info.get("duration"))
                if origin is None or duration_seconds is None or duration_seconds <= 0:
                    raise ValueError("video requires a finite container start time and positive duration")
                if abs(origin) > 0.000001:
                    duration_seconds, packet_count = packet_duration(source, probe, origin, streams)
                    media_info["normalized_packet_timeline"] = {"duration_seconds": duration_seconds, "packet_count": packet_count}
        elif source.suffix.lower() in VIDEO_SUFFIXES:
            raise ValueError("video import requires FFprobe to preserve the container timeline")
        version = subprocess.run([executable, "-version"], capture_output=True, check=True,
                                 text=True, encoding="utf-8", errors="replace").stdout.splitlines()[0]
        args = [executable, "-nostdin", "-v", "error", "-y"]
        if video:
            args += ["-copyts", "-start_at_zero"]
        args += ["-i", str(source), "-map", f"0:a:{audio_stream}", "-vn"]
        if video:
            # Retain late audio starts and timestamp gaps on the container clock,
            # and keep a video tail even if the chosen audio ends earlier.
            args += ["-af", f"aresample=async=1:first_pts=0,apad=whole_dur={duration_seconds:.9f}",
                     "-t", f"{duration_seconds:.9f}"]
        args += ["-c:a", "pcm_s16le", str(destination)]
        result = subprocess.run(args,
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise ValueError(f"FFmpeg decode failed: {result.stderr[-2000:]}")
        decoder = "ffmpeg"
    else:
        if audio_stream:
            raise ValueError("audio stream selection requires FFmpeg")
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
    result = {"track_id": "main", "source_path": str(source), "source_sha256": sha256(source),
        "analysis_copy": "media/analysis.wav", "decoder": decoder, "decoder_version": version,
        "sample_rate": rate, "sample_count": frames, "channels": channels,
        "sample_width_bytes": width, "duration_ms": frames * 1000 // rate,
        "source_start_ms": origin * 1000 if video else 0, "analysis_start_ms": 0,
        "time_mapping": "audio_ms = sample_index * 1000 / sample_rate",
        "compressed_source_sync_verified": False, "auxiliary_tracks": []}
    result.update(source_kind="video" if video else "audio", selected_audio_stream=audio_stream,
                  container_probe=media_info,
                  timeline_policy="container_start_zero_pad_trim" if video else "decoded_audio_start_zero")
    if video:
        result.update(container_origin_seconds=origin, container_duration_seconds=duration_seconds,
            source_time_mapping="source_timestamp_ms = audio_ms + container_origin_seconds * 1000",
            synchronization_review_required=True)
    return result
