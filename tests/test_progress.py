import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_one_by_one as sim  # noqa: E402
from test_one_by_one import FakeGcmd, staa  # noqa: E402

CLOSE = 'RESPOND type="command" msg="action:prompt_end"'


BaseGcode = sim.FakeGcode


class RecordingGcode(BaseGcode):
    """Records dialog output, and scripts sent the way Moonraker would."""

    def __init__(self, *args):
        BaseGcode.__init__(self, *args)
        self.raw = []
        self.queued = []

    def respond_raw(self, msg):
        self.raw.append(msg)

    def run_script(self, script):
        self.queued.append(script)


class FakeDisplayStatus:
    def __init__(self):
        self.message = "Before"
        self.seen = []


def make(heights, efficiency=1., **kwargs):
    original = sim.FakeGcode
    sim.FakeGcode = RecordingGcode
    try:
        obj, bed, gcode, sta = sim.make(heights, efficiency, **kwargs)
    finally:
        sim.FakeGcode = original
    display = FakeDisplayStatus()
    obj.printer.objects["display_status"] = display
    return obj, bed, gcode, display


def dialogs(raw):
    # Each dialog drawn, as its list of lines (text and buttons)
    shown = []
    for msg in raw:
        action = msg[len("// action:"):]
        if action.startswith("prompt_begin "):
            shown.append([action[len("prompt_begin "):]])
        elif action.startswith(("prompt_text ", "prompt_footer_button ")):
            shown[-1].append(action.split(" ", 1)[1])
    return shown


def on_motor(obj, action, turn=1):
    # Runs action as the given motor turn starts
    run_motor = obj._run_motor
    count = [0]
    def run(screw, direction, seconds):
        count[0] += 1
        if count[0] == turn:
            action()
        return run_motor(screw, direction, seconds)
    obj._run_motor = run


class StatusTest(unittest.TestCase):
    def test_idle_before_any_run(self):
        obj, bed, gcode, display = make([0., 0.1, -0.1])
        status = obj.get_status(0.)
        self.assertEqual(status["state"], "idle")
        self.assertFalse(status["running"])
        self.assertEqual(status["screws"], [])

    def test_status_while_turning(self):
        obj, bed, gcode, display = make([0., 0.1, -0.05])
        seen = []
        on_motor(obj, lambda: seen.append(obj.get_status(0.)))
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        status = seen[0]
        self.assertTrue(status["running"])
        self.assertEqual(status["mode"], "level")
        self.assertEqual(status["pass"], 1)
        self.assertEqual(status["max_passes"], 10)
        self.assertEqual(status["state"], "turning")
        self.assertEqual([s["name"] for s in status["screws"]], ["screw1", "screw2", "screw3"])
        # Screw 1 is the reference, as the others are either side of it
        self.assertEqual([s["reference"] for s in status["screws"]], [True, False, False])
        self.assertAlmostEqual(status["screws"][1]["deviation"], 0.1)
        self.assertAlmostEqual(status["screws"][2]["deviation"], -0.05)
        self.assertAlmostEqual(status["screws"][1]["turns"], -0.2)
        self.assertAlmostEqual(status["spread"], 0.15)
        self.assertEqual(status["screw"], "screw2")
        self.assertEqual(status["message"], "Turning screw2 CCW 00:12")

    def test_status_after_a_run(self):
        obj, bed, gcode, display = make([0., 0.1, -0.05])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        status = obj.get_status(0.)
        self.assertEqual(status["state"], "done")
        self.assertFalse(status["running"])
        self.assertEqual(status["pass"], 2)
        self.assertEqual(status["message"], "Bed screws level")
        self.assertTrue(all(s["level"] for s in status["screws"]))
        self.assertAlmostEqual(status["start_spread"], 0.15)
        self.assertAlmostEqual(status["spread"], 0.)
        self.assertAlmostEqual(status["run_turns"][0], 0.)
        self.assertAlmostEqual(status["run_turns"][1], 0.2)
        self.assertAlmostEqual(status["run_turns"][2], 0.1)

    def test_one_by_one_updates_each_screw_as_it_settles(self):
        obj, bed, gcode, display = make([0., 0.2, -0.1], efficiency=0.7)
        seen = []
        on_motor(obj, lambda: seen.append(obj.get_status(0.)), turn=2)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))
        # The second turn is screw 2's second go: it has moved 70% of the way
        screw = seen[0]["screws"][1]
        self.assertEqual(seen[0]["screw"], "screw2")
        self.assertAlmostEqual(screw["deviation"], 0.06, places=3)
        self.assertFalse(screw["level"])

    def test_failed_run_keeps_the_error(self):
        obj, bed, gcode, display = make([0., 0.2, -0.1], efficiency=0.)
        with self.assertRaises(sim.CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))
        status = obj.get_status(0.)
        self.assertEqual(status["state"], "failed")
        self.assertIn("barely moved", status["error"])
        self.assertFalse(status["error"].startswith("STAA"))

    def test_status_line_during_run_then_restored(self):
        obj, bed, gcode, display = make([0., 0.1, -0.05])
        on_motor(obj, lambda: display.seen.append(display.message))
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(display.seen[0], "Levelling: Turning screw2 CCW 00:12")
        self.assertEqual(display.message, "Before")

    def test_no_dialog_unless_asked(self):
        obj, bed, gcode, display = make([0., 0.1, -0.05])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(gcode.raw, [])


