import collections
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_one_by_one import CommandError, FakeGcmd, FakeHeaters, is_level, make  # noqa: E402

ProbeResult = collections.namedtuple('probe_result', [
    'bed_x', 'bed_y', 'bed_z', 'test_x', 'test_y', 'test_z'])

# The eddy probe's readings are offset from what SCREWS_TILT_CALCULATE
# reports, as they would be if a different probe or method did the full
# measurement. Continuous mode must only rely on how the reading changes.
SCAN_OFFSET = 0.37


class FakeToolhead:
    def __init__(self):
        self.pos = [0., 0., 10.]
        self.moves = []

    def get_kinematics(self):
        return self

    def get_status(self, eventtime):
        return {"homed_axes": "xyz"}

    def manual_move(self, coord, speed):
        for i, c in enumerate(coord):
            if c is not None:
                self.pos[i] = c
        self.moves.append(list(self.pos))

    def wait_moves(self):
        pass


class FakeScanSession:
    def __init__(self, probe):
        self.probe = probe
        self.pending = []
        self.ended = False

    def run_probe(self, gcmd):
        assert not self.ended
        x, y, z = self.probe.toolhead.pos
        # The probe stays put while it reads
        assert z == self.probe.scan_z, z
        index = self.probe.coords.index((x, y))
        self.probe.reads.append(index)
        bed_z = self.probe.bed.heights[index] + SCAN_OFFSET
        self.pending.append(ProbeResult(x, y, bed_z, x, y, z))

    def pull_probed_results(self):
        results, self.pending = self.pending, []
        return results

    def end_probe_session(self):
        self.ended = True


class FakeEddyProbe:
    def __init__(self, bed, toolhead, scan_z=2.):
        self.bed = bed
        self.toolhead = toolhead
        self.scan_z = scan_z
        self.coords = [(10. * i, 10. * i) for i in range(len(bed.heights))]
        self.sessions = []
        self.reads = []

    def get_probe_params(self, gcmd=None):
        return {"lift_speed": 5.}

    def start_probe_session(self, gcmd):
        assert gcmd.params["METHOD"] == "scan"
        session = FakeScanSession(self)
        self.sessions.append(session)
        return session


class FakeCommand:
    def __init__(self, params):
        self.params = params


def make_continuous(heights, efficiency=1., attempts=10, eddy=True):
    obj, bed, gcode, sta = make(heights, efficiency, attempts)
    toolhead = FakeToolhead()
    probe = FakeEddyProbe(bed, toolhead)
    gcode.create_gcode_command = lambda cmd, line, params: FakeCommand(params)
    objects = obj.printer.objects
    objects["toolhead"] = toolhead
    objects["probe"] = probe
    if eddy:
        objects["probe_eddy_current my_eddy"] = probe
    obj.printer.lookup_objects = lambda module: [
        (n, o) for n, o in objects.items() if n.startswith(module + " ")]
    return obj, bed, gcode, probe, toolhead


def full_measurements(gcode):
    return [s for s in gcode.scripts if s.startswith("probed")]


class ContinuousTest(unittest.TestCase):
    def test_watches_each_screw_while_turning_it(self):
        obj, bed, gcode, probe, toolhead = make_continuous(
            [0., 0.2, -0.1, 0.05], efficiency=0.7)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"CONTINUOUS": 1}))

        self.assertTrue(is_level(bed))
        # Only full measurements are probed; single screws are watched
        self.assertTrue(all(p == "probed 1,2,3,4" for p in full_measurements(gcode)))
        # One session per screw that needed moving, read several times
        self.assertEqual(len(probe.sessions), 3)
        self.assertTrue(all(s.ended for s in probe.sessions))
        self.assertEqual(sorted(set(probe.reads)), [1, 2, 3])
        self.assertEqual(sorted(probe.reads), probe.reads)
        self.assertGreater(probe.reads.count(1), 2)
        self.assertIn("G28 Z", gcode.scripts)
        # Lifted clear of the bed afterwards
        self.assertGreater(toolhead.pos[2], probe.scan_z)

    def test_overshoot_turns_back(self):
        # Motors that turn further than asked make the screw go past level,
        # so the motor has to go back the other way
        obj, bed, gcode, probe, toolhead = make_continuous([0., 0.2, -0.1], efficiency=1.6)
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"CONTINUOUS": 1}))
        self.assertTrue(is_level(bed))
        writes = obj.last_board.writes
        self.assertIn("2", writes)
        self.assertIn("-2", writes)

    def test_already_level_does_nothing(self):
        obj, bed, gcode, probe, toolhead = make_continuous([0., 0., 0.])
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"CONTINUOUS": 1}))
        self.assertEqual(probe.sessions, [])
        self.assertNotIn("G28 Z", gcode.scripts)

    def test_screw_that_never_settles_fails_and_cleans_up(self):
        obj, bed, gcode, probe, toolhead = make_continuous(
            [0., 0.2, -0.1], efficiency=0., attempts=3)
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"CONTINUOUS": 1}))
        self.assertIn("Unable to reach level", str(ctx.exception))
        self.assertEqual(obj.last_board.writes[-1], "0")
        self.assertTrue(all(s.ended for s in probe.sessions))

    def test_needs_an_eddy_probe(self):
        obj, bed, gcode, probe, toolhead = make_continuous([0., 0.2, 0.1], eddy=False)
        with self.assertRaises(CommandError) as ctx:
            obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"CONTINUOUS": 1}))
        self.assertIn("probe_eddy_current", str(ctx.exception))
        # Fails before doing anything
        self.assertEqual(gcode.scripts, [])

    def test_combines_with_wait_for_heaters(self):
        obj, bed, gcode, probe, toolhead = make_continuous([0., 0.2, -0.1], efficiency=0.7)
        obj.printer.objects["heaters"] = FakeHeaters()
        obj.cmd_SCREWS_TILT_ADJUST_AUTO(FakeGcmd({"CONTINUOUS": 1, "WAIT_FOR_HEATERS": 1}))
        self.assertTrue(is_level(bed))
        self.assertGreater(len(probe.sessions), 0)


if __name__ == "__main__":
    unittest.main()
