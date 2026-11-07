import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_one_by_one import CommandError, FakeGcmd, make  # noqa: E402
from test_calibration import make_sim, uneven  # noqa: E402

# Lines only shown with debug on
DEBUG_ONLY = ("seconds.", "Stopping motors", "Motor movements", "movement:",
              "Measuring", "Watching", " : z=", " moved ")


def capture(gcode):
    messages = []
    gcode.respond_info = messages.append
    return messages


class QuietByDefaultTest(unittest.TestCase):
    def assert_quiet(self, messages):
        for msg in messages:
            for marker in DEBUG_ONLY:
                self.assertNotIn(marker, msg)

    def test_standard_mode_summarises_each_pass(self):
        obj, bed, gcode, sta = make([0., 0.25, -0.125])
        messages = capture(gcode)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assert_quiet(messages)
        self.assertEqual(messages[0], "STAA: Levelling bed screws")
        # Same names and turns format as SCREWS_TILT_CALCULATE
        self.assertIn("STAA: Adjusting screw2 CCW 00:30, screw3 CW 00:15", messages)
        self.assertEqual(messages[-1], "STAA: Bed screws level")

    def test_one_by_one_reports_each_screw_once(self):
        obj, bed, gcode, sta = make([0., 0.2, -0.1], efficiency=0.7)
        messages = capture(gcode)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))
        self.assert_quiet(messages)
        self.assertEqual(messages[0], "STAA: Levelling bed screws (one by one)")
        self.assertEqual(len([m for m in messages if m.startswith("STAA: screw2 level after")]), 1)
        self.assertEqual(len([m for m in messages if m.startswith("STAA: screw3 level after")]), 1)

    def test_manual_names_the_screw(self):
        obj, bed, gcode, sta = make([0., 0., 0.])
        messages = capture(gcode)
        obj.cmd_SCREWS_TILT_ADJUST_MANUAL(FakeGcmd({"SCREW": 2, "DISTANCE": -0.75}))
        self.assertEqual(messages, ["STAA: Turning screw2 CCW 00:45"])

    def test_calibration_shows_only_results(self):
        obj, bed, gcode, configfile, probe = make_sim([0., 0.1, -0.05], uneven())
        messages = capture(gcode)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd())
        self.assert_quiet(messages)
        self.assertIn("STAA: Calibrating screw2", messages)
        self.assertEqual(len([m for m in messages if m.startswith("STAA: Motor ") and "clockwise" in m]), 3)


class MotorTestOutputTest(unittest.TestCase):
    def test_quiet_motor_test_names_each_screw(self):
        obj, bed, gcode, configfile, probe = make_sim([0., 0.1, -0.05], uneven())
        messages = capture(gcode)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_TEST(FakeGcmd())
        self.assertEqual(messages, [
            "STAA: Testing motors", "STAA: Motor 1 OK (screw1)", "STAA: Motor 2 OK (screw2)",
            "STAA: Motor 3 OK (screw3)", "STAA: Homing Z as bed level will have changed",
            "STAA: All 3 motors passed the test"])

    def test_debug_motor_test_shows_each_reading(self):
        obj, bed, gcode, configfile, probe = make_sim([0., 0.1, -0.05], uneven())
        messages = capture(gcode)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_TEST(FakeGcmd({"DEBUG": 1}))
        self.assertIn("STAA: Testing motor 2", messages)
        self.assertTrue(any(m.startswith("STAA: Screw 2 moved ") for m in messages))


class DebugTest(unittest.TestCase):
    def test_debug_parameter_shows_every_step(self):
        obj, bed, gcode, sta = make([0., 0.2, -0.1], efficiency=0.7)
        messages = capture(gcode)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1, "DEBUG": 1}))
        self.assertTrue(any(m.endswith("seconds.") for m in messages))
        self.assertIn("STAA: Stopping motors.", messages)
        self.assertTrue(any(m.startswith("STAA: screw2 : z=") for m in messages))
        # The usual lines are still there
        self.assertEqual(messages[-1], "STAA: Bed screws level")

    def test_debug_setting_is_the_default(self):
        obj, bed, gcode, sta = make([0., 0.2], config={"debug": True})
        messages = capture(gcode)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertIn("STAA: Stopping motors.", messages)

    def test_debug_parameter_overrides_the_setting(self):
        obj, bed, gcode, sta = make([0., 0.2], config={"debug": True})
        messages = capture(gcode)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"DEBUG": 0}))
        self.assertNotIn("STAA: Stopping motors.", messages)

    def test_calibration_debug_shows_each_move(self):
        obj, bed, gcode, configfile, probe = make_sim([0., 0.1, -0.05], uneven())
        messages = capture(gcode)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"DEBUG": 1, "SCREW": 2}))
        self.assertTrue(any(m.startswith("STAA: Screw 2 cw for ") for m in messages))


class RawGcode:
    """Klipper's respond_info goes through respond_raw on the same object."""

    def __init__(self):
        self.shown = []

    def respond_info(self, msg):
        self.respond_raw("// " + msg)

    def respond_raw(self, msg):
        self.shown.append(msg)


class QuietlyTest(unittest.TestCase):
    def setUp(self):
        self.obj, bed, gcode, sta = make([0., 0.])
        self.gcode = self.obj.gcode = RawGcode()

    def test_holds_back_klippers_output(self):
        with self.obj._quietly():
            self.gcode.respond_info("probe at 0,0 is z=0.1")
        self.assertEqual(self.gcode.shown, [])
        self.assertNotIn("respond_raw", vars(self.gcode))
        self.gcode.respond_info("after")
        self.assertEqual(self.gcode.shown, ["// after"])

    def test_shows_it_after_all_on_error(self):
        with self.assertRaises(CommandError):
            with self.obj._quietly():
                self.gcode.respond_info("screw2 : x=0, y=0, z=5")
                raise CommandError("bad tilt")
        self.assertEqual(self.gcode.shown, ["// screw2 : x=0, y=0, z=5"])
        self.assertNotIn("respond_raw", vars(self.gcode))

    def test_our_own_messages_still_show(self):
        # e.g. a cancel arriving while a single screw is measured
        with self.obj._quietly():
            self.gcode.respond_info("probe at 0,0 is z=0.1")
            self.obj._info("Cancelling")
        self.assertEqual(self.gcode.shown, ["// STAA: Cancelling"])
        self.assertNotIn("respond_raw", vars(self.gcode))

    def test_shows_it_when_debugging(self):
        self.obj.debug = True
        with self.obj._quietly():
            self.gcode.respond_info("probe at 0,0 is z=0.1")
        self.assertEqual(self.gcode.shown, ["// probe at 0,0 is z=0.1"])


if __name__ == "__main__":
    unittest.main()
