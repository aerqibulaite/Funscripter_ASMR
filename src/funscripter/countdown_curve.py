"""Continuous edging-countdown curve: reference implementation.

Maps a spoken countdown (10 -> 1, optional 0) onto L0 stroke commands with a
smooth, speed-limited period ramp, pause handling and a climax burst at 0.

All maths is integer/millisecond arithmetic so it can be unit tested offline and
then folded into funscripter.controls / funscripter.motion.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    """Device envelope. max_speed is position-units per second."""

    max_speed: float = 300.0
    low: int = 0
    high: int = 100


@dataclass(frozen=True)
class CountdownProfile:
    """One countdown run's motion grammar."""

    base_period_ms: int = 900          # period at coefficient 1.0
    start_coefficient: float = 1.0
    end_coefficient: float = 2.0       # 10 -> 1.0x, 1 -> 2.0x
    max_coefficient: float = 2.0       # hard ceiling, never exceeded
    ease_ms: int = 300                 # per-number ramp into the new period
    low: int = 20
    high: int = 80
    up_ratio: float = 0.5
    climax_duration_ms: int = 3000
    climax_period_factor: float = 0.7  # 1/1.43x on top of the last count
    climax_low: int = 10
    climax_high: int = 95
    climax_center: int = 50
    climax_keep_amplitude: bool = False  # False: trade amplitude for speed
    cooldown_ms: int = 5000


def coefficient(u: float, profile: CountdownProfile) -> float:
    """Geometric interpolation: 10 -> 1.0x, 9 -> 1.08x, ..., 1 -> 2.0x.

    u is normalised countdown progress in [0, 1]:
        u = (start_value - current_value) / (start_value - end_value)
    """
    u = min(max(u, 0.0), 1.0)
    ratio = profile.end_coefficient / profile.start_coefficient
    return min(profile.start_coefficient * ratio ** u, profile.max_coefficient)


def min_period_ms(
    profile: CountdownProfile,
    limits: Limits,
    low: int | None = None,
    high: int | None = None,
    up_ratio: float | None = None,
) -> int:
    """Shortest period whose fast leg still fits the speed limit.

    The fast leg is the shorter fraction of the cycle, so an asymmetric up_ratio
    costs more time than a symmetric one.
    """
    low = profile.low if low is None else low
    high = profile.high if high is None else high
    up_ratio = profile.up_ratio if up_ratio is None else up_ratio
    if not limits.max_speed:
        return 2
    amplitude = max(1, high - low)
    leg = min(up_ratio, 1.0 - up_ratio)
    return max(2, round(1000 * amplitude / (limits.max_speed * leg)))


def period_ms(u: float, profile: CountdownProfile, limits: Limits) -> int:
    """Target stroke period for progress u, already clamped to the device."""
    period = profile.base_period_ms / coefficient(u, profile)
    return max(round(period), min_period_ms(profile, limits))


def stroke_speed(pos_from: int, pos_to: int, period: int, up_ratio: float) -> float:
    """Peak position-units/second for one leg of a stroke."""
    leg_ms = max(1.0, period * (up_ratio if pos_to > pos_from else 1.0 - up_ratio))
    return abs(pos_to - pos_from) * 1000.0 / leg_ms


def advance_position(current: int, goal: int, elapsed_ms: int, max_speed: float) -> int:
    """Move toward `goal` for `elapsed_ms` without ever exceeding max_speed.

    Integer rounding of positions can otherwise push a short leg just over the
    cap, which the device validator would reject.
    """
    budget = math.floor(max_speed * elapsed_ms / 1000 + 1e-9)
    delta = goal - current
    step = min(abs(delta), budget)
    return current + (step if delta > 0 else -step)


