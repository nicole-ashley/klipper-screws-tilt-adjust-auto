import math
import os
import sys
import types
import unittest

# pyserial isn't needed for these tests, so stub it before importing the module
sys.modules.setdefault("serial", types.SimpleNamespace(Serial=None))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import screws_tilt_adjust_auto as staa  # noqa: E402

FULL_TURN_TIME = 3.
PITCH = 0.5  # mm per turn, as Klipper assumes for M3 screws


class CommandError(Exception):
    pass


class FakeConfig:
    def __init__(self, printer, values):
        self.printer = printer
        self.values = values

    def get(self, name, default=None):
        return self.values.get(name, default)

    def getfloat(self, name, default=None, **kwargs):
        return float(self.values.get(name, default))

    def getint(self, name, default=None, **kwargs):
        return int(self.values.get(name, default))

    def get_printer(self):
        return self.printer


class FakeGcmd:
    def __init__(self, params=None):
        self.params = params or {}

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, **kwargs):
        value = self.params.get(name)
        return default if value is None else int(value)

    def get_float(self, name, default=None, **kwargs):
        value = self.params.get(name)
        return default if value is None else float(value)

    def get_commandline(self):
        return "TEST"

    def error(self, msg):
        return CommandError(msg)


class Bed:
    """Screw heights in mm. A motor turns its screw `efficiency` times as
    far as asked, so most screws need more than one go."""

    def __init__(self, heights, efficiency=1.):
        self.heights = list(heights)
        self.efficiency = efficiency
        self.turns = [0.] * len(heights)

    def turn(self, index, turns):
        self.turns[index] += turns
        self.heights[index] += turns * self.efficiency * PITCH


class FakeBoard:
    def __init__(self, bed, reactor):
        self.bed = bed
        self.writes = []
        self.running = 0
        reactor.board = self

    def read(self, n):
        return b"\n"

    def write(self, data):
        self.writes.append(data.decode())
        self.running = int(data.decode())

    def run_for(self, seconds):
        if self.running:
            turns = seconds / FULL_TURN_TIME
            self.bed.turn(abs(self.running) - 1, math.copysign(turns, self.running))

    def close(self):
        pass


class FakeReactor:
    def __init__(self):
        self.now = 0.
        self.board = None

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        if self.board is not None:
            self.board.run_for(waketime - self.now)
        self.now = max(self.now, waketime)
        return self.now


class FakeProbeHelper:
    def __init__(self, points):
        self.probe_points = points


class FakeScrewsTiltAdjust:
    """Mimics the parts of Klipper's screws_tilt_adjust that we touch."""

    def __init__(self, count):
        self.screws = [((10. * i, 10. * i), "screw%d" % (i + 1,)) for i in range(count)]
        self.probe_helper = FakeProbeHelper([c for c, n in self.screws])
        self.results = {"original": True}


class FakeGcode:
    error = CommandError

    def __init__(self, bed, sta):
        self.bed = bed
        self.sta = sta
        self.scripts = []
        self.output_handler = None

    def register_command(self, *args, **kwargs):
        pass

    def register_output_handler(self, handler):
        self.output_handler = handler

    def respond_info(self, msg):
        pass

    def run_script_from_command(self, script):
        self.scripts.append(script)
        if script.startswith("SCREWS_TILT_CALCULATE"):
            self._screws_tilt_calculate()

    def _screws_tilt_calculate(self):
        # Probe the configured points and report like Klipper does,
        # with the first point as the base
        assert [c for c, n in self.sta.screws] == self.sta.probe_helper.probe_points
        all_coords = [(10. * i, 10. * i) for i in range(len(self.bed.heights))]
        indexes = [all_coords.index(c) for c, n in self.sta.screws]
        self.scripts.append("probed %s" % ",".join(str(i + 1) for i in indexes))
        self.sta.results = {}
        z_base = self.bed.heights[indexes[0]]
        self.output_handler("%s (base) : x=0, y=0, z=%.5f" % (self.sta.screws[0][1], z_base))
        for (coord, name), index in zip(self.sta.screws[1:], indexes[1:]):
            z = self.bed.heights[index]
            diff = z_base - z
            adjust = 0 if abs(diff) < 0.001 else diff / PITCH
            sign = "CW" if adjust >= 0 else "CCW"
            adjust = abs(adjust)
            full_turns = math.trunc(adjust)
            minutes = round((adjust - full_turns) * 60, 0)
            self.output_handler("%s : x=0, y=0, z=%.5f : adjust %s %02d:%02d" % (
                name, z, sign, full_turns, minutes))


