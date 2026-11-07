import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_one_by_one import (  # noqa: E402
    CommandError, FakeConfig, FakeGcmd, is_level, make, PITCH, staa)
from test_continuous import make_continuous  # noqa: E402

CW, CCW = staa.CW, staa.CCW


class Motors:
    """Motors that each run at their own speed each way, take a moment to
    get going, and have slack to take up when they change direction."""

    def __init__(self, count, seconds_per_turn=None, spin_up=None, backlash=None):
        self.seconds_per_turn = seconds_per_turn or [{CW: 3., CCW: 3.}] * count
        self.spin_up = spin_up or [{CW: 0., CCW: 0.}] * count
        self.backlash = backlash or [0.] * count
        # Start with the slack taken up clockwise
        self.last_direction = [CW] * count
        self.slack = [0.] * count


class SimBoard:
    def __init__(self, bed, reactor, motors):
        self.bed = bed
        self.motors = motors
        self.writes = []
        self.running = 0
        self.spin_left = 0.
        reactor.board = self

    def read(self, n):
        return b"\n"

    def write(self, data):
        command = int(data.decode())
        self.writes.append(data.decode())
        if command != 0 and command != self.running:
            index = abs(command) - 1
            direction = CW if command > 0 else CCW
            self.spin_left = self.motors.spin_up[index][direction]
            if self.motors.last_direction[index] != direction:
                self.motors.slack[index] = self.motors.backlash[index]
            self.motors.last_direction[index] = direction
        self.running = command

    def run_for(self, seconds):
        if not self.running:
            return
        index = abs(self.running) - 1
        direction = CW if self.running > 0 else CCW
        spin = min(self.spin_left, seconds)
        self.spin_left -= spin
        turns = (seconds - spin) / self.motors.seconds_per_turn[index][direction]
        slack = min(self.motors.slack[index], turns)
        self.motors.slack[index] -= slack
        self.bed.turn(index, direction * (turns - slack))

    def close(self):
        pass


class FakeConfigfile:
    def __init__(self):
        self.values = {}

    def set(self, section, option, value):
        self.values[(section, option)] = value


class FakeSaveVariables:
    def __init__(self):
        self.variables = {}

    def get_status(self, eventtime):
        return {"variables": dict(self.variables)}


def make_sim(heights, motors, continuous=False, save_variables=False):
    if continuous:
        obj, bed, gcode, probe, toolhead = make_continuous(heights)
    else:
        obj, bed, gcode, sta = make(heights)
        probe = None
    sta = obj.printer.objects["screws_tilt_adjust"]
    sta.thread = 0  # CW-M3, as the fake bed moves
    configfile = FakeConfigfile()
    obj.printer.objects["configfile"] = configfile
    if save_variables:
        sv = FakeSaveVariables()
        obj.printer.objects["save_variables"] = sv
        run_script = gcode.run_script_from_command
        def run(script):
            if script.startswith("SAVE_VARIABLE"):
                params = dict(p.split("=", 1) for p in script.split()[1:])
                sv.variables[params["VARIABLE"]] = ast.literal_eval(params["VALUE"])
            run_script(script)
        gcode.run_script_from_command = run
    board = SimBoard(bed, obj.reactor, motors)
    def connect():
        obj.board = obj.last_board = board
        board.writes = []
    obj._connect_to_board = connect
    return obj, bed, gcode, configfile, probe


def single_probes(gcode):
    return [s for s in gcode.scripts if s.startswith("probed") and "," not in s]


UNEVEN = Motors(
    3,
    seconds_per_turn=[{CW: 2.5, CCW: 3.6}, {CW: 4.2, CCW: 2.8}, {CW: 3.1, CCW: 3.3}],
    spin_up=[{CW: 0.2, CCW: 0.3}, {CW: 0.1, CCW: 0.25}, {CW: 0., CCW: 0.15}],
    backlash=[0.05, 0.08, 0.03])


def uneven():
    return Motors(3, [dict(d) for d in UNEVEN.seconds_per_turn],
                  [dict(d) for d in UNEVEN.spin_up], list(UNEVEN.backlash))


