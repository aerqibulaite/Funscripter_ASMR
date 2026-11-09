"""Measure synthetic CLI stages in separate processes, with peak process memory.

No models, FFmpeg or hardware are used. Outputs are never overwritten.
"""
import argparse
import hashlib
import json
import math
import os
import platform
import random
import shutil
import struct
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def peak_memory():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD),
                *[(name, ctypes.c_size_t) for name in ("peak_working_set", "working_set", "peak_paged_pool",
                  "paged_pool", "peak_nonpaged_pool", "nonpaged_pool", "pagefile", "peak_pagefile")]]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        query = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        query.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not query(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"peak_rss_bytes": counters.peak_working_set,
                "memory_method": "GetProcessMemoryInfo.PeakWorkingSetSize"}
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {"peak_rss_bytes": value if sys.platform == "darwin" else value * 1024,
            "memory_method": "getrusage(RUSAGE_SELF).ru_maxrss"}


def machine():
    info = {"os": platform.platform(), "architecture": platform.machine(),
            "processor": platform.processor(), "logical_cpus": os.cpu_count()}
    if os.name == "nt":
        import ctypes
        import winreg
        class Memory(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                *[(name, ctypes.c_ulonglong) for name in ("physical", "available", "pagefile",
                  "available_pagefile", "virtual", "available_virtual", "extended_virtual")]]
        memory = Memory()
        memory.length = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            info["physical_memory_bytes"] = memory.physical
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            info["processor"] = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    return info


def worker(report_path, command):
    started = time.perf_counter()
    code, error = 2, None
    try:
        from funscripter.cli import main
        code = main(command)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        elapsed = time.perf_counter() - started
        import importlib.metadata as metadata
        write_json(report_path, {"returncode": code, "error": error, "cli_seconds": round(elapsed, 6),
            **peak_memory(), "python_version": platform.python_version(), "python_executable": sys.executable,
            "prefix": sys.prefix, "base_prefix": sys.base_prefix,
            "package_file": getattr(sys.modules.get("funscripter"), "__file__", None),
            "packages": {dist.metadata["Name"]: dist.version for dist in metadata.distributions()}})
    return code


def fixture(directory, seconds, rate):
    """Bounded-memory deterministic stereo fixture with noise, pauses and modulation."""
    directory.mkdir()
    generator = random.Random(20261109)
    # One second per pattern; memory does not grow with audio duration.
    patterns = []
    for pattern in range(8):
        block = bytearray()
        for index in range(rate):
            t = index / rate
            envelope = 0.4 + 0.3 * math.sin(2 * math.pi * t / (0.4 + pattern * 0.05))
            signal = int(10000 * envelope * math.sin(2 * math.pi * (440 + pattern * 137) * t)
                         + generator.randrange(-300, 301))
            block.extend(struct.pack("<hh", signal, -signal))
        patterns.append(block)
    with wave.open(str(directory / "track.wav"), "wb") as stream:
        stream.setparams((2, 2, rate, 0, "NONE", "not compressed"))
        for second in range(seconds):
            stream.writeframesraw(b"\0" * rate * 4 if second % 29 == 0 else patterns[second % 8])
    (directory / "track.lrc").write_text("\n".join(
        f"[{second // 60:02d}:{second % 60:02d}.000]合成验收片段 {second // 30 + 1}"
        for second in range(0, seconds, 30)) + "\n", encoding="utf-8")
    # JSON is a YAML subset, avoiding a dependency in the benchmark controller.
    write_json(directory / "config.yaml", {"audio": "track.wav", "subtitles": {"path": "track.lrc"},
               "ffmpeg": "nonexistent-ffmpeg-for-benchmark"})


def tree_bytes(directory):
    return sum(path.stat().st_size for path in Path(directory).rglob("*") if path.is_file())


