"""Revision-bound diagnostic MP4 and matching OFS script copy."""
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
import yaml
from .models import DiagnosticProfile, SegmentPlan
from .project import reopen_project, atomic_json
from .fuse_project import read_json, verify_bundle, verify_source
from .media import sha256
from .media_sync import run_tool, inspect_video
from .diagnostic_render import DiagnosticRenderer


def resolve_tool(value, base):
    candidate = base / value
    executable = str(candidate.resolve()) if candidate.is_file() else shutil.which(value)
    if not executable:
        raise ValueError("media executable not found: " + value)
    return executable


def encode_frames(renderer, source, output, ffmpeg, config, duration, frame_count, directory):
    args = [ffmpeg,"-nostdin","-v","error","-y","-f","rawvideo","-pix_fmt","rgb24",
        "-video_size",f"{config.width}x{config.height}","-framerate",str(config.fps),"-i","pipe:0",
        "-i",str(source),"-map","0:v:0","-map","1:a:0","-c:v","libx264","-preset","veryfast",
        "-crf",str(config.crf),"-pix_fmt","yuv420p","-c:a","aac","-ar",str(config.audio_sample_rate),
        "-b:a",config.audio_bitrate,"-t",f"{duration:.12f}","-movflags","+faststart",str(output)]
    samples = sorted({0,frame_count//2,frame_count-1})
    captured, process, timer = [], None, None
    started = time.monotonic()
    log_path = Path(directory) / "encoder.log"
    with log_path.open("wb") as log:
        try:
            process = subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=log)
            timer = threading.Timer(config.render_timeout_seconds,process.kill)
            timer.daemon = True
            timer.start()
            for index in range(frame_count):
                frame, metadata = renderer.frame(index)
                if index in samples:
                    frame.save(Path(directory) / f"frame-{index:06d}.png")
                    captured.append(metadata)
                process.stdin.write(frame.tobytes())
            process.stdin.close()
            code = process.wait(timeout=config.render_timeout_seconds)
            if code:
                raise ValueError("FFmpeg encoding failed")
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            if process is not None and process.poll() is None:
                process.kill()
            if process is not None:
                process.wait()
            log.flush()
            detail = log_path.read_text(encoding="utf-8",errors="replace")[-1500:]
            raise ValueError(f"diagnostic encode failed ({type(exc).__name__}): {detail}") from None
        finally:
            if timer:
                timer.cancel()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                if process.stdin and not process.stdin.closed:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
    return {"command":args,"frame_count":frame_count,"sample_frames":captured,
            "elapsed_seconds":round(time.monotonic()-started,3)}


def create_diagnostic(directory, draft, output, profile_path):
    directory,draft,output = Path(directory).resolve(),Path(draft).resolve(),Path(output).resolve()
    profile_path = Path(profile_path).resolve()
    if output.exists() or output.is_relative_to(directory) or output.is_relative_to(draft):
        raise ValueError("diagnostic output must be a new directory outside its sources")
    project,timeline,media,_ = reopen_project(directory)
    baseline = read_json(draft / "export_manifest.json")
    verify_source(baseline,project,"subtitle_sha256")
    verify_bundle(draft,baseline,"artifact_hashes")
    required = {"track.funscript","segment_plan.json","preview.png","validation.json"}
    if not baseline.get("script_exported") or not required <= set(baseline["artifact_hashes"]):
        raise ValueError("diagnostic requires a constraint-qualified exported draft")
    if read_json(draft / "validation.json").get("export_blocked"):
        raise ValueError("diagnostic source script export is blocked")
    plan = SegmentPlan.model_validate_json((draft / "segment_plan.json").read_text(encoding="utf-8")).model_dump(mode="json")
    config = DiagnosticProfile.model_validate_json(json.dumps(yaml.safe_load(profile_path.read_text(encoding="utf-8-sig")),allow_nan=False))
    if not Path(config.font_path).is_absolute():
        config.font_path = str((profile_path.parent / config.font_path).resolve())
    renderer = DiagnosticRenderer(timeline,plan,draft / "preview.png",config,baseline["revision_id"])
    ffmpeg,ffprobe = resolve_tool(config.ffmpeg,profile_path.parent),resolve_tool(config.ffprobe,profile_path.parent)
    tool_versions = {"ffmpeg":run_tool([ffmpeg,"-version"]).decode("utf-8",errors="replace").splitlines()[0],
                     "ffprobe":run_tool([ffprobe,"-version"]).decode("utf-8",errors="replace").splitlines()[0]}
    duration = media["sample_count"] / media["sample_rate"]
    frame_count = max(1,(media["sample_count"]*config.fps + media["sample_rate"]-1)//media["sample_rate"])
    source = directory / project.artifacts["analysis_audio"]
    output.parent.mkdir(parents=True,exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".funscripter-diagnostic-",dir=output.parent))
    try:
        render = encode_frames(renderer,source,stage / "track.mp4",ffmpeg,config,duration,frame_count,stage)
        sync = inspect_video(ffmpeg,ffprobe,stage / "track.mp4",source,stage,config,duration)
        shutil.copyfile(draft / "track.funscript",stage / "track.funscript")
        if sha256(stage / "track.funscript") != baseline["artifact_hashes"]["track.funscript"]:
            raise ValueError("diagnostic script copy does not match baseline")
        atomic_json(stage / "sync_report.json",sync)
        atomic_json(stage / "render_report.json",render)
        atomic_json(stage / "subtitles.json",timeline.model_dump(mode="json"))
        compatibility = {"ofs":{"status":"not_tested","checks":["load track.mp4 and matching track.funscript","listen at head/middle/tail","pause and seek","edit and save as a new funscript","reopen saved revision"]},
            "multifunplayer":{"status":"not_tested","checks":["bind media and L0 script","verify head/middle/tail","pause/seek/offset/playback speed"]},
            "diagnostic_relation":"generated_baseline_only; external edits do not update this video"}
        atomic_json(stage / "compatibility.json",compatibility)
        manifest = {"schema_version":"1.3","status":"needs_review","project_id":project.project_id,
            "source_revision_id":project.revision_id,"fusion_revision_id":baseline["revision_id"],
            "subtitle_sha256":project.artifact_hashes["subtitles"],"original_audio_sha256":project.input_hashes["audio"],
            "analysis_audio_sha256":project.artifact_hashes["analysis_audio"],
            "baseline_manifest_sha256":sha256(draft / "export_manifest.json"),
            "plan_sha256":baseline["artifact_hashes"]["segment_plan.json"],
            "script_sha256":baseline["artifact_hashes"]["track.funscript"],
            "profile":config.model_dump(mode="json"),"font_sha256":sha256(config.font_path),
            "tools":tool_versions,"tool_hashes":{"ffmpeg":sha256(ffmpeg),"ffprobe":sha256(ffprobe)},
            "implementation_hashes":{name:sha256(Path(__file__).with_name(name)) for name in
                ["diagnostic.py","diagnostic_render.py","media_sync.py"]},
            "audio_encoding":"AAC listening copy from saved analysis PCM; original input is unchanged",
            "visual_frame_interval_ms":1000/config.fps,"script_time_quantized_to_video_frames":False,
            "automated_alignment_status":sync["automated_alignment_status"],
            "human_player_verification":"not_tested", "artifact_hashes":{path.name:sha256(path) for path in stage.iterdir() if path.is_file()}}
        atomic_json(stage / "diagnostic_manifest.json",manifest)
        os.replace(stage,output)
    except BaseException:
        shutil.rmtree(stage)
        raise
    return manifest
