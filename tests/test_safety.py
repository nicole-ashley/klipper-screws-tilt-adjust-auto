import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_one_by_one import CommandError, FakeGcmd, is_level, make  # noqa: E402
from test_continuous import make_continuous  # noqa: E402


def motor_writes(obj):
    return [w for w in obj.last_board.writes if w != "0"]


class PerMoveLimitTest(unittest.TestCase):
    def test_big_turn_is_refused_before_any_motor_moves(self):
        # Screw 2 is 1mm low, which is 2 turns of an M3 screw
        for params in ({}, {"ONE_BY_ONE": 1}, {"CONTINUOUS": 1}):
            obj, bed, gcode, probe, toolhead = make_continuous([0., 1., 0.1])
            with self.assertRaises(CommandError) as ctx:
                obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(params))
            self.assertIn("max_turns_per_move", str(ctx.exception))
            self.assertEqual(bed.turns, [0., 0., 0.])
            self.assertEqual(motor_writes(obj), [])

    def test_limit_can_be_raised(self):
        obj, bed, gcode, sta = make([0., 1., 0.1], config={"max_turns_per_move": 3})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertTrue(is_level(bed))


class PerRunLimitTest(unittest.TestCase):
    def test_turns_add_up_across_attempts(self):
        # Half-efficient motor: 0.8 turns, then 0.4 more, is over 1 turn
        obj, bed, gcode, sta = make([0., 0.4, 0.], efficiency=0.5,
                                    config={"max_turns_per_run": 1})
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))
        self.assertIn("max_turns_per_run", str(ctx.exception))
        self.assertAlmostEqual(bed.turns[1], -0.8)
        self.assertEqual(obj.last_board.writes[-1], "0")

    def test_back_and_forth_counts_both_ways(self):
        obj, bed, gcode, probe, toolhead = make_continuous(
            [0., 0.2, 0.], efficiency=1.9, config={"max_turns_per_run": 0.8})
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"CONTINUOUS": 1}))
        self.assertIn("max_turns_per_run", str(ctx.exception))

    def test_counts_across_full_measurements(self):
        obj, bed, gcode, sta = make([0., 0.4, 0.], efficiency=0.5,
                                    config={"max_turns_per_run": 1})
        # Each pass leaves the screw short, so turns keep adding up
        with self.assertRaises(CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())

    def test_resets_between_runs(self):
        obj, bed, gcode, sta = make([0., 0.4, 0.], config={"max_turns_per_run": 1})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        bed.heights[1] = 0.4
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertTrue(is_level(bed))


class ProgressCheckTest(unittest.TestCase):
    def test_motor_turning_the_wrong_way_stops(self):
        for params in ({"ONE_BY_ONE": 1}, {"CONTINUOUS": 1}):
            obj, bed, gcode, probe, toolhead = make_continuous([0., 0.2, -0.1], efficiency=-1.)
            with self.assertRaises(CommandError) as ctx:
                obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(params))
            self.assertIn("further from level", str(ctx.exception))
            # Stopped after the first bad turn
            self.assertEqual(motor_writes(obj), ["-2"])
            self.assertEqual(obj.last_board.writes[-1], "0")

    def test_wrong_way_in_default_mode_stops_after_one_round(self):
        obj, bed, gcode, sta = make([0., 0.2, -0.1], efficiency=-1.)
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertIn("further from level", str(ctx.exception))
        self.assertEqual(sum(s.startswith("probed") for s in gcode.scripts), 2)

    def test_motor_that_does_not_move_stops(self):
        for params in ({"ONE_BY_ONE": 1}, {"CONTINUOUS": 1}):
            obj, bed, gcode, probe, toolhead = make_continuous([0., 0.2, -0.1], efficiency=0.)
            with self.assertRaises(CommandError) as ctx:
                obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(params))
            self.assertIn("barely moved", str(ctx.exception))
            self.assertEqual(motor_writes(obj), ["-2", "-2"])

    def test_overshoot_that_still_gets_closer_is_fine(self):
        obj, bed, gcode, sta = make([0., 0.2, -0.1], efficiency=1.6)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))
        self.assertTrue(is_level(bed))

    def test_small_noise_is_tolerated(self):
        obj, bed, gcode, sta = make([0., 0.01, 0.])
        obj.heights = [0., 0.01, 0.]
        self.assertEqual(obj._check_bed_got_closer(0.), 0.01)
        obj.turns_per_mm = 2.
        # 0.01mm further away is within reading_tolerance
        self.assertEqual(obj._check_screw_progress(1, 0.01, 0., 0.01, 0.02, 0), 0)


