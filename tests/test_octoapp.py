import ast
import os
import shlex
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import test_one_by_one as sim  # noqa: E402
from test_one_by_one import FakeGcmd, staa  # noqa: E402


class FakePrintStats:
    def __init__(self, state):
        self.state = state

    def get_status(self, eventtime):
        return {"state": self.state}


def octoapp_macro(script):
    """What OctoApp's macros from its Notifications wiki page store in
    _OCTOAPP_STATUS for this command, and what the plugin reads back from
    it, going through the same parsing Klipper does on the way."""
    # Klipper ends a command at ;, then splits KEY=value parameters
    script = script.split(";")[0]
    command, args = script.split(" ", 1)
    params = dict(arg.split("=", 1) for arg in shlex.split(args))
    final_message = params.get("MESSAGE", "") + "|||" + params.get("DETAIL", "") + "~~~123.4"
    # SET_GCODE_VARIABLE ... VALUE="'{ final_message }'"
    rendered = "SET_GCODE_VARIABLE MACRO=_OCTOAPP_STATUS VARIABLE=notify_message " \
               "VALUE=\"'%s'\"" % (final_message,)
    rendered = rendered.split(";")[0]
    value = dict(arg.split("=", 1) for arg in shlex.split(rendered.split(" ", 1)[1]))["VALUE"]
    stored = ast.literal_eval(value)
    # The plugin splits at ~~~ then |||
    content = stored.split("~~~")[0]
    message, detail = content.split("|||", 1)
    return command, message, detail


def make(heights, efficiency=1., macro=True, printing=None, config=None, **kwargs):
    obj, bed, gcode, sta = sim.make(heights, efficiency, config=config, **kwargs)
    if macro:
        obj.printer.objects["gcode_macro OCTOAPP_NOTIFY_STATUS"] = object()
    if printing is not None:
        obj.printer.objects["print_stats"] = FakePrintStats(printing)
    return obj, bed, gcode


def notifications(gcode):
    return [octoapp_macro(s)[1:] for s in gcode.scripts
            if s.startswith("OCTOAPP_NOTIFY_STATUS")]


class NotifyTest(unittest.TestCase):
    def test_success_outside_a_print(self):
        obj, bed, gcode = make([0., 0.1, -0.05], printing="standby")
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(notifications(gcode), [
            ("Bed screws level", "Spread 0.000mm, started at 0.150mm, 2 passes")])

    def test_success_without_print_stats(self):
        obj, bed, gcode = make([0., 0., 0.])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(notifications(gcode), [("Bed screws level", "Spread 0.000mm, 1 pass")])

    def test_success_during_a_print_is_not_sent(self):
        for state in ("printing", "paused"):
            obj, bed, gcode = make([0., 0.1, -0.05], printing=state)
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
            self.assertEqual(notifications(gcode), [], state)

    def test_failure_is_sent_during_a_print(self):
        # The screws barely move, so it gives up
        obj, bed, gcode = make([0., 0.2, -0.1], efficiency=0.01, attempts=2, printing="printing")
        with self.assertRaises(sim.CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        sent = notifications(gcode)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "Bed levelling stopped")
        self.assertEqual(sent[0][1], obj.get_status(0.)["error"].replace('"', ""))

    def test_failure_is_sent_outside_a_print(self):
        obj, bed, gcode = make([0., 0.2, -0.1], efficiency=0.01, attempts=2)
        with self.assertRaises(sim.CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual([m for m, d in notifications(gcode)], ["Bed levelling stopped"])

    def test_cancel_is_not_sent(self):
        obj, bed, gcode = make([0., 0.1, -0.05])
        run_motor = obj._run_motor
        def run(screw, direction, seconds):
            obj._request_cancel()
            return run_motor(screw, direction, seconds)
        obj._run_motor = run
        with self.assertRaises(sim.CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(obj.get_status(0.)["state"], "cancelled")
        self.assertEqual(notifications(gcode), [])

    def test_nothing_sent_without_the_octoapp_macro(self):
        obj, bed, gcode = make([0., 0.1, -0.05], macro=False)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertFalse([s for s in gcode.scripts if "OCTOAPP" in s])

    def test_can_be_turned_off(self):
        obj, bed, gcode = make([0., 0.1, -0.05], config={"octoapp_notifications": False})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(notifications(gcode), [])

    def test_other_commands_are_not_sent(self):
        obj, bed, gcode = make([0., 0.1, -0.05])
        obj.cmd_SCREWS_TILT_ADJUST_MANUAL(FakeGcmd({"SCREW": 2, "DISTANCE": 0.1}))
        self.assertEqual(notifications(gcode), [])

    def test_broken_notify_macro_does_not_change_the_result(self):
        obj, bed, gcode = make([0., 0.1, -0.05])
        run_script = gcode.run_script_from_command
        def broken(script):
            if script.startswith("OCTOAPP_NOTIFY_STATUS"):
                raise sim.CommandError("Unknown command")
            return run_script(script)
        gcode.run_script_from_command = broken
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(obj.get_status(0.)["state"], "done")


class NotifyTextTest(unittest.TestCase):
    def test_awkward_text_survives_octoapp_macros(self):
        obj, bed, gcode = make([0., 0., 0.])
        text = 'Screw "rear left" can\'t move; check it\\now ||| or ~~~ later\nplease'
        obj._notify(text, text)
        command, message, detail = octoapp_macro(gcode.scripts[-1])
        self.assertEqual(command, "OCTOAPP_NOTIFY_STATUS")
        expected = u"Screw rear left can’t move, check it/now | or ~ later please"
        self.assertEqual(message, expected)
        self.assertEqual(detail, expected)


if __name__ == "__main__":
    unittest.main()
