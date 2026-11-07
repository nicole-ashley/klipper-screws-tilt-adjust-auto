import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_screws_tilt_adjust_auto import (  # noqa: E402
    LEVEL, OFF, CommandError, FakeGcmd, FakeGcode, FakeReactor, TestBase, staa)
import test_screws_tilt_adjust_auto as base  # noqa: E402


class CancellableGcode(FakeGcode):
    """Adds what Moonraker requests go through, and records output."""

    def __init__(self, *args, **kwargs):
        FakeGcode.__init__(self, *args, **kwargs)
        self.info = []
        self.raw = []
        self.queued = []

    def respond_info(self, msg):
        self.info.append(msg)

    def respond_raw(self, msg):
        self.raw.append(msg)

    def run_script(self, script):
        # The real one waits for the G-code lock, so a running command
        # would hold it up. Recorded here to show it wasn't called.
        self.queued.append(script)


class CancelReactor(FakeReactor):
    """Sends a script the way Moonraker would, once the clock passes a
    given time, while the code under test is waiting."""

    def __init__(self):
        FakeReactor.__init__(self)
        self.at = None
        self.script = None
        self.gcode = None

    def pause(self, waketime):
        result = FakeReactor.pause(self, waketime)
        if self.at is not None and self.now >= self.at:
            self.at = None
            self.gcode.run_script(self.script)
        return result


class CancelTest(TestBase):
    def make(self, rounds, cancel_at=None, script="SCREWS_TILT_ADJUST_AUTO_CANCEL", **kwargs):
        original_gcode, original_reactor = base.FakeGcode, base.FakeReactor
        base.FakeGcode, base.FakeReactor = CancellableGcode, CancelReactor
        try:
            obj = TestBase.make(self, rounds, **kwargs)
        finally:
            base.FakeGcode, base.FakeReactor = original_gcode, original_reactor
        self.reactor.gcode = self.gcode
        self.reactor.at = cancel_at
        self.reactor.script = script
        return obj

    def motor_writes(self):
        return [w for board in self.boards for w in board.writes]

    def test_cancel_mid_turn_stops_the_motor_promptly(self):
        # Probing takes 60s, then screw 2 turns 0.5 turns at 4s per turn:
        # cancel 1s into the turn
        obj = self.make([OFF, LEVEL], cancel_at=61.0,
                        config={"full_turn_time_in_seconds": 4})
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertIn("Cancelled", str(ctx.exception))
        self.assertEqual(self.motor_writes(), ["2", "0"])
        self.assert_motors_stopped()
        # Stopped within one check of the cancel, not after the 2s turn
        self.assertLessEqual(self.reactor.now, 61.0 + staa.CANCEL_CHECK_INTERVAL)
        # Caught on the way in, not queued behind the run
        self.assertEqual(self.gcode.queued, [])
        # No more measuring or homing after a cancel
        self.assertEqual(self.calculate_count(), 1)
        self.assertEqual(self.home_z_count(), 0)
        self.assertTrue(any("home Z" in m for m in self.gcode.info))

    def test_turn_cut_short_still_counts_for_backlash(self):
        obj = self.make([OFF, LEVEL], cancel_at=60.5)
        with self.assertRaises(CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(obj.motors[1].last_direction, staa.CW)

    def test_cancel_while_waiting_for_heaters(self):
        # Probing takes 60s, so the first pass is done by 120s and the bed
        # isn't ready until 600s
        obj = self.make([OFF, LEVEL, LEVEL], cancel_at=200.,
                        heaters={"heater_bed": (60., 600.)})
        with self.assertRaises(CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"WAIT_FOR_HEATERS": 1}))
        self.assertLess(self.reactor.now, 202.)
        self.assertEqual(self.calculate_count(), 2)
        self.assert_motors_stopped()

    def test_cancel_during_probing_stops_before_turning(self):
        obj = self.make([OFF, LEVEL])
        original = self.gcode.run_script_from_command

        def probe_then_cancel(script):
            original(script)
            obj.gcode.run_script("SCREWS_TILT_ADJUST_AUTO_CANCEL")
        self.gcode.run_script_from_command = probe_then_cancel
        with self.assertRaises(CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(self.motor_writes(), ["0"])
        self.assertFalse(any("home Z" in m for m in self.gcode.info))

    def test_cancel_macro_and_any_case_are_caught(self):
        for script in ("CANCEL_LEVEL_BED_SCREWS", " screws_tilt_adjust_auto_cancel\n"):
            obj = self.make([OFF, LEVEL], cancel_at=61.0, script=script)
            with self.assertRaises(CommandError):
                obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
            self.assertEqual(self.gcode.queued, [])

    def test_other_commands_go_through_during_a_run(self):
        obj = self.make([OFF, LEVEL], cancel_at=61.0, script="M117 hello")
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(self.gcode.queued, ["M117 hello"])
        self.assertEqual(self.home_z_count(), 1)

    def test_cancel_when_idle_goes_through_and_does_nothing(self):
        obj = self.make([OFF, LEVEL])
        obj.gcode.run_script("SCREWS_TILT_ADJUST_AUTO_CANCEL")
        self.assertEqual(self.gcode.queued, ["SCREWS_TILT_ADJUST_AUTO_CANCEL"])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_CANCEL(FakeGcmd())
        self.assertIn("STAA: Nothing to cancel", self.gcode.info)
        # A cancel before the run doesn't stop the next one
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(self.home_z_count(), 1)

    def test_next_run_works_after_a_cancel(self):
        obj = self.make([OFF, OFF, LEVEL], cancel_at=61.0)
        with self.assertRaises(CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertFalse(obj.running)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(self.home_z_count(), 1)

    def test_manual_turn_can_be_cancelled(self):
        obj = self.make([], cancel_at=0.5)
        with self.assertRaises(CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_MANUAL(FakeGcmd({"SCREW": 2, "DISTANCE": 0.5}))
        self.assertEqual(self.motor_writes(), ["2", "0"])
        self.assertLessEqual(self.reactor.now, 0.5 + staa.CANCEL_CHECK_INTERVAL)
        self.assert_motors_stopped()

    def test_dialog_has_a_cancel_button_and_closes_after(self):
        obj = self.make([OFF, LEVEL])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"DIALOG": 1}))
        self.assertEqual(self.gcode.raw[0], "// action:prompt_begin Levelling bed screws")
        self.assertIn("// action:prompt_footer_button Cancel|SCREWS_TILT_ADJUST_AUTO_CANCEL|error",
                      self.gcode.raw)
        self.assertEqual(self.gcode.raw[-2:], ["// action:prompt_show", "// action:prompt_end"])

    def test_dialog_closes_after_a_cancel_or_error(self):
        for rounds, cancel_at in (([OFF, LEVEL], 1.0), ([OFF, RuntimeError("probe")], None)):
            obj = self.make(rounds, cancel_at=cancel_at)
            with self.assertRaises(Exception):
                obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"DIALOG": 1}))
            self.assertEqual(self.gcode.raw[-1], "// action:prompt_end")

    def test_no_dialog_unless_asked(self):
        obj = self.make([OFF, LEVEL])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(self.gcode.raw, [])