class TurnsPerMmCheckTest(unittest.TestCase):
    def learn(self, lines, thread=None):
        obj, bed, gcode, sta = make([0., 0., 0.])
        if thread is not None:
            sta.thread = thread
        obj.active = True
        for line in lines:
            obj._handle_output(line)
        obj._learn_turns_per_mm()
        return obj

    M3 = ["a (base) : x=0, y=0, z=1.00000",
          "b : x=0, y=0, z=0.75000 : adjust CW 00:30",
          "c : x=0, y=0, z=1.10000 : adjust CCW 00:12"]

    def test_impossible_rate_is_an_error(self):
        with self.assertRaises(CommandError) as ctx:
            self.learn(["a (base) : x=0, y=0, z=1.00000",
                        "b : x=0, y=0, z=0.90000 : adjust CW 01:00"])
        self.assertIn("no supported screw thread", str(ctx.exception))

    def test_uses_screw_thread_when_it_agrees(self):
        obj = self.learn(["a (base) : x=0, y=0, z=1.00000",
                          "b : x=0, y=0, z=0.74000 : adjust CW 00:31"], thread=0)
        self.assertEqual(obj.turns_per_mm, 2.)

    def test_ccw_thread(self):
        obj = self.learn(["a (base) : x=0, y=0, z=1.00000",
                          "b : x=0, y=0, z=0.65000 : adjust CCW 00:30"], thread=3)
        self.assertAlmostEqual(obj.turns_per_mm, -1 / 0.7)

    def test_wrong_direction_for_screw_thread_is_an_error(self):
        with self.assertRaises(CommandError) as ctx:
            self.learn(self.M3, thread=1)
        self.assertIn("screw_thread", str(ctx.exception))

    def test_tiny_tilt_is_not_compared(self):
        # Rounding to the minute makes small tilts learn a poor rate
        obj = self.learn(["a (base) : x=0, y=0, z=1.00000",
                          "b : x=0, y=0, z=0.99000 : adjust CW 00:01"], thread=6)
        self.assertEqual(obj.turns_per_mm, 1.)


class MaxDeviationTest(unittest.TestCase):
    def test_default_and_configured(self):
        obj, bed, gcode, sta = make([0., 0., 0.])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertIn("SCREWS_TILT_CALCULATE MAX_DEVIATION=2.0", gcode.scripts)
        obj, bed, gcode, sta = make([0., 0., 0.], config={"max_deviation": 0.8})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertIn("SCREWS_TILT_CALCULATE MAX_DEVIATION=0.8", gcode.scripts)


class ManualLimitTest(unittest.TestCase):
    def test_big_manual_turn_needs_force(self):
        obj, bed, gcode, sta = make([0., 0., 0.])
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_MANUAL(FakeGcmd({"SCREW": 2, "DISTANCE": -1.5}))
        self.assertIn("FORCE=1", str(ctx.exception))
        self.assertFalse(hasattr(obj, "last_board"))
        obj.cmd_SCREWS_TILT_ADJUST_MANUAL(FakeGcmd({"SCREW": 2, "DISTANCE": -1.5, "FORCE": 1}))
        self.assertAlmostEqual(bed.turns[1], -1.5)

    def test_manual_turns_do_not_count_towards_a_run(self):
        obj, bed, gcode, sta = make([0., 0., 0.], config={"max_turns_per_run": 1})
        obj.cmd_SCREWS_TILT_ADJUST_MANUAL(FakeGcmd({"SCREW": 2, "DISTANCE": 1}))
        obj.cmd_SCREWS_TILT_ADJUST_MANUAL(FakeGcmd({"SCREW": 2, "DISTANCE": 1}))
        self.assertAlmostEqual(bed.turns[1], 2.)


class KeepaliveTest(unittest.TestCase):
    def test_long_turns_keep_telling_the_board_to_run(self):
        obj, bed, gcode, sta = make([0., 0., 0.], config={"full_turn_time_in_seconds": 10})
        obj.cmd_SCREWS_TILT_ADJUST_MANUAL(FakeGcmd({"SCREW": 2, "DISTANCE": 0.5}))
        self.assertEqual(obj.last_board.writes, ["2", "2", "2", "0", "0"])
        self.assertAlmostEqual(obj.reactor.now, 5.)


if __name__ == "__main__":
    unittest.main()
