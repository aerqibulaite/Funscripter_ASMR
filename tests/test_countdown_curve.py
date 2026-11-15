"""Checks for the countdown curve: shape, clamping, pauses, climax."""
import itertools
import unittest

from funscripter.countdown_curve import (
    CountdownProfile,
    Limits,
    coefficient,
    countdown_10_to_1,
    expand_countdown,
    expand_repeats,
    expand_rhythm,
    min_period_ms,
    period_ms,
    stroke_speed,
    validate,
)


def expand(profile=None, limits=None, counts=None, **kwargs):
    profile = profile or CountdownProfile()
    limits = limits or Limits(max_speed=300)
    return expand_countdown(counts or countdown_10_to_1(), 10, 1, profile, limits, **kwargs)


class CurveMathTests(unittest.TestCase):
    def test_coefficient_is_one_then_two(self):
        profile = CountdownProfile()
        self.assertAlmostEqual(coefficient(0.0, profile), 1.0)
        self.assertAlmostEqual(coefficient(1.0, profile), 2.0)

    def test_each_spoken_number_adds_about_eight_percent(self):
        profile = CountdownProfile()
        values = [coefficient(i / 9, profile) for i in range(10)]
        steps = [b / a for a, b in itertools.pairwise(values)]
        self.assertTrue(all(1.07 < step < 1.09 for step in steps), steps)
        self.assertAlmostEqual(values[-1] / values[0], 2.0)

    def test_period_shrinks_monotonically(self):
        profile, limits = CountdownProfile(), Limits(max_speed=300)
        periods = [period_ms(i / 9, profile, limits) for i in range(10)]
        self.assertTrue(all(a > b for a, b in itertools.pairwise(periods)), periods)
        self.assertEqual(periods[0], 900)
        self.assertEqual(periods[-1], 450)

    def test_speed_limit_binds_at_a_slow_device(self):
        profile, limits = CountdownProfile(), Limits(max_speed=120)
        floor = min_period_ms(profile, limits)
        self.assertEqual(period_ms(1.0, profile, limits), floor)
        self.assertLessEqual(stroke_speed(20, 80, floor, profile.up_ratio), 120 + 1e-9)

    def test_asymmetric_strokes_need_a_longer_period(self):
        self.assertGreater(min_period_ms(CountdownProfile(up_ratio=0.3), Limits(300)),
                           min_period_ms(CountdownProfile(), Limits(300)))


