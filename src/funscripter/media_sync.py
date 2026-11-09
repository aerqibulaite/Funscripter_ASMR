"""Measured container/sample checks, kept separate from player compatibility claims."""
import json
import subprocess
import wave
from pathlib import Path
import numpy as np
from .audio import pcm_values


def run_tool(args, timeout=120):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("media tool failed or timed out: " + type(exc).__name__) from None
    if result.returncode:
        raise ValueError("media tool failed: " + result.stderr.decode("utf-8",errors="replace")[-1500:])
    return result.stdout


def alignment_checks(source, decoded_path, config):
    with wave.open(str(source), "rb") as stream:
        rate, count, channels = stream.getframerate(), stream.getnframes(), stream.getnchannels()
        decoded_count = Path(decoded_path).stat().st_size // (4*channels)
        if not decoded_count:
            raise ValueError("encoded preview decodes to empty audio")
        decoded = np.memmap(decoded_path,dtype="<f4",mode="r",shape=(decoded_count,channels))
        checks = []
        try:
            size = min(count,max(16,rate//2))
            positions = [("head",0),("middle",max(0,count//2-size//2)),("tail",max(0,count-size))]
            radius = max(1,round(rate*config.alignment_search_ms/1000))
            for name, first in positions:
                stream.setpos(first)
                reference = pcm_values(stream.readframes(size),stream.getsampwidth(),channels)
                column = int(np.argmax(np.std(reference,axis=0)))
                vector = reference[:,column]
                vector = vector - np.mean(vector)
                norm = float(np.linalg.norm(vector))
                row = {"anchor":name,"source_start_sample":first,"window_samples":len(vector),"channel":column}
                if norm < 1e-7:
                    row.update(status="uninformative_low_energy",lag_samples=None,lag_ms=None)
                    checks.append(row)
                    continue
                begin, end = max(0,first-radius),min(decoded_count,first+radius+size)
                search = np.asarray(decoded[begin:end,column],dtype=float)
                if len(search)<size:
                    row.update(status="insufficient_decoded_samples",lag_samples=None,lag_ms=None)
                    checks.append(row)
                    continue
                dots = np.correlate(search,vector,mode="valid")
                sums = np.concatenate(([0.0],np.cumsum(search)))
                squares = np.concatenate(([0.0],np.cumsum(search*search)))
                energy = np.maximum(0,squares[size:]-squares[:-size]-(sums[size:]-sums[:-size])**2/size)
                scores = dots / np.maximum(norm*np.sqrt(energy),1e-15)
                best = int(np.argmax(scores))
                lag = begin+best-first
                score = float(scores[best])
                local = np.ones(len(scores),dtype=bool)
                local[1:] &= scores[1:]>=scores[:-1]
                local[:-1] &= scores[:-1]>=scores[1:]
                alternatives = np.flatnonzero(local & (scores>=score-0.001))
                ambiguous = any(abs(int(other)-best)*1000/rate>config.max_alignment_error_ms for other in alternatives)
                status = ("low_correlation" if score<config.min_alignment_correlation else
                          "ambiguous_periodic_waveform" if ambiguous else
                          "passed" if abs(lag)*1000/rate<=config.max_alignment_error_ms else "offset_exceeds_tolerance")
                row.update(status=status,lag_samples=lag,lag_ms=lag*1000/rate,correlation=score,
                           near_peak_count=len(alternatives))
                checks.append(row)
        finally:
            decoded._mmap.close()
    return {"source_samples":count,"decoded_samples":decoded_count,"sample_rate":rate,
            "decoded_tail_delta_samples":decoded_count-count,"decoded_tail_delta_ms":(decoded_count-count)*1000/rate,
            "anchor_checks":checks,
            "sample_count_check_passed":-1 <= decoded_count-count <= config.max_tail_padding_ms*rate/1000,
            "three_anchor_alignment_passed":all(row["status"]=="passed" for row in checks)}


def inspect_video(ffmpeg, ffprobe, video, source, output, config, duration_seconds):
    probe = json.loads(run_tool([ffprobe,"-v","error","-show_streams","-show_format","-of","json",str(video)]))
    video_streams = [stream for stream in probe["streams"] if stream["codec_type"]=="video"]
    audio_streams = [stream for stream in probe["streams"] if stream["codec_type"]=="audio"]
    if len(video_streams)!=1 or len(audio_streams)!=1:
        raise ValueError("diagnostic video must contain one video stream and one audio stream")
    video_stream,audio_stream = video_streams[0],audio_streams[0]
    starts = {"video":float(video_stream.get("start_time","nan")),"audio":float(audio_stream.get("start_time","nan"))}
    observed_duration = float(probe["format"]["duration"])
    container_ok = all(abs(start)<=0.001 for start in starts.values()) and abs(observed_duration-duration_seconds)<=1/config.fps+0.05
    packets = json.loads(run_tool([ffprobe,"-v","error","-select_streams","a:0","-read_intervals","%+#2", "-show_packets","-of","json",str(video)]))
    decoded = Path(output) / "decoded-audio-working.f32"
    try:
        with wave.open(str(source),"rb") as stream:
            rate,channels = stream.getframerate(),stream.getnchannels()
        run_tool([ffmpeg,"-nostdin","-v","error","-y","-i",str(video),"-map","0:a:0",
                  "-f","f32le","-c:a","pcm_f32le","-ar",str(rate),"-ac",str(channels),str(decoded)],
                 timeout=config.render_timeout_seconds)
        sample_checks = alignment_checks(source,decoded,config)
    finally:
        decoded.unlink(missing_ok=True)
    passed = container_ok and sample_checks["sample_count_check_passed"] and sample_checks["three_anchor_alignment_passed"]
    return {"container_checks_passed":container_ok,"start_times_seconds":starts,
            "expected_audio_duration_seconds":duration_seconds,"container_duration_seconds":observed_duration,
            "container_tail_delta_ms":(observed_duration-duration_seconds)*1000,
            "video_stream":video_stream,"audio_stream":audio_stream,"first_audio_packets":packets.get("packets",[]),
            **sample_checks,"automated_alignment_status":"passed" if passed else "needs_review",
            "ofs_compatibility":"not_tested","multifunplayer_sync":"not_tested"}
