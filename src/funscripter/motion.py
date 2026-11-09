def validate_actions(actions, duration_ms, device):
    errors = []
    previous = None
    for point in actions:
        time, pos = point["at"], point["pos"]
        if type(time) is not int or not 0 <= time <= duration_ms:
            errors.append("invalid_time")
        if type(pos) is not int or not 0 <= pos <= 100:
            errors.append("invalid_position")
        if device.allowed_position_range is not None:
            low, high = device.allowed_position_range
            if not low <= pos <= high:
                errors.append("device_position_range")
        if previous is not None:
            delta = time - previous["at"]
            if delta <= 0:
                errors.append("non_increasing_time")
            elif device.max_speed_pos_per_sec is not None:
                speed = abs(pos - previous["pos"]) * 1000 / delta
                if speed > device.max_speed_pos_per_sec + 1e-9:
                    errors.append("device_speed_limit")
        previous = point
    resolved = device.allowed_position_range is not None and device.max_speed_pos_per_sec is not None
    return {"format_valid": not any(e in errors for e in
                ["invalid_time", "invalid_position", "non_increasing_time"]),
            "constraint_errors": sorted(set(errors)),
            "device_constraints_checked": resolved,
            "device_constraints_passed": resolved and not errors,
            "interpolation": device.final_interpolation,
            "status": "offline_draft_needs_review"}


def generate(config, duration_ms, *, allow_constraint_errors=False):
    generation = config.generation
    segments = sorted(generation.segments, key=lambda s: s.start_ms)
    previous_end, ids = 0, set()
    for segment in segments:
        if segment.segment_id in ids:
            raise ValueError("duplicate segment_id")
        ids.add(segment.segment_id)
        if not previous_end <= segment.start_ms < segment.end_ms <= duration_ms:
            raise ValueError("segments must be non-overlapping and inside audio")
        if segment.preset_id not in generation.presets:
            raise ValueError(f"unknown preset: {segment.preset_id}")
        previous_end = segment.end_ms
    points = {0: generation.initial_position}
    current = generation.initial_position
    for segment in segments:
        preset = generation.presets[segment.preset_id]
        # Start at the current position. Reach high then low without moving time anchors.
        points[segment.start_ms] = current
        up = round(preset.period_ms * preset.up_time_ratio)
        down = preset.period_ms - up
        time, target, step = segment.start_ms, preset.high, up
        if current >= preset.high:
            target, step = preset.low, down
        while time < segment.end_ms:
            remaining = segment.end_ms - time
            elapsed = min(step, remaining)
            position = round(current + (target - current) * elapsed / step)
            time += elapsed
            points[time] = position
            current = position
            if target == preset.high:
                target, step = preset.low, down
            else:
                target, step = preset.high, up
    # Explicit same-position boundary points prevent motion through unassigned gaps.
    points[duration_ms] = current
    actions = [{"at": time, "pos": pos} for time, pos in sorted(points.items())]
    report = validate_actions(actions, duration_ms, config.device)
    if report["constraint_errors"] and not allow_constraint_errors:
        raise ValueError("draft violates constraints: " + ", ".join(report["constraint_errors"]))
    return {"version": "1.0", "inverted": False, "range": 100, "actions": actions}, report