def cancel_after_turns(obj, turns):
    # Cancels once the given number of motor turns have started, the way a
    # cancel caught on its way in would
    run_motor = obj._run_motor
    started = []
    def run(screw, direction, seconds):
        started.append(screw)
        if len(started) > turns:
            obj._request_cancel()
        return run_motor(screw, direction, seconds)
    obj._run_motor = run
    return started


class CancelOtherModesTest(unittest.TestCase):
    # These use the simulated bed, which has its own fakes
    def test_continuous_cancel_ends_the_scan_and_lifts_the_probe(self):
        from test_continuous import make_continuous
        from test_one_by_one import CommandError, FakeGcmd
        obj, bed, gcode, probe, toolhead = make_continuous([0., 0.2, -0.1], efficiency=0.5)
        cancel_after_turns(obj, 1)
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"CONTINUOUS": 1}))
        self.assertIn("Cancelled", str(ctx.exception))
        self.assertTrue(all(s.ended for s in probe.sessions))
        self.assertGreater(toolhead.pos[2], probe.scan_z)
        self.assertEqual(obj.last_board.writes[-1], "0")
        self.assertNotIn("G28 Z", gcode.scripts)

    def test_calibration_cancel_saves_nothing(self):
        from test_calibration import make_sim, uneven
        from test_one_by_one import CommandError, FakeGcmd
        obj, bed, gcode, configfile, probe = make_sim([0., 0., 0.], uneven())
        cancel_after_turns(obj, 3)
        with self.assertRaises(CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"LEVEL": 0}))
        self.assertEqual(obj.last_board.writes[-1], "0")
        self.assertFalse(obj.running)
        self.assertEqual(configfile.values, {})


if __name__ == "__main__":
    unittest.main()
