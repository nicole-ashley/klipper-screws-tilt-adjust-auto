import os
import sys
import types
import unittest

# pyserial isn't needed for these tests, so stub it before importing the module
sys.modules.setdefault("serial", types.SimpleNamespace(Serial=None))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import screws_tilt_adjust_auto as staa  # noqa: E402


class CommandError(Exception):
    pass


class ConfigError(Exception):
    pass


class FakeConfig:
    def __init__(self, printer, values):
        self.printer = printer
        self.values = values

    def get(self, name, default=None):
        return self.values.get(name, default)

    def getfloat(self, name, default=None, **kwargs):
        value = self.values.get(name, default)
        return None if value is None else float(value)

    def getboolean(self, name, default=None, **kwargs):
        return bool(self.values.get(name, default))

    def getint(self, name, default=None, **kwargs):
        return int(self.values.get(name, default))

    def get_printer(self):
        return self.printer


class FakeGcmd:
    def __init__(self, params=None):
        self.params = params or {}

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_float(self, name, default=None, **kwargs):
        value = self.params.get(name)
        return default if value is None else float(value)

    def get_int(self, name, default=None, **kwargs):
        value = self.params.get(name)
        return default if value is None else int(value)

    def get_commandline(self):
        return "TEST"

    def error(self, msg):
        return CommandError(msg)


class FakeReactor:
    """A clock that only moves when the code under test pauses."""

    def __init__(self):
        self.now = 0.
        self.paused = 0.

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.paused += max(0., waketime - self.now)
        self.now = max(self.now, waketime)
        return self.now


class FakeHeater:
    """At target once the clock reaches ready_at. target=0 means off."""

    def __init__(self, reactor, target, ready_at):
        self.reactor = reactor
        self.target = target
        self.ready_at = ready_at

    def get_temp(self, eventtime):
        temp = self.target if eventtime >= self.ready_at else 20.
        return temp, self.target

    def check_busy(self, eventtime):
        return eventtime < self.ready_at


class FakeHeaters:
    def __init__(self, heaters):
        self.heaters = heaters

    def get_all_heaters(self):
        return list(self.heaters)

    def lookup_heater(self, name):
        if name not in self.heaters:
            raise ConfigError(name)
        return self.heaters[name]


class FakeScrewsTiltAdjust:
    """Holds results the way Klipper's screws_tilt_adjust does."""

    def __init__(self):
        self.screws = []
        self.probe_helper = types.SimpleNamespace(probe_points=[])
        self.results = {}

    def get_status(self, eventtime):
        return {"error": False, "max_deviation": 10., "results": self.results}


class FakeGcode:
    """Plays back SCREWS_TILT_CALCULATE results, one results dict per call.
    Each call takes `probe_time` seconds on the fake clock."""

    error = CommandError

    def __init__(self, reactor, rounds, probe_time=60.):
        self.reactor = reactor
        self.rounds = list(rounds)
        self.probe_time = probe_time
        self.scripts = []
        self.sta = FakeScrewsTiltAdjust()

    def register_command(self, *args, **kwargs):
        pass

    def respond_info(self, msg):
        pass

    def run_script_from_command(self, script):
        self.scripts.append(script)
        if script.startswith("SCREWS_TILT_CALCULATE"):
            self.reactor.now += self.probe_time
            result = self.rounds.pop(0)
            if isinstance(result, Exception):
                raise result
            # Klipper replaces its results with a new dict on every run
            self.sta.results = result.copy()


class FakeBoard:
    def __init__(self):
        self.writes = []
        self.closed = False

    def read(self, n):
        return b"\n"

    def write(self, data):
        self.writes.append(data.decode())

    def close(self):
        self.closed = True


class FakeToolhead:
    def get_kinematics(self):
        return self

    def get_status(self, curtime):
        return {"homed_axes": "xyz"}


class FakePrinter:
    config_error = ConfigError

    def __init__(self, reactor, objects):
        self.reactor = reactor
        self.objects = objects
        self.shutdown = False

    def lookup_object(self, name, *default):
        if default:
            return self.objects.get(name, default[0])
        return self.objects[name]

    def get_reactor(self):
        return self.reactor

    def is_shutdown(self):
        return self.shutdown


def adjust(direction, hhmm, z=0.):
    return {"z": z, "sign": direction, "adjust": hhmm, "is_base": False}


