import configparser
import os
import unittest

try:
    import jinja2
except ImportError:  # pragma: no cover
    jinja2 = None

CFG = os.path.join(os.path.dirname(__file__), "..", "screws_tilt_adjust_auto.cfg")


class MacroError(Exception):
    pass


def load_macros():
    # Same comment handling Klipper's config reader uses
    parser = configparser.RawConfigParser(
        strict=False, inline_comment_prefixes=(';', '#'))
    with open(CFG, encoding="utf-8") as f:
        parser.read_file(f)
    return {section.split(" ", 1)[1]: parser.get(section, "gcode")
            for section in parser.sections()
            if section.startswith("gcode_macro ")}


def render(gcode, printer, params=None):
    # Same delimiters Klipper's gcode_macro uses
    env = jinja2.Environment('{%', '%}', '{', '}')

    def raise_error(msg):
        raise MacroError(msg)

    out = env.from_string(gcode).render(
        printer=printer, params=params or {}, action_raise_error=raise_error)
    return [line.strip() for line in out.split("\n")
            if line.strip() and not line.strip().startswith("#")]


def printer_state(settings=None, heaters=None):
    heaters = heaters or {}
    printer = {
        "configfile": {"settings": settings if settings is not None
                       else {"respond": {}, "screws_tilt_adjust": {}}},
        "heaters": {"available_heaters": list(heaters)},
    }
    for name, (temp, target) in heaters.items():
        printer[name] = {"temperature": temp, "target": target}
    return printer


@unittest.skipIf(jinja2 is None, "jinja2 not installed")
class TestLevelBedScrewsDialog(unittest.TestCase):
    def setUp(self):
        self.macros = load_macros()

    def dialog(self, **kwargs):
        return render(self.macros["LEVEL_BED_SCREWS"], printer_state(**kwargs))

    def buttons(self, lines):
        return [l for l in lines if "action:prompt_button " in l]

    def test_opens_and_shows_a_prompt(self):
        lines = self.dialog()
        self.assertIn('RESPOND TYPE=command MSG="action:prompt_begin Level bed screws"', lines)
        self.assertEqual(lines[-1], 'RESPOND TYPE=command MSG="action:prompt_show"')

    def test_standard_and_one_by_one_without_eddy_probe(self):
        buttons = self.buttons(self.dialog())
        self.assertEqual(len(buttons), 2)
        self.assertIn("MODE=standard", buttons[0])
        self.assertIn("MODE=one_by_one", buttons[1])

    def test_continuous_offered_with_eddy_probe(self):
        settings = {"respond": {}, "probe_eddy_current btt_eddy": {}}
        buttons = self.buttons(self.dialog(settings=settings))
        self.assertEqual(len(buttons), 3)
        self.assertIn("MODE=continuous", buttons[2])

    def test_lists_heaters_with_a_target(self):
        lines = self.dialog(heaters={
            "heater_bed": (41.2, 60.0),
            "extruder": (120.0, 200.0),
            "heater_generic chamber": (30.0, 0.0),
        })
        text = [l for l in lines if "action:prompt_text" in l][0]
        self.assertIn("Bed 41/60°C, Extruder 120/200°C.", text)
        self.assertNotIn("Chamber", text)

    def test_says_so_when_no_heaters_are_on(self):
        lines = self.dialog(heaters={"heater_bed": (20.0, 0.0)})
        text = [l for l in lines if "action:prompt_text" in l][0]
        self.assertIn("No heaters are on", text)

    def test_messages_survive_klippers_comment_stripping(self):
        # Klipper drops anything after # or ; on a gcode line
        for line in self.dialog(heaters={"heater_bed": (41.0, 60.0)}):
            self.assertNotIn("#", line)
            self.assertNotIn(";", line)

    def test_needs_respond_section(self):
        with self.assertRaises(MacroError):
            self.dialog(settings={})


@unittest.skipIf(jinja2 is None, "jinja2 not installed")
class TestLevelBedScrewsRun(unittest.TestCase):
    def setUp(self):
        self.macros = load_macros()

    def run_mode(self, mode=None):
        params = {"MODE": mode} if mode else {}
        return render(self.macros["_LEVEL_BED_SCREWS_RUN"], printer_state(), params)

    def test_closes_dialog_first(self):
        self.assertEqual(self.run_mode()[0], 'RESPOND TYPE=command MSG="action:prompt_end"')

    def test_modes(self):
        self.assertEqual(self.run_mode()[-1], "SCREWS_TILT_ADJUST_AUTO WAIT_FOR_HEATERS=1")
        self.assertEqual(self.run_mode("standard")[-1], "SCREWS_TILT_ADJUST_AUTO WAIT_FOR_HEATERS=1")
        self.assertEqual(self.run_mode("one_by_one")[-1],
                         "SCREWS_TILT_ADJUST_AUTO WAIT_FOR_HEATERS=1 ONE_BY_ONE=1")
        self.assertEqual(self.run_mode("continuous")[-1],
                         "SCREWS_TILT_ADJUST_AUTO WAIT_FOR_HEATERS=1 CONTINUOUS=1")

    def test_cancel_closes_dialog(self):
        lines = render(self.macros["_LEVEL_BED_SCREWS_CLOSE"], printer_state())
        self.assertEqual(lines, ['RESPOND TYPE=command MSG="action:prompt_end"'])


if __name__ == "__main__":
    unittest.main()
