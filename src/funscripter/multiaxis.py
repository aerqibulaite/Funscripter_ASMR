"""Offline axis contracts and numeric fixtures; no model or device control."""
import json
import uuid
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal

import yaml
from pydantic import Field, model_validator

from .models import Model, DeviceConfig, GenerationConfig
from .motion import generate, validate_actions
from .project import atomic_json, reopen_project
from .media import sha256
from .revision_diff import value_on_grid
from .revision_workflow import stage_output, write_manifest

AxisName = Literal["L0", "L1", "L2", "R0", "R1", "R2"]
Position = Annotated[int, Field(ge=0, le=100)]
SUFFIXES = {"L0": "", "L1": ".surge", "L2": ".sway", "R0": ".twist", "R1": ".roll", "R2": ".pitch"}


class AxisLink(Model):
    source_axis: AxisName
    scale: float = 1.0
    offset: float = 0.0


class AxisDraft(Model):
    enabled: bool = False
    mode: Literal["independent", "linked"] = "independent"
    generation: GenerationConfig | None = None
    link: AxisLink | None = None
    device: DeviceConfig = Field(default_factory=DeviceConfig)
    calibration_reference: str | None = None

    @model_validator(mode="after")
    def unambiguous(self):
        if self.mode == "independent" and self.link is not None:
            raise ValueError("independent axis cannot have a link")
        if self.mode == "linked" and self.generation is not None:
            raise ValueError("linked axis cannot have independent generation settings")
        if self.enabled and ((self.mode == "independent" and self.generation is None)
                             or (self.mode == "linked" and self.link is None)):
            raise ValueError("enabled axis requires generation settings or an explicit link")
        return self


class PositionBudget(Model):
    rule_id: str = Field(min_length=1)
    references: dict[AxisName, Position] = Field(min_length=2)
    max_total_deviation: float = Field(ge=0)


class MultiAxisProfile(Model):
    schema_version: Literal["1"] = "1"
    basename: str = Field(default="track", pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    axes: dict[AxisName, AxisDraft]
    position_budgets: list[PositionBudget] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_graph(self):
        reserved = {"CON", "PRN", "AUX", "NUL", *[f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)]}
        if self.basename.upper() in reserved:
            raise ValueError("basename is a reserved portable filename")
        enabled = {axis for axis, settings in self.axes.items() if settings.enabled}
        if not enabled:
            raise ValueError("explicitly enable at least one axis")
        visiting, finished = set(), set()
        def visit(axis):
            if axis in visiting:
                raise ValueError("axis link cycle")
            if axis in finished:
                return
            visiting.add(axis)
            settings = self.axes[axis]
            if settings.mode == "linked":
                source = settings.link.source_axis
                if source not in enabled:
                    raise ValueError("linked source must be an enabled axis")
                visit(source)
            visiting.remove(axis)
            finished.add(axis)
        for axis in enabled:
            visit(axis)
        if len({rule.rule_id for rule in self.position_budgets}) != len(self.position_budgets):
            raise ValueError("position budget IDs must be unique")
        if any(set(rule.references) - enabled for rule in self.position_budgets):
            raise ValueError("position budget references a disabled or absent axis")
        return self


def effective_device(axis, requested, source_device):
    if axis != "L0":
        return requested.model_copy(deep=True)
    # Existing project L0 limits may be tightened, never silently widened.
    bounds = [device.allowed_position_range for device in (requested, source_device)
              if device.allowed_position_range is not None]
    allowed = (max(bound[0] for bound in bounds), min(bound[1] for bound in bounds)) if bounds else None
    if allowed and allowed[0] >= allowed[1]:
        raise ValueError("L0 profile/project position ranges conflict")
    speeds = [device.max_speed_pos_per_sec for device in (requested, source_device)
              if device.max_speed_pos_per_sec is not None]
    return DeviceConfig(allowed_position_range=allowed, max_speed_pos_per_sec=min(speeds) if speeds else None)


def check_budgets(profile, curves, duration_ms):
    results = []
    for rule in profile.position_budgets:
        if any(curves.get(axis) is None for axis in rule.references):
            results.append({"rule_id": rule.rule_id, "passed": False, "error": "axis_unavailable"})
            continue
        actions = {axis: curves[axis]["actions"] for axis in rule.references}
        times = {axis: [point["at"] for point in points] for axis, points in actions.items()}
        knots = sorted({0, duration_ms, *(time for values in times.values() for time in values)})
        maximum, peak_time = Fraction(-1), 0
        for time in knots:
            deviation = sum(abs(value_on_grid(actions[axis], times[axis], time) - reference)
                            for axis, reference in rule.references.items())
            if deviation > maximum:
                maximum, peak_time = deviation, time
        # Sum of absolute affine functions is convex between the union of knots:
        # its maximum on each interval is attained at an endpoint.
        results.append({"rule_id": rule.rule_id, "passed": maximum <= Fraction(str(rule.max_total_deviation)),
            "maximum_total_deviation": float(maximum), "maximum_exact": str(maximum), "at_ms": peak_time,
            "limit": rule.max_total_deviation, "checked_knots": len(knots),
            "method": "exact_rational_union_knots_linear_interpolation"})
    return results