def results(*screws):
    """Klipper's results for screw1 as the base, then the given screws."""
    out = {"screw1": {"z": 0., "sign": "CW", "adjust": "00:00", "is_base": True}}
    for i, screw in enumerate(screws):
        out["screw%d" % (i + 2,)] = screw
    return out


LEVEL = results(adjust("CW", "00:00"), adjust("CW", "00:00"), adjust("CW", "00:00"))
OFF = results(adjust("CW", "00:30"), adjust("CCW", "00:15"), adjust("CW", "00:00"))


class TestBase(unittest.TestCase):
    def make(self, rounds, heaters=None, config=None):
        self.reactor = FakeReactor()
        self.gcode = FakeGcode(self.reactor, rounds)
        heaters = {name: FakeHeater(self.reactor, *spec)
                   for name, spec in (heaters or {}).items()}
        self.printer = FakePrinter(self.reactor, {
            "gcode": self.gcode,
            "toolhead": FakeToolhead(),
            "heaters": FakeHeaters(heaters),
            "screws_tilt_adjust": self.gcode.sta,
        })
        values = {"serial_device": "/dev/null", "serial_baud": 115200}
        values.update(config or {})
        obj = staa.ScrewsTiltAdjustAuto(FakeConfig(self.printer, values))
        self.boards = []

        def connect():
            obj.board = FakeBoard()
            self.boards.append(obj.board)
        obj._connect_to_board = connect
        return obj

    def calculate_count(self):
        return sum(s.startswith("SCREWS_TILT_CALCULATE") for s in self.gcode.scripts)

    def home_z_count(self):
        return self.gcode.scripts.count("G28 Z")

    def assert_motors_stopped(self):
        for board in self.boards:
            self.assertEqual(board.writes[-1], "0")
            self.assertTrue(board.closed)