class DialogTest(unittest.TestCase):
    def test_redrawn_at_each_step_with_cancel(self):
        obj, bed, gcode, display = make([0., 0.1, -0.05])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"DIALOG": 1}))
        shown = dialogs(gcode.raw)
        self.assertGreater(len(shown), 3)
        for dialog in shown[:-1]:
            self.assertEqual(dialog[0], "Levelling bed screws")
            self.assertEqual(dialog[-1], "Cancel|SCREWS_TILT_ADJUST_AUTO_CANCEL|error")
        turning = [d for d in shown if "Turning screw2 CCW 00:12" in d][0]
        self.assertEqual(turning[1], "Pass 1 of up to 10")
        self.assertIn("screw1: reference", turning)
        self.assertIn("screw2: +0.100mm, CCW 00:12", turning)
        self.assertIn("screw3: -0.050mm, CW 00:06", turning)
        self.assertIn("Spread 0.150mm", turning)

    def test_heaters_shown_while_warming_up(self):
        obj, bed, gcode, display = make([0., 0.1, -0.05])
        heater = type("Heater", (), {})()
        # At temperature after the first pass's turns
        heater.get_temp = lambda t: (50. if t < 0.5 else 60., 60.)
        heater.check_busy = lambda t: t < 0.5
        heaters = type("Heaters", (), {})()
        heaters.get_all_heaters = lambda: ["heater_bed"]
        heaters.lookup_heater = lambda name: heater
        obj.printer.objects["heaters"] = heaters
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"DIALOG": 1, "WAIT_FOR_HEATERS": 1}))
        lines = [l for d in dialogs(gcode.raw) for l in d]
        self.assertIn("Pass 1 of up to 10, heaters warming up", lines)

    def test_summary_when_done(self):
        obj, bed, gcode, display = make([0., 0.1, -0.05])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"DIALOG": 1}))
        summary = dialogs(gcode.raw)[-1]
        self.assertEqual(summary[1], "Bed screws level")
        self.assertIn("Passes: 2", summary)
        self.assertIn("screw2: level", summary)
        self.assertIn("Turned: screw2 0.20, screw3 0.10", summary)
        self.assertEqual(summary[-1], "Close|" + staa.CLOSE_DIALOG_COMMAND)
        self.assertEqual(gcode.raw[-1], "// action:prompt_show")

    def test_summary_when_it_fails(self):
        obj, bed, gcode, display = make([0., 0.2, -0.1], efficiency=0.)
        with self.assertRaises(sim.CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"DIALOG": 1, "ONE_BY_ONE": 1}))
        summary = dialogs(gcode.raw)[-1]
        self.assertTrue(summary[1].startswith("Stopped: Screw 2 barely moved"), summary)

    def test_cancel_after_turning_says_to_home_z(self):
        obj, bed, gcode, display = make([0., 0.1, -0.05])
        on_motor(obj, obj._request_cancel, turn=2)
        with self.assertRaises(sim.CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"DIALOG": 1}))
        summary = dialogs(gcode.raw)[-1]
        self.assertEqual(summary[1], "Cancelled")
        self.assertIn("Screws were turned, so home Z (G28 Z) before printing.", summary)
        self.assertEqual(obj.get_status(0.)["state"], "cancelled")

    def test_cancel_before_turning_just_closes(self):
        obj, bed, gcode, display = make([0., 0.1, -0.05])
        measure = obj._measure_bed_tilt
        def cancel_then_measure():
            obj._request_cancel()
            measure()
        obj._measure_bed_tilt = cancel_then_measure
        with self.assertRaises(sim.CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"DIALOG": 1}))
        self.assertEqual(gcode.raw[-1], "// action:prompt_end")

    def test_closing_it_keeps_it_closed(self):
        obj, bed, gcode, display = make([0., 0.1, -0.05])
        on_motor(obj, lambda: gcode.run_script(CLOSE))
        on_motor_status = []
        on_motor(obj, lambda: on_motor_status.append(display.message), turn=2)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"DIALOG": 1}))
        # Closed straight away, not queued behind the run
        self.assertEqual(gcode.queued, [])
        closed_at = gcode.raw.index("// action:prompt_end")
        # Nothing redrawn after, not even the summary
        self.assertEqual(gcode.raw[closed_at + 1:], [])
        # The status line carries on
        self.assertTrue(on_motor_status[0].startswith("Levelling: Turning"))

    def test_close_is_only_caught_during_a_run(self):
        obj, bed, gcode, display = make([0., 0.1, -0.05])
        gcode.run_script(CLOSE)
        self.assertEqual(gcode.queued, [CLOSE])

    def test_close_button_command_closes_the_summary(self):
        obj, bed, gcode, display = make([0., 0.1, -0.05])
        obj.cmd_CLOSE_DIALOG(FakeGcmd())
        self.assertEqual(gcode.raw, ["// action:prompt_end"])


if __name__ == "__main__":
    unittest.main()
