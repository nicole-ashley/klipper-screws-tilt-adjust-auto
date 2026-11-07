import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_one_by_one import CommandError, FakeGcmd, make  # noqa: E402
from test_continuous import make_continuous  # noqa: E402
from test_calibration import make_sim, uneven  # noqa: E402


def wire(bed, ports=None, reversed_=(), dead=(), one_way=(), gain=1.):
    """Wire the motors up wrongly: `ports` maps a motor to the screw it
    really turns, `reversed_` motors turn backwards, `dead` ones don't move,
    `one_way` ones only move clockwise, and every move is `gain` times as
    far as asked."""
    turn = bed.turn
    ports = ports or {}

    def wired(index, turns):
        if index in dead or (index in one_way and turns < 0):
            return
        if index in reversed_:
            turns = -turns
        turn(ports.get(index, index), turns * gain)
    bed.turn = wired


def full(heights=(0., 0.1, -0.05, 0.02), **wiring):
    obj, bed, gcode, sta = make(list(heights))
    gcode.responses = []
    gcode.respond_info = gcode.responses.append
    sta.thread = 0  # CW-M3, as the fake bed moves
    wire(bed, **wiring)
    return obj, bed, gcode


def continuous(heights=(0., 0.1, -0.05, 0.02), **wiring):
    obj, bed, gcode, probe, toolhead = make_continuous(list(heights))
    obj.printer.objects["screws_tilt_adjust"].thread = 0
    wire(bed, **wiring)
    return obj, bed, gcode, probe


def measurements(gcode):
    return [s for s in gcode.scripts if s.startswith("probed")]


class FullBedTest(unittest.TestCase):
    def run_test(self, obj, params=None):
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_TEST(FakeGcmd(params))

    def assert_fails(self, obj, bed, text):
        start = list(bed.heights)
        with self.assertRaises(CommandError) as cm:
            self.run_test(obj)
        self.assertIn(text, str(cm.exception))
        # The faulty screw is put back before stopping
        for a, b in zip(bed.heights, start):
            self.assertAlmostEqual(a, b, places=6)
        self.assertIsNone(obj.board)

    def test_passes_when_wired_right(self):
        obj, bed, gcode = full()
        start = list(bed.heights)
        self.run_test(obj)
        for a, b in zip(bed.heights, start):
            self.assertAlmostEqual(a, b, places=6)
        # One measurement to start, then one after each turn out and back
        self.assertEqual(measurements(gcode), ["probed 1,2,3,4"] * 9)
        self.assertEqual(gcode.scripts[-1], "G28 Z")
        self.assertIsNone(obj.board)

    def test_each_motor_turns_a_little_each_way(self):
        obj, bed, gcode = full()
        self.run_test(obj)
        for turns in bed.turns:
            self.assertAlmostEqual(turns, 0.)
        commands = [w for w in obj.last_board.writes if w != "0"]
        self.assertEqual(sorted(set(commands)), sorted(["1", "-1", "2", "-2", "3", "-3", "4", "-4"]))

    def test_reversed_motor(self):
        obj, bed, gcode = full(reversed_={1})
        self.assert_fails(obj, bed, "Motor 2 turns screw 2 the wrong way")

    def test_motors_in_the_wrong_ports(self):
        obj, bed, gcode = full(ports={1: 2, 2: 1})
        self.assert_fails(obj, bed, "Motor 2 moved screw 3 instead of screw 2")

    def test_motor_that_does_not_move(self):
        obj, bed, gcode = full(dead={2})
        self.assert_fails(obj, bed, "Screw 3 barely moved when motor 3 turned")

    def test_motor_that_only_turns_one_way(self):
        obj, bed, gcode = full(one_way={3})
        with self.assertRaises(CommandError) as cm:
            self.run_test(obj)
        self.assertIn("Screw 4 didn't come back", str(cm.exception))

    def test_motor_much_faster_than_expected(self):
        obj, bed, gcode = full(gain=4.)
        self.assert_fails(obj, bed, "far more than")

    def test_stops_at_the_first_fault(self):
        obj, bed, gcode = full(reversed_={0, 2})
        self.assert_fails(obj, bed, "Motor 1 turns screw 1")
        commands = set(w.lstrip("-") for w in obj.last_board.writes if w != "0")
        self.assertEqual(commands, {"1"})

    def test_uncalibrated_motors_pass(self):
        # Uneven speeds, spin-up and backlash lose part of each turn, but
        # nowhere near enough to look like a fault
        obj, bed, gcode, configfile, probe = make_sim([0., 0.1, -0.05], uneven())
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_TEST(FakeGcmd())
        self.assertEqual(gcode.scripts[-1], "G28 Z")

    def test_turns_option(self):
        obj, bed, gcode = full()
        self.run_test(obj, {"TURNS": 0.25, "DEBUG": 1})
        # 0.25 turns at the default 3 seconds a turn
        self.assertIn("STAA: 1 for 0.750 seconds.", obj.gcode.responses)

    def test_turn_too_small_to_tell_from_noise(self):
        obj, bed, gcode = full()
        with self.assertRaises(CommandError) as cm:
            self.run_test(obj, {"TURNS": 0.1})
        self.assertIn("bigger TURNS", str(cm.exception))
        self.assertEqual(measurements(gcode), [])

    def test_turn_bigger_than_max_turns_per_move(self):
        obj, bed, gcode = full()
        with self.assertRaises(CommandError) as cm:
            self.run_test(obj, {"TURNS": 1.5})
        self.assertIn("max_turns_per_move", str(cm.exception))
        self.assertEqual(measurements(gcode), [])

    def test_more_screws_than_motors(self):
        obj, bed, gcode = full(heights=[0.] * 5)
        with self.assertRaises(CommandError) as cm:
            self.run_test(obj)
        self.assertIn("only drives 4 motors", str(cm.exception))
        self.assertEqual(bed.turns, [0.] * 5)

    def test_needs_screw_thread(self):
        obj, bed, gcode = full()
        obj.printer.objects["screws_tilt_adjust"].thread = None
        with self.assertRaises(CommandError) as cm:
            self.run_test(obj)
        self.assertIn("screw_thread", str(cm.exception))