class CalibrationTest(unittest.TestCase):
    def assert_matches(self, obj, motors):
        for i, motor in enumerate(obj.motors[:3]):
            for d in (CW, CCW):
                self.assertAlmostEqual(
                    motor.seconds_per_turn[d], motors.seconds_per_turn[i][d], delta=0.05)
                self.assertAlmostEqual(motor.spin_up_time[d], motors.spin_up[i][d], delta=0.02)
            self.assertAlmostEqual(motor.backlash, motors.backlash[i], delta=0.01)

    def test_measures_each_motor_each_way(self):
        motors = uneven()
        obj, bed, gcode, configfile, probe = make_sim([0., 0., 0.], motors)
        start = list(bed.heights)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"LEVEL": 0}))

        self.assert_matches(obj, motors)
        section = "screws_tilt_adjust_auto"
        self.assertAlmostEqual(
            float(configfile.values[(section, "motor2_cw_seconds_per_turn")]), 4.2, delta=0.05)
        self.assertAlmostEqual(
            float(configfile.values[(section, "motor1_backlash")]), 0.05, delta=0.01)
        # Ends where it started
        for h, s in zip(bed.heights, start):
            self.assertAlmostEqual(h, s, delta=0.005)
        self.assertEqual(obj.last_board.writes[-1], "0")
        self.assertIn("G28 Z", gcode.scripts)

    def level(self, motors, calibrate):
        obj, bed, gcode, configfile, probe = make_sim([0., 0., 0.], motors)
        obj.learn_motor_speed = False
        if calibrate:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"LEVEL": 0}))
        bed.heights[1:] = [bed.heights[0] + 0.2, bed.heights[0] - 0.15]
        gcode.scripts = []
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))
        self.assertTrue(is_level(bed))
        return len(single_probes(gcode))

    def test_calibrated_motors_level_in_one_go(self):
        self.assertEqual(self.level(uneven(), True), 2)

    def test_calibrated_motors_level_in_fewer_tries(self):
        # Without spin-up the uncalibrated motors get there, just slower
        def no_spin_up():
            m = uneven()
            m.spin_up = [{CW: 0., CCW: 0.}] * 3
            return m
        self.assertLess(self.level(no_spin_up(), True), self.level(no_spin_up(), False))

    def test_spin_up_stops_uncalibrated_motors_making_small_turns(self):
        with self.assertRaises(CommandError):
            self.level(uneven(), False)

    def test_levels_first_by_default(self):
        motors = uneven()
        obj, bed, gcode, configfile, probe = make_sim([0., 0.2, -0.1], motors)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd())
        self.assert_matches(obj, motors)

    def test_single_screw(self):
        motors = uneven()
        obj, bed, gcode, configfile, probe = make_sim([0., 0., 0.], motors)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"LEVEL": 0, "SCREW": 2}))
        self.assertEqual(sorted(set(single_probes(gcode))), ["probed 2"])
        self.assertAlmostEqual(obj.motors[1].seconds_per_turn[CW], 4.2, delta=0.05)
        self.assertEqual(obj.motors[0].seconds_per_turn[CW], 3.)

    def test_continuous_watches_with_the_probe(self):
        motors = uneven()
        obj, bed, gcode, configfile, probe = make_sim([0., 0., 0.], motors, continuous=True)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"LEVEL": 0, "CONTINUOUS": 1}))
        self.assertEqual(single_probes(gcode), [])
        self.assertEqual(len(probe.sessions), 3)
        self.assertTrue(all(s.ended for s in probe.sessions))
        self.assert_matches(obj, motors)

    def test_motor_that_does_not_move_is_an_error(self):
        motors = uneven()
        motors.seconds_per_turn[1] = {CW: 1e9, CCW: 3.}
        obj, bed, gcode, configfile, probe = make_sim([0., 0., 0.], motors)
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"LEVEL": 0}))
        self.assertIn("Screw 2 didn't move", str(ctx.exception))
        self.assertIn("clockwise", str(ctx.exception))
        self.assertEqual(obj.last_board.writes[-1], "0")
        self.assertEqual(configfile.values, {})

    def test_backlash_bigger_than_the_moves_is_an_error(self):
        motors = uneven()
        motors.backlash[0] = 0.3
        obj, bed, gcode, configfile, probe = make_sim([0., 0., 0.], motors)
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"LEVEL": 0}))
        self.assertIn("bigger TURNS", str(ctx.exception))
        # A bigger move gets past it
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"LEVEL": 0, "TURNS": 0.4}))
        self.assertAlmostEqual(obj.motors[0].backlash, 0.3, delta=0.01)

    def test_needs_screw_thread_or_an_uneven_bed(self):
        obj, bed, gcode, configfile, probe = make_sim([0., 0., 0.], uneven())
        del obj.printer.objects["screws_tilt_adjust"].thread
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"LEVEL": 0}))
        self.assertIn("LEVEL=0", str(ctx.exception))

    def test_unknown_screw_is_an_error(self):
        obj, bed, gcode, configfile, probe = make_sim([0., 0., 0.], uneven())
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"LEVEL": 0, "SCREW": 5}))
        self.assertIn("only 3 screws", str(ctx.exception))

    def test_resets_learned_speeds(self):
        motors = uneven()
        obj, bed, gcode, configfile, probe = make_sim(
            [0., 0., 0.], motors, save_variables=True)
        sv = obj.printer.objects["save_variables"]
        sv.variables["staa_motor2_cw_seconds_per_turn"] = 9.
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"LEVEL": 0}))
        self.assertAlmostEqual(sv.variables["staa_motor2_cw_seconds_per_turn"], 4.2, delta=0.05)


