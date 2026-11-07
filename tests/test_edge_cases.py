"""Paths the scenario tests in the other files don't reach: error handling,
fallbacks for other Klipper versions, and the manual command."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_one_by_one import CommandError, make  # noqa: E402
from test_screws_tilt_adjust_auto import (  # noqa: E402
    LEVEL, OFF, CommandError as HeaterCommandError, FakeBoard, FakeConfig, FakeGcmd as HeaterGcmd, FakeGcode,
    FakePrinter, FakeReactor, TestBase, adjust, staa)


class OutputParsingTest(unittest.TestCase):
    def test_ignores_output_when_not_measuring(self):
        obj, bed, gcode, sta = make([0., 0.])
        obj._handle_output("b : x=0, y=0, z=0.50000 : adjust CW 00:30")
        self.assertEqual(obj.heights, [])
        self.assertEqual(obj.measurements, [])

    def test_ignores_unrelated_output_while_measuring(self):
        obj, bed, gcode, sta = make([0., 0.])
        obj.active = True
        obj._handle_output("// Some other message")
        self.assertEqual(obj.heights, [])
        self.assertEqual(obj.measurements, [])

    def test_reads_negative_heights(self):
        obj, bed, gcode, sta = make([0., 0.])
        obj.active = True
        obj._handle_output("b : x=1.5, y=-2, z=-0.12500 : adjust CCW 01:05")
        self.assertEqual(obj.heights, [-0.125])
        self.assertEqual(obj.measurements, [
            {"direction": "CCW", "hours": "01", "minutes": "05"}])


class BoardConnectionTest(TestBase):
    def use_serial(self, reads):
        class Serial(FakeBoard):
            def __init__(self, **kwargs):
                FakeBoard.__init__(self)
                self.kwargs = kwargs

            def read(self, n):
                return reads.pop(0) if reads else b""

        original = staa.serial.Serial
        staa.serial.Serial = Serial
        self.addCleanup(setattr, staa.serial, "Serial", original)

    def test_opens_the_configured_port(self):
        obj = self.make([])
        del obj._connect_to_board  # use the real one
        self.use_serial([b"x"])
        obj._connect_to_board()
        self.assertEqual(obj.board.kwargs, {
            "port": "/dev/null", "baudrate": 115200, "timeout": 0})
        self.assertEqual(self.reactor.paused, 0.)

    def test_stops_waiting_for_handshake_after_timeout(self):
        obj = self.make([])
        del obj._connect_to_board
        self.use_serial([])
        obj._connect_to_board()
        # Gives up waiting, but keeps the connection open
        self.assertIsNotNone(obj.board)
        self.assertGreaterEqual(self.reactor.now, staa.HANDSHAKE_TIMEOUT)
        self.assertLess(self.reactor.now, staa.HANDSHAKE_TIMEOUT + 0.1)

    def test_disconnect_without_a_board_does_nothing(self):
        obj = self.make([])
        obj._disconnect_from_board()
        self.assertIsNone(obj.board)

    def test_disconnect_stops_motors_and_closes(self):
        obj = self.make([])
        obj._connect_to_board()
        board = obj.board
        obj._disconnect_from_board()
        self.assertEqual(board.writes, ["0"])
        self.assertTrue(board.closed)
        self.assertIsNone(obj.board)


class ManualCommandTest(TestBase):
    def test_turns_one_screw(self):
        obj = self.make([], config={"full_turn_time_in_seconds": 4})
        obj.cmd_SCREWS_TILT_ADJUST_MANUAL(HeaterGcmd({"SCREW": 3, "DISTANCE": -0.25}))
        # Stopped after the turn, then again when disconnecting
        self.assertEqual(self.boards[0].writes, ["-3", "0", "0"])
        self.assertAlmostEqual(self.reactor.paused, 1.0)
        self.assert_motors_stopped()

    def test_missing_screw_is_an_error(self):
        obj = self.make([])
        with self.assertRaises(HeaterCommandError):
            obj.cmd_SCREWS_TILT_ADJUST_MANUAL(HeaterGcmd({"DISTANCE": 0.5}))
        self.assertEqual(self.boards, [])

    def test_missing_distance_is_an_error(self):
        obj = self.make([])
        with self.assertRaises(HeaterCommandError):
            obj.cmd_SCREWS_TILT_ADJUST_MANUAL(HeaterGcmd({"SCREW": 2}))
        self.assertEqual(self.boards, [])


class HomingTest(TestBase):
    def test_homes_first_if_not_homed(self):
        obj = self.make([LEVEL])
        toolhead = self.printer.objects["toolhead"]
        toolhead.get_status = lambda curtime: {"homed_axes": "xy"}
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(HeaterGcmd())
        self.assertEqual(self.gcode.scripts[0], "G28")

    def test_does_not_home_if_already_homed(self):
        obj = self.make([LEVEL])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(HeaterGcmd())
        self.assertNotIn("G28", self.gcode.scripts)


class MotorMovementsTest(TestBase):
    def movements(self, lines):
        obj = self.make([lines])
        obj._measure_bed_tilt()
        return obj._calculate_motor_movements()

    def test_mixed_directions_leave_base_screw_alone(self):
        self.assertEqual(self.movements(OFF), [0, 0.5, -0.25, 0])

    def test_all_clockwise_moves_base_screw_instead(self):
        self.assertEqual(self.movements([
            adjust("b", "CW", "00:30"), adjust("c", "CW", "00:15")]), [-0.25, 0.25, 0])

    def test_all_anticlockwise_moves_base_screw_instead(self):
        self.assertEqual(self.movements([
            adjust("b", "CCW", "01:00"), adjust("c", "CCW", "00:30")]), [0.5, -0.5, 0])


class HeaterListTest(TestBase):
    def test_heaters_param_ignores_spaces_and_empty_names(self):
        obj = self.make([], heaters={"heater_bed": (60., 0.), "extruder": (200., 0.)})
        heaters = obj._lookup_heaters(HeaterGcmd({"HEATERS": " extruder , ,heater_bed,"}))
        self.assertEqual([name for name, heater in heaters], ["extruder", "heater_bed"])

    def test_wait_reports_temperatures_periodically(self):
        obj = self.make([], heaters={"heater_bed": (60., 25.)})
        messages = []
        self.gcode.respond_info = messages.append
        obj._wait_for_heaters(HeaterGcmd(), obj._lookup_heaters(HeaterGcmd()))
        reports = [m for m in messages if m.startswith("STAA: Waiting for heater_bed")]
        # At 0s, 10s and 20s, then at target at 25s
        self.assertEqual(len(reports), 3)
        self.assertEqual(reports[0], "STAA: Waiting for heater_bed 20.0/60.0")


class OneByOneErrorsTest(unittest.TestCase):
    def test_wrong_number_of_heights_is_an_error(self):
        obj, bed, gcode, sta = make([0., 0.2])
        obj.heights = [0.]
        with self.assertRaises(CommandError) as ctx:
            obj._adjust_one_by_one([0, 0.4])
        self.assertIn("Expected 2 screw heights but got 1", str(ctx.exception))

    def test_single_screw_measurement_without_a_height_is_an_error(self):
        obj, bed, gcode, sta = make([0., 0.2, 0.1])
        screws = list(sta.screws)
        points = list(sta.probe_helper.probe_points)
        gcode.output_handler = lambda line: None  # Klipper printed nothing we recognise
        with self.assertRaises(CommandError) as ctx:
            obj._measure_screw(1)
        self.assertIn("Expected 1 screw height but got 0", str(ctx.exception))
        # Klipper's own screw list is put back regardless
        self.assertEqual(sta.screws, screws)
        self.assertEqual(sta.probe_helper.probe_points, points)

    def test_screws_tilt_adjust_results_are_restored(self):
        obj, bed, gcode, sta = make([0., 0.2, 0.1])
        obj._measure_screw(1)
        self.assertEqual(sta.results, {"original": True})

    def test_works_with_klipper_versions_without_results(self):
        obj, bed, gcode, sta = make([0., 0.2, 0.1])
        del sta.results
        self.assertAlmostEqual(obj._measure_screw(1), 0.2)
        # Nothing to restore, so whatever the measurement left is kept
        self.assertEqual(sta.results, {})

    def test_screws_tilt_adjust_without_probe_points_is_an_error(self):
        obj, bed, gcode, sta = make([0., 0.2, 0.1])
        del sta.probe_helper.probe_points
        with self.assertRaises(CommandError) as ctx:
            obj._lookup_screws_tilt_adjust()
        self.assertIn("supported Klipper version", str(ctx.exception))

    def test_measurement_with_no_height_change_cannot_learn_turns(self):
        obj, bed, gcode, sta = make([0., 0.])
        obj.heights = [1., 1.]
        obj.measurements = [{"direction": "CW", "hours": "00", "minutes": "30"}]
        with self.assertRaises(CommandError) as ctx:
            obj._learn_turns_per_mm()
        self.assertIn("turns per mm", str(ctx.exception))


class ScanningProbeTest(unittest.TestCase):
    def make(self, scan_height=2.):
        obj, bed, gcode, sta = make([0., 0.])
        obj.scan_height = scan_height
        return obj

    def test_reads_height_from_newer_probe_results(self):
        obj = self.make()
        result = types.SimpleNamespace(bed_z=0.25)
        self.assertEqual(obj._probe_result_z(result), 0.25)

    def test_reads_height_from_older_probe_results(self):
        obj = self.make()
        self.assertEqual(obj._probe_result_z([1., 2., 0.25]), 0.25)

    def test_travel_height_uses_horizontal_move_z(self):
        obj = self.make()
        sta = types.SimpleNamespace(probe_helper=types.SimpleNamespace(
            horizontal_move_z=8., default_horizontal_move_z=6.))
        self.assertEqual(obj._travel_height(sta), 8.)

    def test_travel_height_falls_back_to_default_horizontal_move_z(self):
        obj = self.make()
        sta = types.SimpleNamespace(probe_helper=types.SimpleNamespace(
            default_horizontal_move_z=6.))
        self.assertEqual(obj._travel_height(sta), 6.)

    def test_travel_height_falls_back_to_5mm(self):
        obj = self.make()
        sta = types.SimpleNamespace(probe_helper=types.SimpleNamespace())
        self.assertEqual(obj._travel_height(sta), 5.)

    def test_travel_height_is_never_below_scan_height(self):
        obj = self.make(scan_height=12.)
        sta = types.SimpleNamespace(probe_helper=types.SimpleNamespace(horizontal_move_z=8.))
        self.assertEqual(obj._travel_height(sta), 12.)

    def test_no_probe_is_a_clear_error(self):
        obj = self.make()
        obj.printer.lookup_objects = lambda module: []
        with self.assertRaises(CommandError) as ctx:
            obj._lookup_scanning_probe()
        self.assertIn("probe_eddy_current", str(ctx.exception))


class ConfigTest(unittest.TestCase):
    def test_load_config_reads_settings_and_registers_commands(self):
        reactor = FakeReactor()
        gcode = FakeGcode(reactor, [])
        registered = []
        gcode.register_command = lambda name, func, desc=None: registered.append(name)
        printer = FakePrinter(reactor, {"gcode": gcode})
        obj = staa.load_config(FakeConfig(printer, {
            "serial_device": "/dev/ttyUSB0", "serial_baud": 9600,
            "full_turn_time_in_seconds": 3, "maximum_attempts": 10,
            "scan_height": 2., "scan_sample_time": 0.5}))
        self.assertIsInstance(obj, staa.ScrewsTiltAdjustAuto)
        self.assertEqual(obj.serial_device, "/dev/ttyUSB0")
        self.assertEqual(registered, ["SCREWS_TILT_ADJUST_AUTO", "SCREWS_TILT_ADJUST_MANUAL"])
        self.assertEqual(gcode.output_handler, obj._handle_output)


if __name__ == "__main__":
    unittest.main()
