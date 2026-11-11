#!/usr/bin/env python3
"""Adds, fixes or removes this extra's [update_manager] section in moonraker.conf.

Used by install.sh. Any existing section for this extra is replaced in place,
including ones added by hand under an older name or with outdated values, so
running it again never adds a second copy. The rest of the file, comments
included, is left as it was.

Usage:
    moonraker_update_manager.py install MOONRAKER_CONF REPO_PATH
    moonraker_update_manager.py uninstall MOONRAKER_CONF REPO_PATH

Prints "added", "updated", "unchanged", "removed" or "absent".
"""
import os
import re
import sys

NAME = "screws-tilt-adjust-auto"
# Names the README has used for this section before.
OLD_NAMES = ("screws_tilt_adjust_auto", "klipper-screws-tilt-adjust-auto")
ORIGIN = "https://github.com/nicole-ashley/klipper-screws-tilt-adjust-auto"
REPO_ID = "nicole-ashley/klipper-screws-tilt-adjust-auto"

HEADER = re.compile(r"^\[([^\]]+)\]")
OPTION = re.compile(r"^([A-Za-z0-9_]+)\s*[:=]\s*(.*?)\s*$")


def display_path(path):
    """Writes the repo path the way the README does, with ~ for home."""
    home = os.path.expanduser("~")
    path = os.path.abspath(path)
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def section_text(repo_path):
    return (f"[update_manager {NAME}]\n"
            f"type: git_repo\n"
            f"path: {display_path(repo_path)}\n"
            f"origin: {ORIGIN}\n"
            f"primary_branch: main\n"
            f"managed_services: klipper\n")


def split_sections(lines):
    """Splits the file into a preamble and one chunk per section.

    Blank and comment lines at the end of a section stay outside it, since
    they usually belong to whatever comes next.
    """
    chunks = [{"name": None, "lines": []}]
    for line in lines:
        match = HEADER.match(line)
        if match:
            chunks.append({"name": match.group(1).strip(), "lines": []})
        chunks[-1]["lines"].append(line)
    out = []
    for chunk in chunks:
        body = chunk["lines"]
        if chunk["name"] is None:
            out.append(chunk)
            continue
        end = len(body)
        while end > 1 and (not body[end - 1].strip()
                           or body[end - 1].lstrip()[:1] in ("#", ";")):
            end -= 1
        out.append({"name": chunk["name"], "lines": body[:end]})
        if body[end:]:
            out.append({"name": None, "lines": body[end:]})
    return out


def options(chunk):
    result = {}
    for line in chunk["lines"][1:]:
        match = OPTION.match(line)
        if match:
            result[match.group(1).lower()] = match.group(2)
    return result


def is_ours(chunk, repo_path):
    name = chunk["name"]
    if name is None:
        return False
    kind, _, label = name.partition(" ")
    if kind != "update_manager":
        return False
    label = label.strip()
    if label == NAME or label in OLD_NAMES:
        return True
    opts = options(chunk)
    if REPO_ID in opts.get("origin", "").lower():
        return True
    path = opts.get("path")
    if path:
        try:
            return (os.path.realpath(os.path.expanduser(path))
                    == os.path.realpath(repo_path))
        except OSError:
            return False
    return False


def update(text, repo_path, remove=False):
    """Returns the new file text and what was done."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    chunks = split_sections(lines)
    wanted = None if remove else section_text(repo_path)
    found = False
    gap = False
    out = []
    for chunk in chunks:
        if is_ours(chunk, repo_path):
            # Keep the first one in its place and drop any duplicates.
            if wanted and not found:
                out.append(wanted)
            else:
                # Don't leave a run of blank lines where it was.
                kept = "".join(out).rstrip("\n")
                out = [kept + "\n"] if kept else []
                gap = True
            found = True
            continue
        part = "".join(chunk["lines"])
        if gap:
            part = part.lstrip("\n")
            if part and out:
                part = "\n" + part
            gap = not part
        out.append(part)
    new = "".join(out)
    if not found:
        if not wanted:
            return text, "absent"
        if new and not new.endswith("\n\n"):
            new += "\n" if new.endswith("\n") else "\n\n"
        new += wanted
        return new, "added"
    if not wanted:
        return new, "removed"
    if new == text or new == text + "\n":
        return text, "unchanged"
    return new, "updated"


def main(argv):
    if len(argv) != 4 or argv[1] not in ("install", "uninstall"):
        sys.stderr.write(__doc__)
        return 2
    conf = argv[2]
    with open(conf, encoding="utf-8") as f:
        text = f.read()
    new, result = update(text, argv[3], remove=argv[1] == "uninstall")
    if new != text:
        # Write in place so a symlinked moonraker.conf stays a symlink.
        with open(conf, "w", encoding="utf-8") as f:
            f.write(new)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