def emit_periodic(
    points: list[dict],
    pos: int,
    start_ms: int,
    end_ms: int,
    target_period: int,
    *,
    ease_from: int | None,
    ease_ms: int,
    low: int,
    high: int,
    up_ratio: float,
    limits: Limits,
) -> tuple[int, int]:
    """Append a periodic stroke train to `points` covering [start_ms, end_ms).

    Returns (final position, last full period) so the next segment continues
    without a positional jump.
    """
    time = start_ms
    target = high if pos < high else low
    last_period = target_period
    while time < end_ms:
        if ease_from is not None and ease_ms > 0 and time - start_ms < ease_ms:
            blend = (time - start_ms) / ease_ms
            period = round(ease_from + (target_period - ease_from) * blend)
        else:
            period = target_period
        period = max(period, 2)
        leg = max(1, math.ceil(period * (up_ratio if target > pos else 1.0 - up_ratio)))
        remaining = end_ms - time
        if leg > remaining:
            # Segment boundary: stop mid-leg, record the exact position so the
            # next segment is continuous.
            pos = advance_position(pos, target, int(remaining), limits.max_speed)
            points.append({"at": end_ms, "pos": min(max(pos, low), high)})
            return min(max(pos, low), high), last_period
        pos = advance_position(pos, target, leg, limits.max_speed)
        time += leg
        points.append({"at": time, "pos": min(max(pos, low), high)})
        last_period = period
        if stroke_speed(pos, low if target == high else high, period, up_ratio) > limits.max_speed + 1e-9:
            raise ValueError("stroke exceeds device speed limit")
        target = low if target == high else high
    return pos, last_period


