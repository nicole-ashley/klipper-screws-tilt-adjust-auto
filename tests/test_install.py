import configparser
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import moonraker_update_manager as mum  # noqa: E402

HOME = os.path.expanduser("~")
REPO = os.path.join(HOME, "screws-tilt-adjust-auto")

BASE = """\
# My Moonraker config
[server]
host: 0.0.0.0

[update_manager]
refresh_interval: 168

# Mainsail
[update_manager mainsail]
type: web
repo: mainsail-crew/mainsail
path: ~/mainsail
"""


def readme_section():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        readme = f.read()
    match = re.search(r"```ini\n(\[update_manager [^\n]*\n.*?)```",
                      readme, re.S)
    return match.group(1)


def parse(text):
    parser = configparser.ConfigParser(strict=True,
                                       inline_comment_prefixes=("#", ";"))
    parser.read_string(text)
    return parser


class UpdateManagerSectionTest(unittest.TestCase):
    def test_matches_the_readme(self):
        self.assertEqual(mum.section_text(REPO), readme_section())

    def test_adds_section_to_the_end(self):
        new, result = mum.update(BASE, REPO)
        self.assertEqual(result, "added")
        self.assertEqual(new, BASE + "\n" + readme_section())
        parse(new)

    def test_adds_to_file_without_trailing_newline(self):
        new, result = mum.update("[server]\nhost: 0.0.0.0", REPO)
        self.assertEqual(result, "added")
        self.assertEqual(new, "[server]\nhost: 0.0.0.0\n\n" + readme_section())

    def test_adds_to_empty_file(self):
        new, result = mum.update("", REPO)
        self.assertEqual((new, result), (readme_section(), "added"))

    def test_running_again_changes_nothing(self):
        once, _ = mum.update(BASE, REPO)
        twice, result = mum.update(once, REPO)
        self.assertEqual(result, "unchanged")
        self.assertEqual(twice, once)

    def test_fixes_hand_added_section_in_place(self):
        manual = ("[update_manager screws_tilt_adjust_auto]\n"
                  "type: git_repo\n"
                  "path: ~/old-place\n"
                  "origin: https://github.com/nicole-ashley/klipper-screws-tilt-adjust-auto.git\n"
                  "primary_branch: master\n"
                  "is_system_service: False\n")
        text = BASE.replace("# Mainsail\n", manual + "\n# Mainsail\n")
        new, result = mum.update(text, REPO)
        self.assertEqual(result, "updated")
        self.assertEqual(new, BASE.replace(
            "# Mainsail\n", readme_section() + "\n# Mainsail\n"))

    def test_finds_section_by_origin_under_any_name(self):
        text = BASE + ("\n[update_manager bed-screws]\n"
                       "type: git_repo\n"
                       "path: ~/screws-tilt-adjust-auto\n"
                       "origin: git@github.com:Nicole-Ashley/klipper-screws-tilt-adjust-auto\n")
        new, result = mum.update(text, REPO)
        self.assertEqual(result, "updated")
        self.assertEqual(new, BASE + "\n" + readme_section())

    def test_finds_section_by_path_under_any_name(self):
        text = BASE + ("\n[update_manager bed-screws]\n"
                       "type: git_repo\n"
                       "path: ~/screws-tilt-adjust-auto\n"
                       "origin: https://example.com/a-fork\n")
        new, result = mum.update(text, REPO)
        self.assertEqual(result, "updated")
        self.assertEqual(new, BASE + "\n" + readme_section())

    def test_removes_duplicates(self):
        text = (BASE + "\n" + readme_section()
                + "\n[update_manager screws_tilt_adjust_auto]\n"
                  "type: git_repo\npath: ~/screws-tilt-adjust-auto\n")
        new, result = mum.update(text, REPO)
        self.assertEqual(result, "updated")
        self.assertEqual(new, BASE + "\n" + readme_section())
        parse(new)

    def test_fixes_last_option_without_newline(self):
        text = BASE + "\n" + readme_section().replace(
            "managed_services: klipper\n", "managed_services: klipper moonraker")
        new, result = mum.update(text, REPO)
        self.assertEqual(result, "updated")
        self.assertEqual(new, BASE + "\n" + readme_section())

    def test_leaves_other_update_manager_sections_alone(self):
        new, _ = mum.update(BASE, REPO)
        parser = parse(new)
        self.assertEqual(parser.get("update_manager mainsail", "repo"),
                         "mainsail-crew/mainsail")
        self.assertEqual(parser.get("update_manager", "refresh_interval"),
                         "168")

    def test_path_outside_home_is_written_in_full(self):
        self.assertIn("path: /opt/screws\n", mum.section_text("/opt/screws"))

    def test_uninstall_removes_section(self):
        text = BASE.replace("# Mainsail\n",
                            readme_section() + "\n# Mainsail\n")
        new, result = mum.update(text, REPO, remove=True)
        self.assertEqual(result, "removed")
        self.assertEqual(new, BASE)

    def test_uninstall_from_end_of_file(self):
        new, result = mum.update(BASE + "\n" + readme_section(), REPO,
                                 remove=True)
        self.assertEqual(result, "removed")
        self.assertEqual(new, BASE)

    def test_uninstall_without_section(self):
        new, result = mum.update(BASE, REPO, remove=True)
        self.assertEqual((new, result), (BASE, "absent"))


class CommandLineTest(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "moonraker_update_manager.py"),
             *args], capture_output=True, text=True, check=True).stdout.strip()

    def test_install_and_uninstall_edit_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real.conf")
            link = os.path.join(tmp, "moonraker.conf")
            with open(real, "w", encoding="utf-8") as f:
                f.write(BASE)
            os.symlink(real, link)
            self.assertEqual(self.run_script("install", link, REPO), "added")
            self.assertEqual(self.run_script("install", link, REPO),
                             "unchanged")
            self.assertTrue(os.path.islink(link))
            with open(real, encoding="utf-8") as f:
                self.assertIn(readme_section(), f.read())
            self.assertEqual(self.run_script("uninstall", link, REPO),
                             "removed")
            with open(real, encoding="utf-8") as f:
                self.assertEqual(f.read(), BASE)


if __name__ == "__main__":
    unittest.main()