class FakeKinematics:
    def get_status(self, eventtime):
        return {"homed_axes": "xyz"}


class FakeToolhead:
    def get_kinematics(self):
        return FakeKinematics()


class FakePrinter:
    def __init__(self, reactor, gcode, sta):
        self.reactor = reactor
        self.objects = {"gcode": gcode, "toolhead": FakeToolhead()}
        if sta is not None:
            self.objects["screws_tilt_adjust"] = sta

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


def make(heights, efficiency=1., attempts=10, with_sta=True, config=None):
    bed = Bed(heights, efficiency)
    reactor = FakeReactor()
    sta = FakeScrewsTiltAdjust(len(heights)) if with_sta else None
    gcode = FakeGcode(bed, sta or FakeScrewsTiltAdjust(len(heights)))
    printer = FakePrinter(reactor, gcode, sta)
    values = {
        "serial_device": "/dev/null",
        "serial_baud": 9600,
        "full_turn_time_in_seconds": FULL_TURN_TIME,
        "maximum_attempts": attempts,
    }
    values.update(config or {})
    obj = staa.ScrewsTiltAdjustAuto(FakeConfig(printer, values))
    def connect():
        obj.board = obj.last_board = FakeBoard(bed, reactor)
    obj._connect_to_board = connect
    return obj, bed, gcode, sta


def is_level(bed):
    # Level as SCREWS_TILT_CALCULATE sees it: every screw reads 00:00
    # against screw 1
    return all(abs(h - bed.heights[0]) < 0.5 / 120 for h in bed.heights)


class OneByOneTest(unittest.TestCase):
    def test_levels_each_screw_measuring_only_that_screw(self):
        obj, bed, gcode, sta = make([0., 0.2, -0.1, 0.05], efficiency=0.7)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))

        self.assertTrue(is_level(bed))
        probes = [s for s in gcode.scripts if s.startswith("probed")]
        # One full pass, then screws 2, 3 and 4 in order, each until level,
        # then a full pass to confirm
        self.assertEqual(probes[0], "probed 1,2,3,4")
        self.assertEqual(probes[-1], "probed 1,2,3,4")
        singles = probes[1:-1]
        self.assertEqual(sorted(set(singles)), ["probed 2", "probed 3", "probed 4"])
        self.assertEqual(sorted(singles), singles)
        # Motors that don't turn their full amount need more than one go
        self.assertGreater(singles.count("probed 2"), 1)
        self.assertIn("G28 Z", gcode.scripts)

    def test_klipper_screw_list_is_restored(self):
        obj, bed, gcode, sta = make([0., 0.2, -0.1], efficiency=0.7)
        screws = list(sta.screws)
        points = list(sta.probe_helper.probe_points)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))
        self.assertEqual(sta.screws, screws)
        self.assertEqual(sta.probe_helper.probe_points, points)

    def test_uses_a_screw_that_needs_no_movement_as_reference(self):
        # Every screw is lower than the base, so the base screw is moved
        # down instead and screw 3 (the smallest change) is the reference
        obj, bed, gcode, sta = make([0., -0.3, -0.1, -0.2], efficiency=0.8)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))

        self.assertTrue(is_level(bed))
        self.assertEqual(bed.turns[2], 0.)
        probes = [s for s in gcode.scripts if s.startswith("probed")]
        self.assertIn("probed 1", probes)
        self.assertNotIn("probed 3", probes)

    def test_already_level_does_nothing(self):
        obj, bed, gcode, sta = make([0., 0., 0.])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))
        self.assertEqual(bed.turns, [0., 0., 0.])
        self.assertNotIn("G28 Z", gcode.scripts)

    def test_screw_that_never_settles_fails_and_stops_motors(self):
        obj, bed, gcode, sta = make([0., 0.2, -0.1], efficiency=0.3, attempts=2)
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))
        self.assertIn("Unable to reach level", str(ctx.exception))
        self.assertEqual(obj.last_board.writes[-1], "0")
        self.assertEqual(sta.screws[1][1], "screw2")

    def test_missing_screws_tilt_adjust_is_a_clear_error(self):
        obj, bed, gcode, sta = make([0., 0.2, 0.1], with_sta=False)
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1}))
        self.assertIn("screws_tilt_adjust", str(ctx.exception))