def expand_countdown(
    counts: list[tuple[int, int]],
    start_value: int,
    end_value: int,
    profile: CountdownProfile,
    limits: Limits,
    *,
    paused_windows: list[tuple[int, int]] | None = None,
    climax_at_ms: int | None = None,
) -> dict:
    """Build a funscript body from [(timestamp_ms, spoken_number), ...].

    Timestamps are authoritative: a skipped number advances the coefficient by
    however many steps were skipped, it does not stretch the timing.
    """
    if not counts:
        raise ValueError("no counts")
    if start_value <= end_value:
        raise ValueError("countdown needs start > end")
    if limit := (limits.low, limits.high):
        if not limit[0] < limit[1]:
            raise ValueError("empty device range")

    counts = sorted(counts, key=lambda item: item[0])
    pauses = sorted(paused_windows or [])

    points: list[dict] = [{"at": 0, "pos": profile.low}]
    pos = profile.low
    previous_period: int | None = None
    trace: list[dict] = []
    last_end = 0

    for index, (at_ms, value) in enumerate(counts):
        u = min(max((start_value - value) / (start_value - end_value), 0.0), 1.0)
        target_period = period_ms(u, profile, limits)
        coef = coefficient(u, profile)

        if index + 1 < len(counts):
            span_end = counts[index + 1][0]
        elif climax_at_ms is not None:
            span_end = climax_at_ms
        else:
            span_end = at_ms + target_period
        span_end = max(span_end, at_ms + 1)

        cursor = at_ms
        for pause_start, pause_end in pauses:
            if pause_end <= at_ms or pause_start >= span_end:
                continue
            stop, resume = max(pause_start, at_ms), min(pause_end, span_end)
            if stop > cursor:
                pos, _ = emit_periodic(
                    points, pos, cursor, stop, target_period,
                    ease_from=previous_period, ease_ms=profile.ease_ms,
                    low=profile.low, high=profile.high, up_ratio=profile.up_ratio,
                    limits=limits,
                )
            points.append({"at": stop, "pos": pos})
            points.append({"at": resume, "pos": pos})
            cursor = resume
            previous_period = None
        if cursor < span_end:
            pos, previous_period = emit_periodic(
                points, pos, cursor, span_end, target_period,
                ease_from=previous_period, ease_ms=profile.ease_ms,
                low=profile.low, high=profile.high, up_ratio=profile.up_ratio,
                limits=limits,
            )
        last_end = span_end
        trace.append({
            "value": value,
            "at_ms": at_ms,
            "u": round(u, 3),
            "coefficient": round(coef, 3),
            "period_ms": target_period,
            "speed": round(stroke_speed(profile.low, profile.high, target_period,
                                        profile.up_ratio), 1),
        })

    climax = None
    if climax_at_ms is not None:
        climax_low = max(profile.climax_low, limits.low)
        climax_high = min(profile.climax_high, limits.high)
        wanted = round(period_ms(1.0, profile, limits) * profile.climax_period_factor)
        leg = min(profile.up_ratio, 1.0 - profile.up_ratio)
        widest = int(wanted * limits.max_speed * leg / 1000)
        if profile.climax_keep_amplitude or widest >= climax_high - climax_low:
            applied = max(wanted, min_period_ms(profile, limits, climax_low, climax_high))
            mode = "same_amplitude"
        else:
            # A speed-capped device cannot deliver "faster AND wider" at once.
            # Shorten the stroke around the centre and spend the budget on rate.
            applied = wanted
            amplitude = max(4, widest)
            centre = min(max(profile.climax_center, limits.low), limits.high)
            climax_low = max(limits.low, centre - amplitude // 2)
            climax_high = min(limits.high, climax_low + amplitude)
            climax_low = max(limits.low, climax_high - amplitude)
            mode = "amplitude_capped"
        climax = {
            "mode": mode,
            "requested_period_ms": wanted,
            "applied_period_ms": applied,
            "low": climax_low,
            "high": climax_high,
            "amplitude": climax_high - climax_low,
            "speed": round(stroke_speed(climax_low, climax_high, applied,
                                        profile.up_ratio), 1),
            "duration_ms": profile.climax_duration_ms,
        }
        points.append({"at": climax_at_ms, "pos": pos})
        burst_start = climax_at_ms
        if pos != climax_low:
            # The entry move is a single travel at max speed, not a double-length
            # first stroke.
            travel_ms = max(1, math.ceil(abs(pos - climax_low) * 1000 / limits.max_speed))
            points.append({"at": climax_at_ms + travel_ms, "pos": climax_low})
            burst_start, pos = climax_at_ms + travel_ms, climax_low
        burst_end = burst_start + profile.climax_duration_ms
        pos, _ = emit_periodic(
            points, pos, burst_start, burst_end, applied,
            ease_from=None, ease_ms=0,
            low=climax_low, high=climax_high, up_ratio=profile.up_ratio,
            limits=limits,
        )
        points.append({"at": burst_end, "pos": pos})
        last_end = burst_end

    points.append({"at": last_end + max(profile.cooldown_ms, 1), "pos": pos})

    merged: dict[int, int] = {}
    for point in points:
        merged[point["at"]] = point["pos"]
    actions = [{"at": at, "pos": merged[at]} for at in sorted(merged)]
    return {
        "script": {"version": "1.0", "inverted": False, "range": 100, "actions": actions},
        "trace": trace,
        "climax": climax,
        "final_position": pos,
    }


def expand_repeats(
    start_ms: int,
    count: int,
    base_period_ms: int,
    profile: CountdownProfile,
    limits: Limits,
    *,
    unit: str = "single_travel",
    start_pos: int | None = None,
    finish_accelerate: float = 0.87,
) -> dict:
    """N counted strokes, with the final two slightly faster.

    count = strokes; unit "round_trip" treats one count as a there-and-back pair
    of legs, so the count stays what the speaker said.
    """
    if count < 1:
        raise ValueError("count must be positive")
    if unit not in ("single_travel", "round_trip"):
        raise ValueError("unit must be single_travel or round_trip")
    legs = count * (2 if unit == "round_trip" else 1)
    legs_per_count = 2 if unit == "round_trip" else 1
    pos = profile.low if start_pos is None else start_pos
    points = [{"at": start_ms, "pos": pos}]
    time, last_period = start_ms, base_period_ms
    for leg_index in range(legs):
        stroke = leg_index // legs_per_count
        fast = stroke >= count - 2
        period = max(round(base_period_ms * (finish_accelerate if fast else 1.0)),
                     min_period_ms(profile, limits))
        target = profile.high if pos < profile.high else profile.low
        leg = max(1, math.ceil(period * (profile.up_ratio if target > pos else 1.0 - profile.up_ratio)))
        pos = advance_position(pos, target, leg, limits.max_speed)
        time += leg
        points.append({"at": time, "pos": pos})
        last_period = period
    return {
        "script": {"version": "1.0", "inverted": False, "range": 100, "actions": points},
        "legs": legs,
        "count": count,
        "unit": unit,
        "end_ms": time,
        "last_period_ms": last_period,
    }


def expand_rhythm(
    anchors_ms: list[int],
    profile: CountdownProfile,
    limits: Limits,
    *,
    start_pos: int | None = None,
) -> dict:
    """One anchor (syllable onset) per stroke leg.

    A beat too short for the device is held, never replayed: the position stays
    put and the missed beat is reported, which keeps audio and motion aligned.
    """
    if not anchors_ms:
        raise ValueError("no anchors")
    anchors = sorted(anchors_ms)
    pos = profile.low if start_pos is None else start_pos
    points = [{"at": anchors[0], "pos": pos}]
    skipped: list[int] = []
    for previous, at in zip(anchors, anchors[1:]):
        interval = at - previous
        target = profile.high if pos < profile.high else profile.low
        needed_ms = abs(target - pos) * 1000 / limits.max_speed
        if needed_ms > interval:
            skipped.append(at)
            points.append({"at": at, "pos": pos})
            continue
        pos = target
        points.append({"at": at, "pos": pos})
    return {
        "script": {"version": "1.0", "inverted": False, "range": 100, "actions": points},
        "beats": len(anchors) - 1,
        "skipped_beats": skipped,
        "end_ms": anchors[-1],
    }


def validate(actions: list[dict], duration_ms: int, limits: Limits) -> dict:
    """Same contract as funscripter.motion.validate_actions, plus a speed pass."""
    errors = []
    previous = None
    for point in actions:
        at, pos = point["at"], point["pos"]
        if not isinstance(at, int) or not 0 <= at <= duration_ms:
            errors.append("invalid_time")
        if not isinstance(pos, int) or not 0 <= pos <= 100:
            errors.append("invalid_position")
        if not limits.low <= pos <= limits.high:
            errors.append("device_position_range")
        if previous is not None:
            delta = at - previous["at"]
            if delta <= 0:
                errors.append("non_increasing_time")
            elif abs(pos - previous["pos"]) * 1000 / delta > limits.max_speed + 1e-9:
                errors.append("device_speed_limit")
        previous = point
    return {"format_valid": not errors, "constraint_errors": sorted(set(errors))}


def countdown_10_to_1(step_ms: int = 2000) -> list[tuple[int, int]]:
    return [(index * step_ms, 10 - index) for index in range(10)]


if __name__ == "__main__":
    profile = CountdownProfile()
    limits = Limits(max_speed=300)
    counts = countdown_10_to_1()
    result = expand_countdown(counts, 10, 1, profile, limits, climax_at_ms=10 * 2000)
    print(f"{'n':>3} {'u':>5} {'coef':>5} {'period':>7} {'pos/s':>7}")
    for row in result["trace"]:
        print(f"{row['value']:>3} {row['u']:>5} {row['coefficient']:>5} "
              f"{row['period_ms']:>7} {row['speed']:>7}")
    print("min_period_ms:", min_period_ms(profile, limits))
    print("climax:", result["climax"])
    print("actions:", len(result["script"]["actions"]))
    print("validate:", validate(result["script"]["actions"], 30_000, limits))