class MotorModelTest(unittest.TestCase):
    def test_reads_config_and_falls_back_to_full_turn_time(self):
        config = FakeConfig(None, {
            "motor1_cw_seconds_per_turn": 2.5, "motor1_ccw_spin_up_time": 0.2,
            "motor1_backlash": 0.1})
        motor = staa.MotorModel(config, 1, 3.)
        self.assertEqual(motor.seconds_per_turn, {CW: 2.5, CCW: 3.})
        self.assertEqual(motor.spin_up_time, {CW: 0., CCW: 0.2})
        self.assertEqual(motor.backlash, 0.1)
        other = staa.MotorModel(config, 2, 3.)
        self.assertEqual(other.seconds_per_turn, {CW: 3., CCW: 3.})
        self.assertEqual(other.backlash, 0.)

    def test_adds_backlash_only_when_changing_direction(self):
        motor = staa.MotorModel(FakeConfig(None, {
            "motor1_cw_seconds_per_turn": 2., "motor1_ccw_seconds_per_turn": 4.,
            "motor1_cw_spin_up_time": 0.1, "motor1_backlash": 0.1}), 1, 3.)
        # Unknown last direction: no backlash
        self.assertEqual(motor.seconds_for(0.5), (1.1, 0.))
        motor.last_direction = CW
        self.assertEqual(motor.seconds_for(0.5), (1.1, 0.))
        seconds, backlash = motor.seconds_for(-0.5)
        self.assertAlmostEqual(seconds, 0.6 * 4.)
        self.assertEqual(backlash, 0.1)

    def test_uncalibrated_timing_is_unchanged(self):
        obj, bed, gcode, sta = make([0., 0.2, 0.1])
        obj.cmd_SCREWS_TILT_ADJUST_MANUAL(FakeGcmd({"SCREW": 2, "DISTANCE": -0.5}))
        self.assertAlmostEqual(bed.turns[1], -0.5)
        self.assertEqual(obj.motors[1].last_direction, CCW)

    def test_manual_turns_use_calibration_and_backlash(self):
        motors = uneven()
        obj, bed, gcode, configfile, probe = make_sim([0., 0., 0.], motors)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(FakeGcmd({"LEVEL": 0}))
        for distance in (0.5, -0.3, -0.2, 0.4):
            before = bed.heights[1]
            obj.cmd_SCREWS_TILT_ADJUST_MANUAL(FakeGcmd({"SCREW": 2, "DISTANCE": distance}))
            self.assertAlmostEqual((bed.heights[1] - before) / PITCH, distance, delta=0.01)