class ContinuousTest(unittest.TestCase):
    def run_test(self, obj):
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_TEST(FakeGcmd({"CONTINUOUS": 1}))

    def test_passes_when_wired_right(self):
        obj, bed, gcode, probe = continuous()
        start = list(bed.heights)
        self.run_test(obj)
        for a, b in zip(bed.heights, start):
            self.assertAlmostEqual(a, b, places=6)
        # Watches each screw instead of measuring the whole bed
        self.assertEqual(measurements(gcode), [])
        self.assertEqual(len(probe.sessions), 4)
        self.assertTrue(all(s.ended for s in probe.sessions))
        self.assertEqual(sorted(set(probe.reads)), [0, 1, 2, 3])
        self.assertEqual(gcode.scripts[-1], "G28 Z")

    def test_motors_in_the_wrong_ports(self):
        obj, bed, gcode, probe = continuous(ports={1: 2, 2: 1})
        start = list(bed.heights)
        with self.assertRaises(CommandError) as cm:
            self.run_test(obj)
        self.assertIn("Screw 2 is turned by motor 3, not motor 2", str(cm.exception))
        for a, b in zip(bed.heights, start):
            self.assertAlmostEqual(a, b, places=6)
        self.assertTrue(all(s.ended for s in probe.sessions))

    def test_motor_that_does_not_move(self):
        obj, bed, gcode, probe = continuous(dead={1})
        with self.assertRaises(CommandError) as cm:
            self.run_test(obj)
        self.assertIn("Screw 2 barely moved", str(cm.exception))

    def test_reversed_motor(self):
        obj, bed, gcode, probe = continuous(reversed_={3})
        start = list(bed.heights)
        with self.assertRaises(CommandError) as cm:
            self.run_test(obj)
        self.assertIn("Motor 4 turns screw 4 the wrong way", str(cm.exception))
        for a, b in zip(bed.heights, start):
            self.assertAlmostEqual(a, b, places=6)

    def test_needs_an_eddy_probe(self):
        obj, bed, gcode, probe, toolhead = make_continuous([0., 0., 0.], eddy=False)
        with self.assertRaises(CommandError) as cm:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO_TEST(FakeGcmd({"CONTINUOUS": 1}))
        self.assertIn("probe_eddy_current", str(cm.exception))


class CalibrationTest(unittest.TestCase):
    def test_tests_motors_before_levelling(self):
        obj, bed, gcode = full(reversed_={1})
        start = list(bed.heights)
        with self.assertRaises(CommandError) as cm:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd())
        self.assertIn("Motor 2 turns screw 2 the wrong way", str(cm.exception))
        for a, b in zip(bed.heights, start):
            self.assertAlmostEqual(a, b, places=6)

    def test_test_can_be_skipped(self):
        obj, bed, gcode = full(reversed_={1})
        with self.assertRaises(CommandError) as cm:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"TEST": 0}))
        self.assertNotIn("wrong way", str(cm.exception).split("STAA:")[1][:30])


if __name__ == "__main__":
    unittest.main()