def run_stage(python, case, name, command, installed):
    logs = case / "logs"
    report_path = logs / f"{name}-metrics.json"
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    if not installed:
        env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.perf_counter()
    with (logs / f"{name}-stdout.json").open("wb") as stdout, (logs / f"{name}-stderr.txt").open("wb") as stderr:
        process = subprocess.run([python, str(Path(__file__).resolve()), "--worker", str(report_path), *command],
                                 cwd=case, env=env, stdout=stdout, stderr=stderr)
    wall_seconds = time.perf_counter() - started
    result = json.loads(report_path.read_text(encoding="utf-8"))
    result.update(stage=name, wall_seconds=round(wall_seconds, 6), command=command)
    if process.returncode:
        raise RuntimeError(f"{name} failed ({process.returncode}); see {logs}")
    package_path = Path(result["package_file"]).resolve()
    if installed and (package_path.is_relative_to(ROOT / "src") or result["prefix"] == result["base_prefix"]):
        raise RuntimeError("installed validation must use an isolated venv package, not source imports")
    cli_result = json.loads((logs / f"{name}-stdout.json").read_text(encoding="utf-8"))
    result["execution_status"] = cli_result.get("execution_status")
    result["cache"] = cli_result.get("cache")
    print(f"{name}: {wall_seconds:.3f}s, peak RSS {result['peak_rss_bytes'] / 1048576:.1f} MiB", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--durations", type=int, nargs="+", default=[60, 600, 3600], help="audio seconds per case")
    parser.add_argument("--rate", type=int, choices=[22050, 44100, 48000], default=22050)
    parser.add_argument("--python", default=sys.executable, help="interpreter used by stage workers")
    parser.add_argument("--installed", action="store_true", help="use an installed venv package; forbid source imports")
    args = parser.parse_args()
    args.python = str(Path(shutil.which(args.python) or args.python).resolve())
    if not Path(args.python).is_file():
        parser.error("Python interpreter not found")
    output = Path(args.output).resolve()
    if output.exists() or any(value < 1 for value in args.durations) or len(set(args.durations)) != len(args.durations):
        parser.error("use a new output directory and unique positive durations")
    output.mkdir(parents=True)
    report = {"schema_version": "1", "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": "installed_venv" if args.installed else "source", "machine": machine(),
        "fixture": {"sample_rate": args.rate, "channels": 2, "sample_width_bytes": 2, "seed": 20261109,
                    "description": "8 deterministic second-patterns, anti-phase stereo, silence every 29 seconds"},
        "scope": "PCM create/inspect/acoustic analysis/reuse only; no model, FFmpeg, fusion or hardware timing",
        "harness_sha256": file_hash(__file__), "cases": [], "status": "running"}
    try:
        for seconds in args.durations:
            case = output / f"{seconds}s"
            case.mkdir()
            (case / "logs").mkdir()
            print(f"Generating {seconds}s fixture...", flush=True)
            fixture(case / "inputs", seconds, args.rate)
            entry = {"duration_seconds": seconds, "input_sha256": file_hash(case / "inputs/track.wav"), "stages": []}
            report["cases"].append(entry)
            commands = [
                ("create", ["create", "--config", "inputs/config.yaml", "--output", "project"]),
                ("inspect", ["inspect", "project"]),
                ("analyze", ["analyze", "project", "--output", "analysis"]),
                ("reuse", ["analyze", "project", "--reuse-analysis", "analysis", "--output", "analysis-reused"]),
            ]
            for name, command in commands:
                entry["stages"].append(run_stage(args.python, case, name, command, args.installed))
                write_json(output / "report.json", report)
            if entry["stages"][-1]["cache"]["status"] != "hit":
                raise RuntimeError("reuse benchmark did not hit the verified cache")
            entry["output_bytes"] = {name: tree_bytes(case / name) for name in ("inputs", "project", "analysis", "analysis-reused")}
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "report.json", report)
    print(output / "report.json")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        raise SystemExit(worker(sys.argv[2], sys.argv[3:]))
    main()