class AutoAdjustTest(TestBase):
    def test_already_level_does_nothing(self):
        obj = self.make([LEVEL])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(self.calculate_count(), 1)
        self.assertEqual(self.home_z_count(), 0)
        self.assert_motors_stopped()

    def test_adjusts_then_homes_z(self):
        obj = self.make([OFF, LEVEL])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(self.calculate_count(), 2)
        self.assertEqual(self.home_z_count(), 1)

    def test_motor_waits_do_not_block_klipper(self):
        obj = self.make([OFF, LEVEL], config={"full_turn_time_in_seconds": 4})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        # 0.5 turns + 0.25 turns at 4s per turn, all through reactor.pause()
        self.assertAlmostEqual(self.reactor.paused, 3.0)

    def test_gives_up_after_maximum_attempts_and_stops_motors(self):
        obj = self.make([OFF] * 3, config={"maximum_attempts": 3})
        with self.assertRaises(CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertEqual(self.calculate_count(), 3)
        self.assertEqual(self.home_z_count(), 1)
        self.assert_motors_stopped()

    def test_measurement_error_stops_motors(self):
        obj = self.make([OFF, RuntimeError("probe failed")])
        with self.assertRaises(RuntimeError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assert_motors_stopped()

    def test_manual_command_stops_motors_on_error(self):
        obj = self.make([])
        obj._pause = lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            obj.cmd_SCREWS_TILT_ADJUST_MANUAL(FakeGcmd({"SCREW": 2, "DISTANCE": 0.5}))
        self.assertEqual(self.boards[0].writes, ["2", "0"])
        self.assert_motors_stopped()

    def test_handshake_wait_does_not_block_klipper(self):
        obj = self.make([])
        del obj._connect_to_board  # use the real one
        reads = [b"", b"", b"x"]

        class Serial(FakeBoard):
            def __init__(self, **kwargs):
                FakeBoard.__init__(self)
                self.kwargs = kwargs

            def read(self, n):
                return reads.pop(0)

        original = staa.serial.Serial
        staa.serial.Serial = Serial
        try:
            obj._connect_to_board()
        finally:
            staa.serial.Serial = original
        self.assertEqual(obj.board.kwargs["timeout"], 0)
        self.assertEqual(reads, [])
        self.assertAlmostEqual(self.reactor.paused, 0.1)


class WaitForHeatersTest(TestBase):
    HEAT = {"WAIT_FOR_HEATERS": 1}

    def test_no_heaters_targeted_adjusts_once(self):
        obj = self.make([OFF, LEVEL], heaters={"heater_bed": (0., 0.)})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assertEqual(self.calculate_count(), 2)
        self.assertEqual(self.home_z_count(), 1)

    def test_already_hot_adjusts_once_and_homes_z(self):
        obj = self.make([OFF, LEVEL], heaters={"heater_bed": (60., 0.)})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assertEqual(self.calculate_count(), 2)
        self.assertEqual(self.home_z_count(), 1)
        self.assertEqual(self.reactor.paused, 3.0 * 0.75)

    def test_already_hot_and_level_does_not_home(self):
        obj = self.make([LEVEL], heaters={"heater_bed": (60., 0.)})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assertEqual(self.home_z_count(), 0)

    def test_at_target_before_final_measurement_exits_after_one_pass(self):
        # Probes take 60s each; heater is ready at 50s, so the second
        # (level) measurement starts at temperature
        obj = self.make([OFF, LEVEL], heaters={"heater_bed": (60., 50.)})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assertEqual(self.calculate_count(), 2)
        self.assertEqual(self.home_z_count(), 1)

    def test_reaching_target_during_final_measurement_adjusts_again(self):
        # Heater is ready at 100s, part way through the second (level)
        # measurement, which started at 60s, so it wasn't taken at temperature
        obj = self.make([OFF, LEVEL, OFF, LEVEL], heaters={"heater_bed": (60., 100.)})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assertEqual(self.calculate_count(), 4)
        self.assertEqual(self.home_z_count(), 2)

    def test_waits_for_heaters_then_adjusts_again(self):
        obj = self.make([OFF, LEVEL, OFF, LEVEL],
                        heaters={"heater_bed": (60., 600.)})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assertEqual(self.calculate_count(), 4)
        self.assertGreaterEqual(self.reactor.now, 600.)
        # Second pass happened after the heaters were ready
        self.assertEqual(self.home_z_count(), 2)
        self.assertEqual(len(self.boards), 2)
        self.assert_motors_stopped()

    def test_final_pass_homes_z_even_if_first_pass_was_level(self):
        obj = self.make([LEVEL, OFF, LEVEL], heaters={"heater_bed": (60., 600.)})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assertEqual(self.calculate_count(), 3)
        self.assertEqual(self.gcode.scripts[-1], "G28 Z")
        self.assertEqual(self.home_z_count(), 1)

    def test_final_pass_level_does_not_home_z_again(self):
        obj = self.make([OFF, LEVEL, LEVEL], heaters={"heater_bed": (60., 600.)})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assertEqual(self.home_z_count(), 1)
        self.assertNotEqual(self.gcode.scripts[-1], "G28 Z")

    def test_first_pass_failing_while_heating_is_not_fatal(self):
        obj = self.make([OFF, OFF, OFF, LEVEL], heaters={"heater_bed": (60., 600.)},
                        config={"maximum_attempts": 2})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assertEqual(self.calculate_count(), 4)
        self.assertEqual(self.home_z_count(), 2)

    def test_final_pass_failing_is_an_error(self):
        obj = self.make([LEVEL, OFF, OFF], heaters={"heater_bed": (60., 600.)},
                        config={"maximum_attempts": 2})
        with self.assertRaises(CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assert_motors_stopped()

    def test_ignores_heaters_without_target(self):
        obj = self.make([OFF, LEVEL], heaters={"heater_bed": (60., 0.),
                                               "extruder": (0., 10000.)})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assertEqual(self.calculate_count(), 2)

    def test_waits_for_slowest_heater(self):
        obj = self.make([LEVEL, LEVEL], heaters={"heater_bed": (60., 300.),
                                                 "extruder": (200., 900.)})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assertGreaterEqual(self.reactor.now, 900.)

    def test_heaters_param_limits_which_heaters_are_waited_on(self):
        obj = self.make([LEVEL, LEVEL], heaters={"heater_bed": (60., 300.),
                                                 "extruder": (200., 900.)})
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(dict(self.HEAT, HEATERS="heater_bed")))
        self.assertGreaterEqual(self.reactor.now, 300.)
        self.assertLess(self.reactor.now, 900.)

    def test_unknown_heater_is_an_error(self):
        obj = self.make([], heaters={"heater_bed": (60., 300.)})
        with self.assertRaises(CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(dict(self.HEAT, HEATERS="chamber")))
        self.assertEqual(self.calculate_count(), 0)

    def test_shutdown_while_waiting_is_an_error(self):
        obj = self.make([LEVEL], heaters={"heater_bed": (60., 600.)})
        self.printer.shutdown = True
        with self.assertRaises(CommandError):
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd(self.HEAT))
        self.assertEqual(self.calculate_count(), 1)


if __name__ == "__main__":
    unittest.main()