class FakeHeater:
    def get_temp(self, eventtime):
        return 20., 0.

    def check_busy(self, eventtime):
        return False


class FakeHeaters:
    def get_all_heaters(self):
        return ["heater_bed"]

    def lookup_heater(self, name):
        return FakeHeater()


class TurnsPerMmTest(unittest.TestCase):
    def learn(self, lines):
        obj, bed, gcode, sta = make([0., 0., 0.])
        obj.active = True
        for line in lines:
            obj._handle_output(line)
        obj._learn_turns_per_mm()
        return obj

    def test_learns_cw_thread_from_measurement(self):
        obj = self.learn([
            "a (base) : x=0, y=0, z=1.00000",
            "b : x=0, y=0, z=0.75000 : adjust CW 00:30",
            "c : x=0, y=0, z=1.10000 : adjust CCW 00:12",
        ])
        self.assertAlmostEqual(obj.turns_per_mm, 2.)
        self.assertAlmostEqual(obj._height_to_turns(0.25), 0.5)

    def test_learns_ccw_thread_from_measurement(self):
        obj = self.learn([
            "a (base) : x=0, y=0, z=1.00000",
            "b : x=0, y=0, z=0.65000 : adjust CCW 00:30",
            "c : x=0, y=0, z=1.00000 : adjust CW 00:00",
        ])
        self.assertAlmostEqual(obj.turns_per_mm, -1 / 0.7)

    def test_under_half_a_minute_is_level(self):
        obj = self.learn([
            "a (base) : x=0, y=0, z=1.00000",
            "b : x=0, y=0, z=0.75000 : adjust CW 00:30",
        ])
        self.assertEqual(obj._height_to_turns(0.004), 0)
        self.assertAlmostEqual(obj._height_to_turns(0.1), 0.2)


class OneByOneWithHeatersTest(unittest.TestCase):
    def test_combines_with_wait_for_heaters(self):
        obj, bed, gcode, sta = make([0., 0.2, -0.1], efficiency=0.7)
        obj.printer.objects["heaters"] = FakeHeaters()
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"ONE_BY_ONE": 1, "WAIT_FOR_HEATERS": 1}))
        self.assertTrue(is_level(bed))
        probes = [s for s in gcode.scripts if s.startswith("probed")]
        self.assertIn("probed 2", probes)


class DefaultModeTest(unittest.TestCase):
    def test_turns_all_screws_each_pass(self):
        obj, bed, gcode, sta = make([0., 0.2, -0.1, 0.05], efficiency=0.7)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertTrue(is_level(bed))
        probes = [s for s in gcode.scripts if s.startswith("probed")]
        self.assertTrue(all(p == "probed 1,2,3,4" for p in probes))
        self.assertIn("G28 Z", gcode.scripts)

    def test_motor_waits_do_not_block(self):
        obj, bed, gcode, sta = make([0., 0.2, 0.1])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd())
        self.assertGreater(obj.reactor.now, 0.)


if __name__ == "__main__":
    unittest.main()