def build_draft(profile, duration_ms, source_device):
    curves, reports = {}, {}
    def build(axis):
        if axis in reports:
            return
        settings = profile.axes[axis]
        if not settings.enabled:
            return
        device = None
        try:
            device = effective_device(axis, settings.device, source_device)
            if settings.mode == "independent":
                script, report = generate(SimpleNamespace(generation=settings.generation, device=device),
                                          duration_ms, allow_constraint_errors=True)
            else:
                source = settings.link.source_axis
                build(source)
                if curves.get(source) is None:
                    raise ValueError("linked source curve unavailable")
                scale, offset = Fraction(str(settings.link.scale)), Fraction(str(settings.link.offset))
                script = {"version": "1.0", "inverted": False, "range": 100,
                    "actions": [{"at": point["at"], "pos": round(offset + scale * point["pos"])}
                                for point in curves[source]["actions"]]}
                report = validate_actions(script["actions"], duration_ms, device)
            curves[axis] = script
            reports[axis] = {**report, "mode": settings.mode,
                "source_axis": settings.link.source_axis if settings.link else None,
                "effective_device": device.model_dump(mode="json")}
        except (ValueError, ArithmeticError) as exc:
            curves[axis] = None
            reports[axis] = {"format_valid": False, "constraint_errors": ["axis_generation_failed"],
                "mode": settings.mode, "error": str(exc), "device_constraints_checked": False,
                "effective_device": device.model_dump(mode="json") if device else None}
    for axis in SUFFIXES:
        if axis in profile.axes:
            build(axis)
    budgets = check_budgets(profile, {axis: curve if reports[axis]["format_valid"] else None
                                     for axis, curve in curves.items()}, duration_ms)
    blocked = any(report["constraint_errors"] for report in reports.values()) or any(not rule["passed"] for rule in budgets)
    return curves, {"schema_version": "1", "duration_ms": duration_ms, "interpolation": "linear",
        "axis_reports": reports, "position_budgets": budgets, "export_blocked": blocked,
        "per_axis_limits_fully_configured": all(report.get("device_constraints_checked", False) for report in reports.values()),
        "axis_combination_hardware_verified": False, "playback_verified": False}


def export_multiaxis_draft(directory, profile_path, output):
    directory, profile_path = Path(directory).resolve(), Path(profile_path).resolve()
    project, _, media, _ = reopen_project(directory)
    overrides = json.loads((directory / project.artifacts["overrides"]).read_text(encoding="utf-8"))
    if overrides.get("overrides") or overrides.get("locked_ranges"):
        raise ValueError("manual edits/locks require revision protection; offline multi-axis draft refused")
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid multi-axis YAML: {exc}") from None
    profile = MultiAxisProfile.model_validate_json(json.dumps(raw, allow_nan=False))
    curves, validation = build_draft(profile, media["duration_ms"], project.config.device)
    with stage_output(output, [directory, profile_path]) as stage:
        atomic_json(stage / "profile.resolved.json", profile.model_dump(mode="json"))
        atomic_json(stage / "candidate_curves.json", curves)
        atomic_json(stage / "validation.json", validation)
        files = {}
        if not validation["export_blocked"]:
            for axis in SUFFIXES:
                if axis in curves:
                    filename = profile.basename + SUFFIXES[axis] + ".funscript"
                    atomic_json(stage / filename, curves[axis])
                    files[axis] = filename
        manifest = {"schema_version": "1", "status": "failed" if validation["export_blocked"] else "needs_review",
            "revision_id": str(uuid.uuid4()),
            "purpose": "offline_axis_contract_fixture", "human_review_required": True,
            "project_id": project.project_id, "source_revision_id": project.revision_id,
            "duration_ms": media["duration_ms"], "time_origin": "saved_analysis_audio_sample_zero",
            "analysis_audio_sha256": project.artifact_hashes["analysis_audio"], "profile_sha256": sha256(profile_path),
            "semantic_plan_applied": False, "enabled_axes": [axis for axis in SUFFIXES if axis in curves],
            "disabled_or_absent_axes": [axis for axis in SUFFIXES if axis not in curves],
            "axis_files": files, "script_exported": bool(files), "export_blocked": validation["export_blocked"],
            "playback_verified": False, "hardware_verified": False}
        # The generic revision helper adds its own fingerprints; include this module explicitly.
        manifest["axis_implementation_hashes"] = {name: sha256(Path(__file__).with_name(name)) for name in
                                                 ("multiaxis.py", "motion.py", "models.py", "revision_diff.py")}
        return write_manifest(stage, "multiaxis_manifest.json", manifest)