class LearningTest(unittest.TestCase):
    def test_learns_while_levelling_and_keeps_it(self):
        motors = Motors(3, seconds_per_turn=[{CW: 3., CCW: 3.}, {CW: 4.5, CCW: 2.4},
                                             {CW: 4.5, CCW: 2.4}])
        obj, bed, gcode, configfile, probe = make_sim(
            [0., 0.2, -0.1], motors, save_variables=True)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))
        self.assertTrue(is_level(bed))
        # Screw 2 was lowered (counter-clockwise) and screw 3 raised, so each
        # learned the speed for the way it turned. Screw 1 didn't move.
        self.assertGreater(obj.motors[2].seconds_per_turn[CW], 3.)
        self.assertLess(obj.motors[1].seconds_per_turn[CCW], 3.)
        self.assertEqual(obj.motors[0].seconds_per_turn, {CW: 3., CCW: 3.})
        sv = obj.printer.objects["save_variables"].variables
        self.assertAlmostEqual(sv["staa_motor2_ccw_seconds_per_turn"],
                               obj.motors[1].seconds_per_turn[CCW], places=3)
        self.assertIn(sv["staa_motor2_last_direction"], (CW, CCW))
        self.assertEqual(sv["staa_motor2_last_direction"], obj.motors[1].last_direction)

        # A fresh start picks up where it left off
        learned = obj.motors[1].seconds_per_turn[CCW]
        obj.motors[1].seconds_per_turn[CCW] = 3.
        obj.motors[1].last_direction = None
        obj._load_motor_state()
        self.assertAlmostEqual(obj.motors[1].seconds_per_turn[CCW], learned, places=3)
        self.assertEqual(obj.motors[1].last_direction, sv["staa_motor2_last_direction"])

    def test_learning_can_be_turned_off(self):
        motors = Motors(3, seconds_per_turn=[{CW: 4.5, CCW: 4.5}] * 3)
        obj, bed, gcode, configfile, probe = make_sim(
            [0., 0.2, -0.1], motors, save_variables=True)
        obj.learn_motor_speed = False
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))
        self.assertTrue(all(m.seconds_per_turn == {CW: 3., CCW: 3.} for m in obj.motors))
        sv = obj.printer.objects["save_variables"].variables
        self.assertNotIn("staa_motor2_ccw_seconds_per_turn", sv)
        # Which way each motor last turned is still kept, for backlash
        self.assertEqual(sv["staa_motor3_last_direction"], CW)

    def test_ignores_readings_that_suggest_an_unlikely_speed(self):
        obj, bed, gcode, sta = make([0., 0.2, -0.1])
        obj.turns_per_mm = 2.
        obj.last_turn = (1, CW, 0.5, 1.5, 0.)
        # Moved a tenth as far as expected
        obj._learn_from_turn(1, 0., 0.025)
        self.assertEqual(obj.motors[1].seconds_per_turn[CW], 3.)
        # Moved the wrong way
        obj._learn_from_turn(1, 0., -0.25)
        self.assertEqual(obj.motors[1].seconds_per_turn[CW], 3.)
        # Too small to tell from the noise
        obj.last_turn = (1, CW, 0.05, 0.15, 0.)
        obj._learn_from_turn(1, 0., 0.0125)
        self.assertEqual(obj.motors[1].seconds_per_turn[CW], 3.)
        # A believable reading moves it part of the way
        obj.last_turn = (1, CW, 0.5, 1.5, 0.)
        obj._learn_from_turn(1, 0., 0.2)
        self.assertAlmostEqual(obj.motors[1].seconds_per_turn[CW], 3. + 0.3 * (3.75 - 3.))


if __name__ == "__main__":
    unittest.main()