class ExpansionTests(unittest.TestCase):
    def test_script_respects_device_limit_at_every_speed(self):
        for max_speed in (80, 150, 300, 500):
            limits = Limits(max_speed=max_speed)
            result = expand(limits=limits, climax_at_ms=20_000)
            report = validate(result["script"]["actions"], 30_000, limits)
            self.assertTrue(report["format_valid"], (max_speed, report["constraint_errors"]))

    def test_timeline_never_goes_backwards(self):
        times = [point["at"] for point in expand(climax_at_ms=20_000)["script"]["actions"]]
        self.assertEqual(times, sorted(set(times)))

    def test_motion_pauses_completely_inside_the_window(self):
        result = expand(paused_windows=[(3_000, 5_000)])
        actions = result["script"]["actions"]
        held = [p for p in actions if 3_000 <= p["at"] <= 5_000]
        self.assertTrue(held)
        self.assertEqual(len({p["pos"] for p in held}), 1, held)

    def test_resume_does_not_replay_missed_beats(self):
        plain = expand()["script"]["actions"]
        paused = expand(paused_windows=[(3_000, 5_000)])["script"]["actions"]
        self.assertGreaterEqual(max(p["at"] for p in paused), max(p["at"] for p in plain))

    def test_skipped_number_still_advances_the_coefficient(self):
        counts = [(0, 10), (2_000, 9), (4_000, 7), (6_000, 6)]
        trace = {row["value"]: row for row in expand(counts=counts, climax_at_ms=8_000)["trace"]}
        self.assertAlmostEqual(trace[7]["u"], 3 / 9, places=3)
        self.assertLess(trace[7]["period_ms"], trace[9]["period_ms"])

    def test_climax_is_faster_than_the_last_count_and_speed_capped(self):
        limits = Limits(max_speed=300)
        climax = expand(limits=limits, climax_at_ms=20_000)["climax"]
        self.assertLess(climax["applied_period_ms"], 450)
        self.assertLessEqual(climax["speed"], limits.max_speed)
        self.assertEqual(climax["mode"], "amplitude_capped")
        self.assertLess(climax["high"] - climax["low"], 85)

    def test_keeping_amplitude_slows_the_burst_instead(self):
        limits = Limits(max_speed=300)
        result = expand(CountdownProfile(climax_keep_amplitude=True), limits, climax_at_ms=20_000)
        self.assertEqual(result["climax"]["mode"], "same_amplitude")
        self.assertGreaterEqual(result["climax"]["applied_period_ms"], 315)
        self.assertTrue(validate(result["script"]["actions"], 30_000, limits)["format_valid"])

    def test_tail_holds_the_final_position_through_cooldown(self):
        profile = CountdownProfile()
        result = expand(profile, climax_at_ms=20_000)
        tail = [p for p in result["script"]["actions"] if p["at"] >= 20_000 + profile.climax_duration_ms]
        self.assertTrue(tail)
        self.assertEqual(len({p["pos"] for p in tail}), 1)
        self.assertGreaterEqual(tail[-1]["at"] - tail[0]["at"], profile.cooldown_ms)

    def test_no_climax_still_ends_cleanly(self):
        limits = Limits(max_speed=300)
        result = expand(limits=limits)
        self.assertIsNone(result["climax"])
        self.assertTrue(validate(result["script"]["actions"], 30_000, limits)["format_valid"])

    def test_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            expand_countdown([], 10, 1, CountdownProfile(), Limits(300))
        with self.assertRaises(ValueError):
            expand_countdown([(0, 5)], 5, 5, CountdownProfile(), Limits(300))


class GameplayPatternTests(unittest.TestCase):
    def test_counted_strokes_emit_exactly_the_spoken_count(self):
        profile, limits = CountdownProfile(), Limits(max_speed=300)
        result = expand_repeats(0, 5, 700, profile, limits)
        actions = result["script"]["actions"]
        self.assertEqual(len(actions) - 1, 5)
        self.assertTrue(validate(actions, 5_000, limits)["format_valid"])
        self.assertLess(result["last_period_ms"], 700)

    def test_round_trip_count_still_matches_the_spoken_number(self):
        profile, limits = CountdownProfile(), Limits(max_speed=300)
        result = expand_repeats(0, 3, 700, profile, limits, unit="round_trip")
        self.assertEqual(result["legs"], 6)
        self.assertEqual(result["count"], 3)

    def test_rhythm_puts_one_edge_on_every_beat(self):
        profile, limits = CountdownProfile(), Limits(max_speed=300)
        anchors = [0, 400, 800, 1_200]
        result = expand_rhythm(anchors, profile, limits)
        self.assertEqual([p["at"] for p in result["script"]["actions"]], anchors)
        positions = [p["pos"] for p in result["script"]["actions"]]
        self.assertEqual(len(set(positions)), 2)
        self.assertTrue(validate(result["script"]["actions"], 2_000, limits)["format_valid"])

    def test_rhythm_holds_instead_of_teleporting_on_a_fast_beat(self):
        profile, limits = CountdownProfile(), Limits(max_speed=300)
        anchors = [0, 400, 420, 800]
        result = expand_rhythm(anchors, profile, limits)
        self.assertEqual(result["skipped_beats"], [420])
        held = [p for p in result["script"]["actions"] if p["at"] in (400, 420)]
        self.assertEqual(held[0]["pos"], held[1]["pos"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
