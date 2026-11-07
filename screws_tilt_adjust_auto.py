# Automatically adjust bed screws using Screws Tilt Adjust and screw motors
#
# Copyright (C) 2024  Nicole Ashley <nicole@ashley.kiwi>
#
# This file may be distributed under the terms of the MIT license.
import contextlib
import re
import serial

HANDSHAKE_TIMEOUT = 30.
HEATER_REPORT_INTERVAL = 10.
# The board stops a motor that hasn't been told to keep running for this
# long, so resend the command more often than that while a motor turns
MOTOR_KEEPALIVE_INTERVAL = 2.
# Waits are split into steps this long so a cancel stops a motor promptly
CANCEL_CHECK_INTERVAL = 0.1
# Commands that cancel a run. Klipper runs one G-code command at a time, so
# these are caught as they arrive rather than queueing behind the run.
CANCEL_COMMANDS = ("SCREWS_TILT_ADJUST_AUTO_CANCEL", "CANCEL_LEVEL_BED_SCREWS")
# Cancelling a print (Mainsail's and Fluidd's Cancel button, and CANCEL_PRINT
# from the console) also stops a run, for one started from a print's start
# G-code. The print cancel itself still runs once the run has stopped.
PRINT_CANCEL_COMMANDS = ("CANCEL_PRINT",)

# How far each of Klipper's screw_thread options moves per turn, in mm, and
# whether a clockwise turn raises the bed (matches screws_tilt_adjust.py)
SCREW_THREADS = {0: (0.5, True), 1: (0.5, False), 2: (0.7, True), 3: (0.7, False),
                 4: (0.8, True), 5: (0.8, False), 6: (1.0, True), 7: (1.0, False)}
# Klipper only supports M3 to M6, so a learned rate outside this range
# (with some margin for rounding) means the readings can't be trusted
MIN_TURNS_PER_MM = 1 / 1.25
MAX_TURNS_PER_MM = 1 / 0.4
# Below this spread of heights the turns can't be learned reliably enough to
# be compared to screw_thread
MIN_SPREAD_TO_CHECK_THREAD = 0.05

# The board drives up to four motors
MAX_MOTORS = 4
CW = 1
CCW = -1
DIRECTION_NAMES = {CW: "cw", CCW: "ccw"}
# How far a learned speed moves toward each new reading. Small enough that
# one odd reading can't take over.
LEARNING_RATE = 0.3
# A turn that suggests a speed further off than this from the current one is
# more likely a bad reading than the motor, so it isn't learned from
MAX_LEARNING_RATIO = 2.
# Calibration moves each motor for 2x, 1x and 2x a short time one way, then
# the same back, so the screw ends up where it started. The first move after
# reversing takes up the slack, and how far it falls short shows the
# backlash. The two moves after it, of different lengths, separate the
# motor's speed from its spin-up time.
CALIBRATION_PATTERN = [(CW, 2), (CW, 1), (CW, 2), (CCW, 2), (CCW, 1), (CCW, 2)]
# In the motor test, a screw has moved if it moved more than this many times
# the probe's noise. Uncalibrated motors can lose much of a small turn to
# spin-up and backlash, so anything clearly more than noise counts.
TEST_NOISE_MULTIPLE = 2
# The test turn must be expected to move the bed at least twice that, and a
# screw that moves more than this many times as far as expected fails
TEST_MAX_FRACTION = 3.
# Prefix for values kept with [save_variables]
VARIABLE_PREFIX = "staa_motor"


# What Mainsail, Fluidd and KlipperScreen send when someone closes a dialog
DIALOG_CLOSE = re.compile(r"^\s*RESPOND\b.*\baction:prompt_end\b", re.IGNORECASE)
CLOSE_DIALOG_COMMAND = "_SCREWS_TILT_ADJUST_AUTO_CLOSE_DIALOG"
CANCEL_BUTTON = "prompt_footer_button Cancel|SCREWS_TILT_ADJUST_AUTO_CANCEL|error"
# Shown before each step on the status line, and when a run finishes
STATUS_LABELS = {"level": "Levelling", "calibrate": "Calibrating",
                 "test": "Motor test", "manual": "Turning screw"}
DONE_MESSAGES = {"level": "Bed screws level", "calibrate": "Calibration done",
                 "test": "Motor test passed", "manual": "Screw turned"}


class Progress:
    """Where a run is up to. Read by Mainsail, Fluidd, KlipperScreen and
    macros through printer.screws_tilt_adjust_auto, and shown on the status
    line and, when asked for, in a dialog that's redrawn at each step.

    Klipper runs one G-code command at a time, so macros like M117 or
    RESPOND can't run while a levelling run holds the lock. Everything here
    is sent from the extra itself instead."""

    def __init__(self, owner):
        self.owner = owner
        self.printer = owner.printer
        self.title = ""
        self.intro = ""
        self.dialog = False
        self.dialog_closed = False
        self.previous_message = None
        self._reset("idle", "")

    def _reset(self, state, mode):
        self.state = state
        self.running = False
        self.mode = mode
        self.heaters = ""
        self.heater_temps = ""
        self.pass_number = 0
        self.max_passes = 0
        self.screw = None
        self.message = ""
        self.screws = []
        self.spread = None
        self.start_spread = None
        self.run_turns = []
        self.error = ""

    def get_status(self):
        return {
            "state": self.state,
            "running": self.running,
            "mode": self.mode,
            "heaters": self.heaters,
            "pass": self.pass_number,
            "max_passes": self.max_passes,
            "screw": self._name(self.screw) if self.screw is not None else "",
            "message": self.message,
            "screws": [dict(s) for s in self.screws],
            "spread": self.spread,
            "start_spread": self.start_spread,
            "run_turns": list(self.run_turns),
            "error": self.error,
        }

    # Run start and end

    def start(self, mode, title, intro, dialog):
        self._reset("starting", mode)
        self.running = True
        self.title = title
        self.intro = intro
        self.dialog = dialog
        self.dialog_closed = False
        display = self._display_status()
        self.previous_message = getattr(display, "message", None)
        self.step("starting", "Starting")

    def finish(self, state, error="", home_z=False):
        self.running = False
        self.state = state
        self.screw = None
        self.error = error
        display = self._display_status()
        if display is not None:
            display.message = self.previous_message
        if state == "done":
            self.message = DONE_MESSAGES.get(self.mode, "Done")
        elif state == "cancelled":
            self.message = "Cancelled"
        else:
            self.message = "Stopped: " + error
        if not self.dialog or self.dialog_closed:
            return
        if state == "cancelled" and not home_z:
            # Whoever pressed Cancel already knows
            self._prompt("prompt_end")
            return
        lines = [self.message]
        if home_z:
            lines.append("Screws were turned, so home Z (G28 Z) before printing.")
        lines += self._summary_lines()
        self._show(lines, "prompt_footer_button Close|%s" % (CLOSE_DIALOG_COMMAND,))

    def close_dialog(self):
        # Someone closed the dialog. It stays closed for the rest of the run,
        # and the status line carries on.
        if self.dialog and not self.dialog_closed:
            self.dialog_closed = True
            self._prompt("prompt_end")

    # Updates during a run

    def step(self, state, message, screw=None):
        if not self.running:
            return
        self.state = state
        self.message = message
        self.screw = screw
        display = self._display_status()
        if display is not None:
            display.message = "%s: %s" % (STATUS_LABELS.get(self.mode, "Bed screws"), message)
        self._redraw()

    def set_heaters(self, heaters):
        # "" when no heaters are on, "warming_up" or "at_temperature"
        self.heaters = heaters

    def set_heater_temps(self, temps):
        self.heater_temps = temps

    def set_pass(self, number, maximum):
        self.pass_number = number
        self.max_passes = maximum

    def set_turns(self, run_turns, count):
        self.run_turns = [round(run_turns.get(i, 0.), 4) for i in range(count)]

    def measured(self, heights, movements):
        # After measuring every screw. Heights are only compared when there's
        # one for every screw.
        if len(heights) != len(movements):
            self.screws = [self._screw(i, None, m, False) for i, m in enumerate(movements)]
            return
        reference = movements.index(0)
        self.screws = [self._screw(i, heights[i] - heights[reference], m, i == reference)
                       for i, m in enumerate(movements)]
        self.spread = round(max(heights) - min(heights), 4)
        if self.start_spread is None:
            self.start_spread = self.spread

    def screw_moved(self, screw, deviation, turns):
        # After measuring one screw on its own
        if screw < len(self.screws):
            self.screws[screw] = self._screw(
                screw, deviation, turns, self.screws[screw]["reference"])

    # Helpers

    def _screw(self, index, deviation, turns, reference):
        return {
            "name": self._name(index),
            "deviation": None if deviation is None else round(deviation, 4),
            "turns": round(turns, 4),
            "level": turns == 0,
            "reference": reference,
        }

    def _name(self, screw):
        return str(self.owner._screw_name(screw))

    def _display_status(self):
        # What M117 sets, shown by Mainsail, Fluidd, KlipperScreen and LCDs
        display = self.printer.lookup_object('display_status', None)
        if display is None or not hasattr(display, "message"):
            return None
        return display

    def _screw_line(self, screw):
        if screw["reference"]:
            text = "reference"
        elif screw["level"]:
            text = "level"
        else:
            text = self.owner._format_turns(screw["turns"])
        if screw["deviation"] is not None and not screw["reference"] and not screw["level"]:
            text = "%+.3fmm, %s" % (screw["deviation"], text)
        return "%s: %s" % (screw["name"], text)

    def _spread_line(self):
        if self.spread is None:
            return None
        if self.start_spread is not None and self.start_spread != self.spread:
            return "Spread %.3fmm, started at %.3fmm" % (self.spread, self.start_spread)
        return "Spread %.3fmm" % (self.spread,)

    def _summary_lines(self):
        lines = []
        if self.pass_number:
            lines.append("Passes: %d" % (self.pass_number,))
        lines += [self._screw_line(s) for s in self.screws]
        spread = self._spread_line()
        if spread:
            lines.append(spread)
        if any(self.run_turns):
            lines.append("Turned: " + ", ".join(
                "%s %.2f" % (self._name(i), t) for i, t in enumerate(self.run_turns) if t))
        return lines

    def _redraw(self):
        if not self.dialog or self.dialog_closed:
            return
        lines = []
        if self.state == "waiting_for_heaters":
            lines.append("Waiting for heaters" + (": " + self.heater_temps if self.heater_temps else ""))
        elif self.pass_number:
            heading = "Pass %d of up to %d" % (self.pass_number, self.max_passes)
            if self.heaters == "warming_up":
                heading += ", heaters warming up"
            elif self.heaters == "at_temperature":
                heading += ", at temperature"
            lines.append(heading)
        if self.state == "starting":
            lines.append(self.intro + " Cancel stops the motors straight away. A"
                         " probe move that's already started finishes first.")
        elif self.state != "waiting_for_heaters":
            lines.append(self.message)
        lines += [self._screw_line(s) for s in self.screws]
        spread = self._spread_line()
        if spread:
            lines.append(spread)
        self._show(lines, CANCEL_BUTTON)

    def _show(self, lines, button):
        self._prompt("prompt_begin " + self.title)
        for line in lines:
            self._prompt("prompt_text " + line)
        self._prompt(button)
        self._prompt("prompt_show")

    def _prompt(self, action):
        # Same output as RESPOND TYPE=command, which Mainsail, Fluidd and
        # KlipperScreen show as a dialog
        self.owner._prompt(action)


class MotorModel:
    """How long to run one motor for a given turn. Turning direction
    changes how fast the motor runs, since one way works against the bed
    springs, so each direction has its own speed and spin-up time. Backlash
    is the slack taken up when the motor changes direction, in turns, so it
    doesn't depend on how fast the motor runs."""

    def __init__(self, config, number, default_seconds_per_turn):
        self.number = number
        self.seconds_per_turn = {}
        self.spin_up_time = {}
        for direction, name in DIRECTION_NAMES.items():
            self.seconds_per_turn[direction] = config.getfloat(
                "motor%d_%s_seconds_per_turn" % (number, name),
                default_seconds_per_turn, above=0.)
            self.spin_up_time[direction] = config.getfloat(
                "motor%d_%s_spin_up_time" % (number, name), 0.)
        self.backlash = config.getfloat("motor%d_backlash" % (number,), 0., minval=0.)
        # Which way the motor last turned, if known, for backlash
        self.last_direction = None

    def seconds_for(self, turns):
        # Run time for a turn, and the backlash added to it in turns
        direction = CW if turns > 0 else CCW
        backlash = 0.
        if self.last_direction is not None and self.last_direction != direction:
            backlash = self.backlash
        seconds = self.spin_up_time[direction] + \
            (abs(turns) + backlash) * self.seconds_per_turn[direction]
        return max(seconds, 0.), backlash


class ScrewsTiltAdjustAuto:
    def __init__(self, config):
        self.active = False
        self.measurements = []
        self.serial_device = config.get("serial_device")
        self.serial_baud = config.get("serial_baud")
        self.full_turn_time_in_seconds = config.getfloat("full_turn_time_in_seconds", 3)
        self.maximum_attempts = config.getint("maximum_attempts", 10)
        self.scan_height = config.getfloat("scan_height", 2.)
        self.scan_sample_time = config.getfloat("scan_sample_time", 0.5)
        self.max_turns_per_move = config.getfloat("max_turns_per_move", 1., above=0.)
        self.max_turns_per_run = config.getfloat("max_turns_per_run", 3., above=0.)
        self.max_deviation = config.getfloat("max_deviation", 2., above=0.)
        self.reading_tolerance = config.getfloat("reading_tolerance", 0.02, minval=0.)
        self.learn_motor_speed = config.getboolean("learn_motor_speed", True)
        self.calibration_turns = config.getfloat("calibration_turns", 0.125, above=0.)
        self.calibration_cycles = config.getint("calibration_cycles", 2, minval=1)
        self.test_turns = config.getfloat("test_turns", 0.5, above=0.)
        # Debug output shows every step: each motor run, each reading, and
        # Klipper's own output for single screw measurements. DEBUG=1 on a
        # command turns it on for that run.
        self.debug_default = config.getboolean("debug", False)
        self.debug = self.debug_default
        # Klipper's own respond_raw while its output is held back
        self.held_respond_raw = None
        self.motors = [MotorModel(config, number, self.full_turn_time_in_seconds)
                       for number in range(1, MAX_MOTORS + 1)]
        # Details of the last turn, for learning from how far it went
        self.last_turn = None
        self.run_turns = None
        self.board = None
        self.one_by_one = False
        self.continuous = False
        self.heights = []
        # Set while a command that turns motors is running, and when a
        # cancel has been asked for
        self.running = False
        self.cancel_requested = False
        self.motors_moved = False

        self.printer = config.get_printer()
        self.progress = Progress(self)
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command("SCREWS_TILT_ADJUST_AUTO",
                                    self.cmd_SCREWS_TILT_ADJUST_AUTO,
                                    desc=self.cmd_SCREWS_TILT_ADJUST_AUTO_help)
        self.gcode.register_command("SCREWS_TILT_ADJUST_MANUAL",
                                    self.cmd_SCREWS_TILT_ADJUST_MANUAL,
                                    desc=self.cmd_SCREWS_TILT_ADJUST_MANUAL_help)
        self.gcode.register_command("SCREWS_TILT_ADJUST_AUTO_CALIBRATE",
                                    self.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE,
                                    desc=self.cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE_help)
        self.gcode.register_command("SCREWS_TILT_ADJUST_AUTO_CANCEL",
                                    self.cmd_SCREWS_TILT_ADJUST_AUTO_CANCEL,
                                    desc=self.cmd_SCREWS_TILT_ADJUST_AUTO_CANCEL_help)
        self.gcode.register_command("SCREWS_TILT_ADJUST_AUTO_TEST",
                                    self.cmd_SCREWS_TILT_ADJUST_AUTO_TEST,
                                    desc=self.cmd_SCREWS_TILT_ADJUST_AUTO_TEST_help)
        self.gcode.register_command(CLOSE_DIALOG_COMMAND, self.cmd_CLOSE_DIALOG)
        self.gcode.register_output_handler(self._handle_output)
        self._catch_cancel_commands()

    def get_status(self, eventtime=None):
        # printer.screws_tilt_adjust_auto, for Mainsail, Fluidd, KlipperScreen
        # and macros
        return self.progress.get_status()

    def cmd_CLOSE_DIALOG(self, gcmd):
        self._prompt("prompt_end")

    def _catch_cancel_commands(self):
        # A run holds Klipper's G-code lock until it finishes, so a cancel
        # sent from Mainsail, Fluidd, KlipperScreen or the console would
        # wait behind it and arrive too late. Each request from Moonraker is
        # handled on its own, so catch cancels here, before they wait for the
        # lock. Everything else goes through untouched.
        run_script = getattr(self.gcode, "run_script", None)
        if run_script is None:
            return
        def run_script_or_cancel(script):
            if self.running:
                if DIALOG_CLOSE.match(script):
                    # Closing the dialog would otherwise wait for the run,
                    # then close whatever dialog is open by then
                    self.progress.close_dialog()
                    return
                command = self._single_command(script)
                if command in CANCEL_COMMANDS:
                    self._request_cancel()
                    return
                if command in PRINT_CANCEL_COMMANDS:
                    self._request_cancel()
            return run_script(script)
        self.gcode.run_script = run_script_or_cancel

    def _single_command(self, script):
        # The command, in capitals, if the script is just one command with no
        # parameters, otherwise None
        words = script.split()
        return words[0].upper() if len(words) == 1 else None

    def _request_cancel(self):
        if not self.cancel_requested:
            self.cancel_requested = True
            self._info("Cancelling")

    def _check_cancelled(self):
        if self.cancel_requested:
            raise self.gcode.error("STAA: Cancelled")

    @contextlib.contextmanager
    def _run(self, gcmd=None, mode="level", title="Levelling bed screws",
             text="Turning the bed screws until the bed is level."):
        # Marks a command that turns motors as running, so it can be
        # cancelled, and keeps its progress up to date, in a dialog with a
        # Cancel button when asked to
        if self.running:
            raise self.gcode.error("STAA: Already running")
        dialog = gcmd is not None and bool(gcmd.get_int("DIALOG", 0, minval=0, maxval=1))
        self.running = True
        self.cancel_requested = False
        self.motors_moved = False
        self.progress.start(mode, title, text, dialog)
        try:
            yield
        except Exception as e:
            cancelled = self.cancel_requested
            home_z = cancelled and self.motors_moved
            if home_z:
                self._info("Screws were turned before cancelling, so home Z (G28 Z)"
                           " before printing")
            if cancelled:
                self.progress.finish("cancelled", home_z=home_z)
            else:
                self.progress.finish("failed", self._error_text(e))
            raise
        else:
            self.progress.finish("done")
        finally:
            self.running = False
            self.cancel_requested = False

    def _error_text(self, error):
        # Just the message, without "Error on '...': STAA: "
        text = re.sub(r"^Error on '.*?': ", "", str(error))
        return re.sub(r"^STAA: ", "", text)

    def _prompt(self, action):
        # Same output as RESPOND TYPE=command, which Mainsail, Fluidd and
        # KlipperScreen show as a dialog. It shows even while Klipper's own
        # output is held back.
        respond_raw = self.held_respond_raw or self.gcode.respond_raw
        respond_raw("// action:" + action)

    cmd_SCREWS_TILT_ADJUST_AUTO_CANCEL_help = "Cancel a running bed screw adjustment"
    def cmd_SCREWS_TILT_ADJUST_AUTO_CANCEL(self, gcmd):
        # Usually only reached when nothing is running, since a cancel sent
        # during a run is caught on its way in
        if self.running:
            self._request_cancel()
        else:
            self._info("Nothing to cancel")

    def _handle_output(self, output):
        if self.active:
            match = re.search(r": x=[-\d.]+, y=[-\d.]+, z=(-?[\d.]+)", output)
            if match is not None:
                self.heights.append(float(match.group(1)))
            match = re.search(r":[^:]+: adjust (C?CW) (\d+):(\d+)", output)
            if match is not None:
                self.measurements.append({
                    "direction": match.group(1),
                    "hours": match.group(2),
                    "minutes": match.group(3)
                })

    def _info(self, msg):
        held = self.held_respond_raw
        if held is None:
            self.gcode.respond_info("STAA: " + msg)
            return
        # Our own messages always show, even while Klipper's are held back,
        # such as a cancel arriving mid measurement
        holding = self.gcode.respond_raw
        self.gcode.respond_raw = held
        try:
            self.gcode.respond_info("STAA: " + msg)
        finally:
            self.gcode.respond_raw = holding

    def _debug(self, msg):
        if self.debug:
            self._info(msg)

    def _read_debug(self, gcmd):
        self.debug = bool(gcmd.get_int(
            "DEBUG", int(self.debug_default), minval=0, maxval=1))

    def _screw_name(self, screw):
        # The name given in [screws_tilt_adjust], as SCREWS_TILT_CALCULATE
        # shows it
        sta = self.printer.lookup_object('screws_tilt_adjust', None)
        screws = getattr(sta, "screws", None)
        if screws and screw < len(screws):
            return screws[screw][1]
        return "screw %d" % (screw + 1,)

    def _format_turns(self, turns):
        # Same as SCREWS_TILT_CALCULATE: CW 01:20 is one full turn and 20
        # minutes clockwise
        minutes = int(round(abs(turns) * 60))
        return "%s %02d:%02d" % ("CW" if turns >= 0 else "CCW", minutes // 60, minutes % 60)

    def _describe_movements(self, movements):
        return ", ".join("%s %s" % (self._screw_name(screw), self._format_turns(turns))
                         for screw, turns in enumerate(movements) if turns != 0)

    @contextlib.contextmanager
    def _quietly(self):
        # Hold back Klipper's own console output (probe results and
        # SCREWS_TILT_CALCULATE's table) unless debugging. It still goes to
        # klippy.log, and is shown after all if something goes wrong, so an
        # error comes with the readings that led to it.
        if self.debug or not hasattr(self.gcode, "respond_raw"):
            yield
            return
        had_own = "respond_raw" in vars(self.gcode)
        original = self.gcode.respond_raw
        held = []
        self.gcode.respond_raw = held.append
        self.held_respond_raw = original
        try:
            yield
        except BaseException:
            self.held_respond_raw = None
            self._restore_respond_raw(had_own, original)
            for msg in held:
                original(msg)
            raise
        self.held_respond_raw = None
        self._restore_respond_raw(had_own, original)

    def _restore_respond_raw(self, had_own, original):
        if had_own:
            self.gcode.respond_raw = original
        else:
            del self.gcode.respond_raw

    def _pause(self, seconds):
        # Let Klipper keep running (heater control in particular) while we
        # wait. A blocking sleep here stalls the host, and if heaters are on
        # for more than a few seconds Klipper shuts the printer down. Waits
        # are split up so a cancel is noticed quickly.
        end = self.reactor.monotonic() + seconds
        while True:
            self._check_cancelled()
            now = self.reactor.monotonic()
            if now >= end:
                return
            self.reactor.pause(min(end, now + CANCEL_CHECK_INTERVAL))

    def _connect_to_board(self):
        self.board = serial.Serial(
            port=self.serial_device,
            baudrate=self.serial_baud,
            timeout=0
        )
        # Wait for the board's handshake without blocking Klipper
        deadline = self.reactor.monotonic() + HANDSHAKE_TIMEOUT
        while not self.board.read(1):
            if self.reactor.monotonic() >= deadline:
                self.board.close()
                self.board = None
                raise self.gcode.error(
                    "STAA: No response from the board on %s after %d seconds" % (
                        self.serial_device, HANDSHAKE_TIMEOUT))
            self._pause(0.05)

    def _disconnect_from_board(self):
        if self.board is None:
            return
        try:
            # Never leave a motor running, whatever state we're in
            self.board.write("0".encode())
        finally:
            self.board.close()
            self.board = None

    cmd_SCREWS_TILT_ADJUST_MANUAL_help = "Manually adjust bed level screws using motors"
    def cmd_SCREWS_TILT_ADJUST_MANUAL(self, gcmd):
        screw = gcmd.get_int("SCREW", None)
        distance = gcmd.get_float("DISTANCE", None)
        if (screw is None or distance is None):
             raise gcmd.error(
                    "Error on '%s': SCREW and DISTANCE must be provided" % (
                        gcmd.get_commandline(),))
        if screw < 1:
            raise gcmd.error(
                "Error on '%s': SCREW must be 1 or more" % (gcmd.get_commandline(),))

        force = bool(gcmd.get_int("FORCE", 0, minval=0, maxval=1))
        self._read_debug(gcmd)

        directions = [0] * screw
        directions[screw - 1] = distance
        if not force:
            self._check_move_size(directions, "Add FORCE=1 to turn it anyway.")

        self._load_motor_state()
        with self._run(gcmd, "manual", "Turning a bed screw",
                       "Turning %s." % (self._screw_name(screw - 1),)):
            self._connect_to_board()
            try:
                self._info("Turning " + self._describe_movements(directions))
                self._debug("Motor movements: " + ", ".join(map(str, directions)))
                self._turn_motors(directions)
            finally:
                self._disconnect_from_board()
                self._save_motor_state()

    cmd_SCREWS_TILT_ADJUST_AUTO_help = "Tool to automatically adjust bed level screws"
    def cmd_SCREWS_TILT_ADJUST_AUTO(self, gcmd):
        self._read_debug(gcmd)
        self.continuous = bool(gcmd.get_int("CONTINUOUS", 0, minval=0, maxval=1))
        # Continuous mode settles one screw at a time too, it just watches
        # the screw with the probe instead of probing it again each time
        self.one_by_one = self.continuous or \
            bool(gcmd.get_int("ONE_BY_ONE", 0, minval=0, maxval=1))
        if self.continuous:
            # Fail before homing or measuring if the probe can't do this
            self._lookup_scanning_probe()
        # Turns made by each screw in this run, however many passes it takes
        self.run_turns = {}
        self._load_motor_state()
        try:
            with self._run(gcmd):
                self._adjust(gcmd)
        finally:
            self.run_turns = None
            self._save_motor_state()

    def _adjust(self, gcmd):
        if gcmd.get_int("WAIT_FOR_HEATERS", 0, minval=0, maxval=1):
            self._adjust_while_heating(gcmd)
        else:
            self._adjust_or_fail(gcmd)

    def _adjust_or_fail(self, gcmd):
        if not self._auto_adjust():
            raise gcmd.error(
                "Error on '%s': STAA: Unable to reach level after %s attempts. Please check manually and retry." % (
                    gcmd.get_commandline(), self.maximum_attempts,))

    def _auto_adjust(self, heaters=None):
        # Returns True once the bed is level, or False if it ran out of
        # attempts. When heaters are given, also records whether they were
        # at target just before the last measurement was taken.
        mode = ""
        if self.continuous:
            mode = " (continuous)"
        elif self.one_by_one:
            mode = " (one by one)"
        self._info("Levelling bed screws" + mode)
        self.heaters_at_target_before_last_measurement = False

        self._home_if_necessary()
        self._connect_to_board()
        adjustments_made = False
        level = False
        spread = None

        try:
            for i in range(self.maximum_attempts):
                self.progress.set_pass(i + 1, self.maximum_attempts)
                if heaters is not None:
                    self.heaters_at_target_before_last_measurement = \
                        self._heaters_at_target(heaters)
                self._measure_bed_tilt()
                spread = self._check_bed_got_closer(spread)
                movements = self._calculate_motor_movements()
                self.progress.measured(self.heights, movements)
                if all(m == 0 for m in movements):
                    level = True
                    break
                self._info("Adjusting " + self._describe_movements(movements))
                self._debug("Motor movements: " + ", ".join(map(str, movements)))
                adjustments_made = True
                if not self.one_by_one:
                    self._turn_motors(movements)
                elif not self._adjust_one_by_one(movements):
                    break
        finally:
            self._disconnect_from_board()

        if adjustments_made:
            self._info("Homing Z as bed level will have changed")
            self.progress.step("homing", "Homing Z")
            self.gcode.run_script_from_command("G28 Z")
        if level:
            self._info("Bed screws level")
        return level

    def _adjust_one_by_one(self, movements):
        # Settle each screw in turn, measuring only that screw between turns
        # and comparing it to the height the full measurement says it should
        # be at (that of a screw that doesn't need moving). The caller does a
        # full measurement again afterwards, since moving one screw can
        # slightly change the others. Returns False if a screw couldn't be
        # settled within maximum_attempts.
        if len(self.heights) != len(movements):
            raise self.gcode.error(
                "STAA: Expected %d screw heights but got %d" % (len(movements), len(self.heights)))
        # Refuse before turning any screw, not part way through
        self._check_move_size(
            movements, "Check the bed and probe, or raise max_turns_per_move.")
        # Measuring a single screw replaces self.heights, so keep the full set
        heights = list(self.heights)
        target = heights[movements.index(0)]
        self._learn_turns_per_mm()
        for screw, movement in enumerate(movements):
            if movement == 0:
                continue
            with self._screw_reader(screw) as read:
                height = heights[screw]
                stalled = 0
                for attempt in range(self.maximum_attempts):
                    self._debug("Screw %d movement: %s" % (screw + 1, movement))
                    self._turn_motors(self._single_motor(screw, movement, len(movements)))
                    new_height = read()
                    self._debug("%s : z=%.5f : adjust %s" % (
                        self._screw_name(screw), new_height,
                        self._format_turns((target - new_height) * self.turns_per_mm)))
                    stalled = self._check_screw_progress(
                        screw, movement, target, height, new_height, stalled)
                    self._learn_from_turn(screw, height, new_height)
                    height = new_height
                    movement = self._height_to_turns(target - height)
                    self.progress.screw_moved(screw, height - target, movement)
                    if movement == 0:
                        break
                else:
                    self._info("Unable to level %s after %s attempts" % (
                        self._screw_name(screw), self.maximum_attempts))
                    return False
            self._info("%s level after %d %s" % (
                self._screw_name(screw), attempt + 1, "try" if attempt == 0 else "tries"))
        return True

    @contextlib.contextmanager
    def _screw_reader(self, screw):
        # Yields a function that returns the screw's current height, on the
        # same scale as the full measurement
        if not self.continuous:
            yield lambda: self._measure_screw(screw)
            return
        with self._scan_screw(screw) as scan:
            # The probe's own reading can be offset from what the full
            # measurement found, so only trust how it changes. The screw
            # hasn't moved since the full measurement, so this first
            # reading is the height that measurement found.
            offset = self.heights[screw] - scan()
            yield lambda: scan() + offset

    @contextlib.contextmanager
    def _scan_screw(self, screw):
        # Park the probe over the screw and keep it there, reading the bed
        # with Klipper's own METHOD=scan probing (which samples where the
        # toolhead is rather than moving down to the bed) while the motor
        # turns. Yields a function that takes a fresh reading.
        probe = self._lookup_scanning_probe()
        sta = self._lookup_screws_tilt_adjust()
        coord = sta.screws[screw][0]
        self._debug("Watching screw %d with the probe" % (screw + 1,))
        self.progress.step("measuring", "Watching %s with the probe" % (
            self._screw_name(screw),), screw)
        self._move_to_scan(probe, sta, coord)
        gcmd = self.gcode.create_gcode_command("PROBE", "PROBE", {
            "METHOD": "scan", "SAMPLE_TIME": str(self.scan_sample_time)})
        session = probe.start_probe_session(gcmd)
        def scan():
            self._check_cancelled()
            session.run_probe(gcmd)
            return self._probe_result_z(session.pull_probed_results()[-1])
        try:
            yield scan
        finally:
            session.end_probe_session()
            self._lift_after_scan(probe, sta)

    def _move_to_scan(self, probe, sta, coord):
        # Same moves SCREWS_TILT_CALCULATE makes: up to its travel height,
        # across to the screw, then down to where the probe can read the bed
        toolhead = self.printer.lookup_object('toolhead')
        lift_speed = probe.get_probe_params()['lift_speed']
        toolhead.manual_move([None, None, self._travel_height(sta)], lift_speed)
        toolhead.manual_move([coord[0], coord[1], None], getattr(sta.probe_helper, "speed", 50.))
        toolhead.manual_move([None, None, self.scan_height], lift_speed)
        toolhead.wait_moves()

    def _lift_after_scan(self, probe, sta):
        toolhead = self.printer.lookup_object('toolhead')
        lift_speed = probe.get_probe_params()['lift_speed']
        toolhead.manual_move([None, None, self._travel_height(sta)], lift_speed)

    def _travel_height(self, sta):
        helper = sta.probe_helper
        z = getattr(helper, "horizontal_move_z",
                    getattr(helper, "default_horizontal_move_z", 5.))
        return max(z, self.scan_height)

    def _probe_result_z(self, result):
        # Newer Klipper returns a ProbeResult, older versions a plain list
        z = getattr(result, "bed_z", None)
        return result[2] if z is None else z

    def _lookup_scanning_probe(self):
        # METHOD=scan is only honoured by Klipper's eddy current probes. Any
        # other probe would treat it as a normal probe and move down to the
        # bed, so don't let continuous mode use one.
        probe = self.printer.lookup_object('probe', None)
        eddy_probes = [obj for name, obj in self.printer.lookup_objects('probe_eddy_current')]
        if probe is None or not any(probe is obj for obj in eddy_probes):
            raise self.gcode.error(
                "STAA: Continuous mode needs a [probe_eddy_current] probe. Use ONE_BY_ONE=1 for other probes.")
        return probe

    def _single_motor(self, screw, movement, count):
        movements = [0] * count
        movements[screw] = movement
        return movements

    def _adjust_while_heating(self, gcmd):
        heaters = self._lookup_heaters(gcmd)

        if not self._heating_heaters(heaters):
            self.progress.set_heaters("")
            self._info("No heaters have a target temperature, adjusting once")
            self._adjust_or_fail(gcmd)
            return

        if self._heaters_at_target(heaters):
            self.progress.set_heaters("at_temperature")
            self._info("Heaters already at target, adjusting once")
            self._adjust_or_fail(gcmd)
            return

        self._info("Adjusting while heaters reach target")
        self.progress.set_heaters("warming_up")
        level = self._auto_adjust(heaters)

        if level and self.heaters_at_target_before_last_measurement:
            # The measurement that found the bed level was taken at
            # temperature, so there's no heat flex left to adjust for
            self._info("Heaters were at target for the final measurement, finished")
            return
        if not level:
            # The bed is still moving as it heats, so this can happen. The
            # pass at temperature below is the one that has to succeed.
            self._info("Could not reach level while heating, will adjust again at target")

        self._wait_for_heaters(gcmd, heaters)
        self.progress.set_heaters("at_temperature")
        self._info("Heaters at target, adjusting for heat flex")
        self._adjust_or_fail(gcmd)

    def _lookup_heaters(self, gcmd):
        pheaters = self.printer.lookup_object('heaters')
        names = gcmd.get("HEATERS", None)
        if names is None:
            names = pheaters.get_all_heaters()
        else:
            names = [n.strip() for n in names.split(",") if n.strip()]
        heaters = []
        for name in names:
            try:
                heaters.append((name, pheaters.lookup_heater(name)))
            except self.printer.config_error:
                raise gcmd.error("Error on '%s': STAA: Unknown heater '%s'" % (
                    gcmd.get_commandline(), name))
        return heaters

    def _heating_heaters(self, heaters):
        # Heaters with a target set; ones that are off aren't waited on
        eventtime = self.reactor.monotonic()
        return [(name, heater) for name, heater in heaters
                if heater.get_temp(eventtime)[1] > 0.]

    def _heaters_at_target(self, heaters):
        # Same test Klipper uses for M109/M190/TEMPERATURE_WAIT-style waits
        eventtime = self.reactor.monotonic()
        return not any(heater.check_busy(eventtime)
                       for name, heater in self._heating_heaters(heaters))

    def _wait_for_heaters(self, gcmd, heaters):
        self._info("Waiting for heaters to reach target")
        next_report = self.reactor.monotonic()
        while not self._heaters_at_target(heaters):
            if self.printer.is_shutdown():
                raise gcmd.error("Error on '%s': STAA: Printer shut down while waiting for heaters" % (
                    gcmd.get_commandline(),))
            eventtime = self.reactor.monotonic()
            if eventtime >= next_report:
                temps = []
                for name, heater in self._heating_heaters(heaters):
                    current, target = heater.get_temp(eventtime)
                    temps.append("%s %.1f/%.1f" % (name, current, target))
                self._info("Waiting for " + ", ".join(temps))
                self.progress.set_heater_temps(", ".join(temps))
                self.progress.step("waiting_for_heaters", "Waiting for heaters")
                next_report = eventtime + HEATER_REPORT_INTERVAL
            self._pause(1.)

    def _home_if_necessary(self):
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.printer.get_reactor().monotonic()
        kin_status = toolhead.get_kinematics().get_status(curtime)
        if kin_status['homed_axes'] != 'xyz':
            self._debug("Homing toolhead")
            self.progress.step("homing", "Homing")
            self.gcode.run_script_from_command("G28")

    def _measure_bed_tilt(self):
        self._debug("Measuring bed tilt")
        self.progress.step("measuring", "Measuring all screws")
        self._run_screws_tilt_calculate()

    def _run_screws_tilt_calculate(self):
        self._check_cancelled()
        self.active = True
        self.measurements = []
        self.heights = []
        try:
            self.gcode.run_script_from_command(
                "SCREWS_TILT_CALCULATE MAX_DEVIATION=%s" % (self.max_deviation,))
        finally:
            self.active = False

    def _measure_screw(self, screw):
        # Klipper's screws_tilt_adjust probes every screw it's configured
        # with, so narrow it to just this screw while we measure. Using the
        # same probing as the full measurement keeps the heights comparable.
        sta = self._lookup_screws_tilt_adjust()
        screws = sta.screws
        probe_points = sta.probe_helper.probe_points
        results = getattr(sta, "results", None)
        self._debug("Measuring screw %d" % (screw + 1,))
        self.progress.step("measuring", "Measuring " + self._screw_name(screw), screw)
        try:
            sta.screws = [screws[screw]]
            sta.probe_helper.probe_points = [coord for coord, name in sta.screws]
            # A one screw table on every turn isn't much use to anyone
            with self._quietly():
                self._run_screws_tilt_calculate()
        finally:
            sta.screws = screws
            sta.probe_helper.probe_points = probe_points
            if results is not None:
                sta.results = results
        if len(self.heights) != 1:
            raise self.gcode.error(
                "STAA: Expected 1 screw height but got %d" % (len(self.heights),))
        return self.heights[0]

    def _learn_turns_per_mm(self):
        # Work out how many turns move the bed 1mm (and which way) from the
        # last full measurement, where Klipper reports both the height and
        # the turns for each screw against the base (screw 1). A best fit
        # across all screws smooths out Klipper's rounding to the minute.
        base = self.heights[0]
        pairs = [(base - z, self._measurement_to_turns(m))
                 for z, m in zip(self.heights[1:], self.measurements)]
        total = sum(diff * diff for diff, turns in pairs)
        if total == 0:
            raise self.gcode.error("STAA: Couldn't work out turns per mm from the measurement")
        learned = sum(diff * turns for diff, turns in pairs) / total
        if not MIN_TURNS_PER_MM <= abs(learned) <= MAX_TURNS_PER_MM:
            raise self.gcode.error(
                "STAA: The measurement works out to %.2f turns per mm, which no supported"
                " screw thread gives. Check the probe and retry." % (learned,))
        expected = self._screw_thread_turns_per_mm()
        if expected is None:
            self.turns_per_mm = learned
            return
        spread = max(abs(diff) for diff, turns in pairs)
        if spread >= MIN_SPREAD_TO_CHECK_THREAD and (
                learned * expected < 0 or not 0.5 <= learned / expected <= 2.):
            raise self.gcode.error(
                "STAA: The measurement works out to %.2f turns per mm, but screw_thread"
                " says %.2f. Check screw_thread in [screws_tilt_adjust] and the probe." % (
                    learned, expected))
        # Exact, unlike the learned rate, which suffers from Klipper's rounding
        self.turns_per_mm = expected

    def _screw_thread_turns_per_mm(self):
        # From the screw_thread set in [screws_tilt_adjust], if this Klipper
        # version exposes it
        sta = self.printer.lookup_object('screws_tilt_adjust', None)
        thread = SCREW_THREADS.get(getattr(sta, "thread", None))
        if thread is None:
            return None
        pitch, clockwise_raises = thread
        return 1. / pitch if clockwise_raises else -1. / pitch

    def _check_bed_got_closer(self, previous_spread):
        # After screws have been turned, the gap between the highest and
        # lowest screw should never grow. If it does, a motor is turning the
        # wrong way or the readings are bad, and carrying on would make it
        # worse. Returns the spread for the next check.
        if not self.heights:
            return previous_spread
        spread = max(self.heights) - min(self.heights)
        if previous_spread is not None and spread > previous_spread + self.reading_tolerance:
            raise self.gcode.error(
                "STAA: The bed got further from level after turning the screws"
                " (%.3fmm to %.3fmm between highest and lowest). Check each motor"
                " turns its screw the right way, then retry." % (previous_spread, spread))
        return spread

    def _check_screw_progress(self, screw, movement, target, before, after, stalled):
        # After a turn the screw should be closer to level, and should have
        # moved roughly as far as asked. Returns how many turns in a row the
        # screw has barely moved.
        if abs(target - after) > abs(target - before) + self.reading_tolerance:
            raise self.gcode.error(
                "STAA: Screw %d got further from level after turning (%.3fmm to %.3fmm)."
                " Check its motor turns the right way, then retry, or run"
                " SCREWS_TILT_ADJUST_AUTO_CALIBRATE." % (screw + 1, abs(target - before), abs(target - after)))
        expected = abs(movement / self.turns_per_mm)
        if expected < 2 * self.reading_tolerance:
            # Too small a move to tell from the readings' noise
            return stalled
        if abs(after - before) >= 0.2 * expected:
            return 0
        stalled += 1
        if stalled >= 2:
            raise self.gcode.error(
                "STAA: Screw %d barely moved after %d turns in a row. Check its motor"
                " and coupling, then retry." % (screw + 1, stalled))
        return stalled

    def _height_to_turns(self, diff):
        # How far to turn a screw to raise its corner of the bed by diff mm.
        # Anything under half a minute is level, as SCREWS_TILT_CALCULATE
        # would report it as 00:00.
        turns = diff * self.turns_per_mm
        if abs(turns) * 60 < 0.5:
            return 0
        return turns

    def _lookup_screws_tilt_adjust(self):
        sta = self.printer.lookup_object('screws_tilt_adjust', None)
        if (sta is None or not hasattr(sta, "screws")
                or not hasattr(getattr(sta, "probe_helper", None), "probe_points")):
            raise self.gcode.error(
                "STAA: One by one mode needs [screws_tilt_adjust] from a supported Klipper version")
        return sta

    def _measurement_to_turns(self, m):
        direction = 1 if m["direction"] == "CW" else -1
        turns = float(m["hours"]) + (float(m["minutes"]) / 60)
        return turns * direction

    def _calculate_motor_movements(self):
        if not self.measurements:
            # Without this the bed would look level and nothing would move
            raise self.gcode.error(
                "STAA: Couldn't read any screw adjustments from SCREWS_TILT_CALCULATE")
        movements = [self._measurement_to_turns(m) for m in self.measurements]

        base_offset = 0
        if all(m > 0 for m in movements):
            base_offset = min(movements)
        elif all(m < 0 for m in movements):
            base_offset = max(movements)
        base_offset *= -1
        movements = [m + base_offset for m in movements]
        movements.insert(0, base_offset)

        return movements

    def _check_move_size(self, movements, hint):
        for screw, distance in enumerate(movements):
            if abs(distance) > self.max_turns_per_move:
                raise self.gcode.error(
                    "STAA: Screw %d would turn %.2f turns, more than max_turns_per_move"
                    " (%s). %s" % (screw + 1, abs(distance), self.max_turns_per_move, hint))

    def _check_run_total(self, movements):
        for screw, distance in enumerate(movements):
            total = self.run_turns.get(screw, 0.) + abs(distance)
            if total > self.max_turns_per_run:
                raise self.gcode.error(
                    "STAA: Screw %d would turn %.2f turns in total this run, more than"
                    " max_turns_per_run (%s). Check the bed and probe, then retry." % (
                        screw + 1, total, self.max_turns_per_run))

    def _turn_motors(self, movements):
        if self.run_turns is not None:
            # Check every screw before moving any of them
            self._check_move_size(
                movements, "Check the bed and probe, or raise max_turns_per_move.")
            self._check_run_total(movements)
        self.last_turn = None
        for screw, distance in enumerate(movements):
            if distance == 0:
                continue
            if self.run_turns is not None:
                self.run_turns[screw] = self.run_turns.get(screw, 0.) + abs(distance)
            motor = self._motor(screw)
            seconds, backlash = motor.seconds_for(distance)
            direction = CW if distance > 0 else CCW
            self.progress.step("turning", "Turning %s %s" % (
                self._screw_name(screw), self._format_turns(distance)), screw)
            self._run_motor(screw, direction, seconds)
            self.last_turn = (screw, direction, abs(distance), seconds, backlash)
            if self.run_turns is not None:
                self.progress.set_turns(self.run_turns, len(movements))

        self._stop_motors()

    def _motor(self, screw):
        if screw < len(self.motors):
            return self.motors[screw]
        # More screws than the board has motors: the board ignores these,
        # so the default timing is as good as any
        return MotorModel(_NoConfig(), screw + 1, self.full_turn_time_in_seconds)

    def _run_motor(self, screw, direction, seconds):
        # Run one motor for the given time. The next command to the board
        # stops it, so callers stop the motors when they're done.
        self._check_cancelled()
        motor = (screw + 1) * direction
        self._debug("%d for %.3f seconds." % (motor, seconds))
        command = str(motor).encode()
        self.board.write(command)
        self.motors_moved = True
        # Set now, since even a turn cut short by a cancel takes up the slack
        self._motor(screw).last_direction = direction
        wait_time = seconds
        while wait_time > MOTOR_KEEPALIVE_INTERVAL:
            self._pause(MOTOR_KEEPALIVE_INTERVAL)
            wait_time -= MOTOR_KEEPALIVE_INTERVAL
            # Tells the board to keep this motor running
            self.board.write(command)
        self._pause(wait_time)

    def _stop_motors(self):
        self._debug("Stopping motors.")
        self.board.write("0".encode())

    def _learn_from_turn(self, screw, before, after):
        # Compare how far the screw actually went with how far it was asked
        # to, and move that direction's speed part of the way toward what
        # this turn suggests. Turns too small to tell from the probe's noise,
        # and ones that suggest an unlikely speed, are skipped.
        if not self.learn_motor_speed or self.last_turn is None:
            return
        turned_screw, direction, asked, seconds, backlash = self.last_turn
        if turned_screw != screw:
            return
        if abs(asked / self.turns_per_mm) < 2 * self.reading_tolerance:
            return
        moved = (after - before) * self.turns_per_mm * direction
        if moved <= 0:
            return
        motor = self._motor(screw)
        running = seconds - motor.spin_up_time[direction]
        if running <= 0:
            return
        current = motor.seconds_per_turn[direction]
        estimate = running / (moved + backlash)
        if not 1. / MAX_LEARNING_RATIO <= estimate / current <= MAX_LEARNING_RATIO:
            return
        motor.seconds_per_turn[direction] = current + LEARNING_RATE * (estimate - current)
        self._debug("Motor %d %s: %.3fs per turn, now %.3fs" % (
            motor.number, DIRECTION_NAMES[direction], current,
            motor.seconds_per_turn[direction]))

    def _variables(self):
        # Values kept with Klipper's [save_variables], or None without it
        save_variables = self.printer.lookup_object('save_variables', None)
        if save_variables is None:
            return None
        return save_variables.get_status(self.reactor.monotonic()).get("variables", {})

    def _variable_names(self, motor):
        prefix = "%s%d_" % (VARIABLE_PREFIX, motor.number)
        speeds = dict((direction, prefix + name + "_seconds_per_turn")
                      for direction, name in DIRECTION_NAMES.items())
        return speeds, prefix + "last_direction"

    def _load_motor_state(self):
        # Learned speeds and each motor's last direction, from earlier runs.
        # Learned speeds win over the config, since they started from it.
        variables = self._variables()
        if variables is None:
            return
        for motor in self.motors:
            speeds, last_direction = self._variable_names(motor)
            if last_direction in variables:
                motor.last_direction = variables[last_direction] or None
            if not self.learn_motor_speed:
                continue
            for direction, name in speeds.items():
                value = variables.get(name)
                if isinstance(value, (int, float)) and value > 0:
                    motor.seconds_per_turn[direction] = float(value)

    def _save_motor_state(self):
        # Keep learned speeds and last directions for the next run. Klipper's
        # SAVE_CONFIG restarts Klipper, so these go in [save_variables].
        variables = self._variables()
        if variables is None:
            return
        try:
            for motor in self.motors:
                speeds, last_direction = self._variable_names(motor)
                values = {last_direction: motor.last_direction or 0}
                if self.learn_motor_speed:
                    for direction, name in speeds.items():
                        values[name] = round(motor.seconds_per_turn[direction], 4)
                for name, value in sorted(values.items()):
                    if variables.get(name) != value:
                        self.gcode.run_script_from_command(
                            "SAVE_VARIABLE VARIABLE=%s VALUE=%r" % (name, value))
        except self.gcode.error as e:
            # Don't hide whatever the run itself reported
            self._info("Couldn't save motor speeds: %s" % (e,))

    cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE_help = \
        "Measure how fast each screw motor turns each way, and its backlash"
    def cmd_SCREWS_TILT_ADJUST_AUTO_CALIBRATE(self, gcmd):
        self.continuous = bool(gcmd.get_int("CONTINUOUS", 0, minval=0, maxval=1))
        self.one_by_one = True
        self._read_debug(gcmd)
        level_first = bool(gcmd.get_int("LEVEL", 1, minval=0, maxval=1))
        test_first = bool(gcmd.get_int("TEST", 1, minval=0, maxval=1))
        cycles = gcmd.get_int("CYCLES", self.calibration_cycles, minval=1)
        turns = gcmd.get_float("TURNS", self.calibration_turns, above=0.)
        screw = gcmd.get_int("SCREW", None, minval=1)
        if self.continuous:
            self._lookup_scanning_probe()
        # Every move is checked on its own, but the back and forth adds up
        # past max_turns_per_run without going anywhere, so only the
        # levelling counts toward it
        self._check_move_size([turns * 2], "Use a smaller TURNS.")
        self._load_motor_state()
        try:
            with self._run(gcmd, "calibrate", "Calibrating screw motors",
                           "Measuring how fast each screw motor turns."):
                if test_first:
                    # Find wiring and coupling faults with small turns, before
                    # levelling makes big ones
                    self._self_test(self.test_turns)
                if level_first:
                    # Calibrate where the screws normally sit, since the motors
                    # run slower the tighter a screw is. Motors that are far off
                    # might not quite get there before they're calibrated, which
                    # is close enough.
                    self.run_turns = {}
                    if not self._auto_adjust():
                        self._info("Couldn't fully level first, calibrating from here")
                    self.run_turns = None
                results = self._calibrate(gcmd, screw, turns, cycles)
        finally:
            self.run_turns = None
            self._save_motor_state()
        self._store_calibration(results)

    def _calibrate(self, gcmd, screw, turns, cycles):
        self._info("Calibrating motors")
        self._home_if_necessary()
        self._connect_to_board()
        try:
            self._measure_bed_tilt()
            count = len(self.heights)
            if screw is not None and screw > count:
                raise gcmd.error("Error on '%s': STAA: There are only %d screws" % (
                    gcmd.get_commandline(), count))
            self.turns_per_mm = self._calibration_turns_per_mm()
            screws = range(count) if screw is None else [screw - 1]
            results = []
            for index in screws:
                with self._screw_reader(index) as read:
                    results.append(self._calibrate_screw(index, read, turns, cycles))
        finally:
            self._disconnect_from_board()
        self._info("Homing Z as bed level will have changed")
        self.gcode.run_script_from_command("G28 Z")
        return results

    def _calibration_turns_per_mm(self):
        expected = self._screw_thread_turns_per_mm()
        if expected is not None:
            return expected
        try:
            self._learn_turns_per_mm()
        except self.gcode.error:
            raise self.gcode.error(
                "STAA: Can't tell how far a turn moves the bed. This Klipper version"
                " doesn't say which screw_thread is set, so calibrate with the bed"
                " slightly out of level and LEVEL=0.")
        return self.turns_per_mm

    def _calibrate_screw(self, screw, read, turns, cycles):
        # Rock the screw back and forth around where it sits, measuring after
        # every move, then put it back where it started. Returns the motor's
        # new values.
        motor = self._motor(screw)
        self._info("Calibrating %s" % (self._screw_name(screw),))
        self.progress.step("calibrating", "Calibrating " + self._screw_name(screw), screw)
        # Seconds for the shortest move each way, from what's known so far
        base = dict((d, motor.spin_up_time[d] + turns * motor.seconds_per_turn[d])
                    for d in DIRECTION_NAMES)
        steady = {CW: [], CCW: []}
        reversals = []
        previous = motor.last_direction
        start = height = read()
        for cycle in range(cycles):
            for direction, multiple in CALIBRATION_PATTERN:
                seconds = max(base[direction], 0.01) * multiple
                self._run_motor(screw, direction, seconds)
                self._stop_motors()
                new_height = read()
                moved = (new_height - height) * self.turns_per_mm * direction
                self._debug("Screw %d %s for %.3fs moved %.4f turns" % (
                    screw + 1, DIRECTION_NAMES[direction], seconds, moved))
                height = new_height
                if previous == direction:
                    steady[direction].append((seconds, moved))
                elif previous is not None:
                    if moved < self.reading_tolerance * abs(self.turns_per_mm):
                        # The slack may not all be taken up, so the next
                        # moves would be off and the backlash can't be told
                        raise self.gcode.error(
                            "STAA: Screw %d barely moved after changing direction, so its"
                            " backlash is more than the calibration moves. Use a bigger"
                            " TURNS." % (screw + 1,))
                    reversals.append((direction, seconds, moved))
                # The very first move takes up any slack, and how much there
                # was isn't known, so it's not used
                previous = direction
            # Use what this cycle found so both ways move the same distance
            # next time, keeping the screw around the same spot
            for d in DIRECTION_NAMES:
                rate, offset = self._fit_speed(screw, d, steady[d])
                base[d] = (turns - offset) / rate
        fits = dict((d, self._fit_speed(screw, d, steady[d])) for d in DIRECTION_NAMES)
        backlash = [fits[d][0] * seconds + fits[d][1] - moved
                    for d, seconds, moved in reversals]
        backlash = max(0., sum(backlash) / len(backlash))
        if backlash > self.max_turns_per_move:
            raise self.gcode.error(
                "STAA: Screw %d seems to have %.2f turns of backlash, which is more"
                " likely bad readings. Check the probe and motor coupling, then retry." % (
                    screw + 1, backlash))
        result = {"backlash": backlash}
        for d, (rate, offset) in fits.items():
            result[d] = (1. / rate, -offset / rate)
        self._return_to(screw, (start - height) * self.turns_per_mm, previous, result)
        return screw, result

    def _return_to(self, screw, distance, previous, result):
        # Turn the screw back to where calibration started, using the
        # values it just found
        if abs(distance) * 60 < 0.5:
            return
        self._check_move_size([distance], "Check the probe and motor coupling.")
        direction = CW if distance > 0 else CCW
        seconds_per_turn, spin_up_time = result[direction]
        backlash = result["backlash"] if previous != direction else 0.
        seconds = spin_up_time + (abs(distance) + backlash) * seconds_per_turn
        if seconds > 0:
            self._run_motor(screw, direction, seconds)
            self._stop_motors()

    def _fit_speed(self, screw, direction, samples):
        # Straight line fit of turns moved against seconds run, slack already
        # taken up. Returns (turns per second, turns at zero seconds); the
        # second is negative when the motor takes a moment to get going.
        n = float(len(samples))
        mean_t = sum(t for t, m in samples) / n
        mean_m = sum(m for t, m in samples) / n
        var = sum((t - mean_t) ** 2 for t, m in samples)
        if var == 0:
            raise self.gcode.error("STAA: Not enough calibration moves to fit")
        rate = sum((t - mean_t) * (m - mean_m) for t, m in samples) / var
        if rate <= 0:
            raise self.gcode.error(
                "STAA: Screw %d didn't move when its motor ran %s. Check the motor"
                " and its coupling, then retry." % (
                    screw + 1, "clockwise" if direction == CW else "counter-clockwise"))
        return rate, mean_m - rate * mean_t

    def _store_calibration(self, results):
        # Use the new values now, stage them for SAVE_CONFIG like Klipper's
        # own calibration commands do, and start learning over from them
        configfile = self.printer.lookup_object('configfile')
        for screw, result in results:
            motor = self._motor(screw)
            for direction, name in DIRECTION_NAMES.items():
                seconds_per_turn, spin_up_time = result[direction]
                motor.seconds_per_turn[direction] = seconds_per_turn
                motor.spin_up_time[direction] = spin_up_time
                configfile.set("screws_tilt_adjust_auto",
                               "motor%d_%s_seconds_per_turn" % (motor.number, name),
                               "%.4f" % (seconds_per_turn,))
                configfile.set("screws_tilt_adjust_auto",
                               "motor%d_%s_spin_up_time" % (motor.number, name),
                               "%.4f" % (spin_up_time,))
            motor.backlash = result["backlash"]
            configfile.set("screws_tilt_adjust_auto", "motor%d_backlash" % (motor.number,),
                           "%.4f" % (result["backlash"],))
            self._info(
                "Motor %d: clockwise %.3fs per turn (%.3fs spin-up),"
                " counter-clockwise %.3fs per turn (%.3fs spin-up), backlash %.3f turns" % (
                    motor.number, result[CW][0], result[CW][1],
                    result[CCW][0], result[CCW][1], result["backlash"]))
        self._save_motor_state()
        self._info("Calibration done. Run SAVE_CONFIG to keep these values.")

    cmd_SCREWS_TILT_ADJUST_AUTO_TEST_help = \
        "Turn each screw motor a little and back to check it moves the right screw the right way"
    def cmd_SCREWS_TILT_ADJUST_AUTO_TEST(self, gcmd):
        self._read_debug(gcmd)
        self.continuous = bool(gcmd.get_int("CONTINUOUS", 0, minval=0, maxval=1))
        turns = gcmd.get_float("TURNS", self.test_turns, above=0.)
        if self.continuous:
            self._lookup_scanning_probe()
        self._load_motor_state()
        try:
            with self._run(gcmd, "test", "Testing screw motors",
                           "Turning each screw motor a little and back."):
                self._self_test(turns)
        finally:
            self._save_motor_state()

    def _self_test(self, turns):
        # Turn each motor clockwise by `turns` and back again, checking that
        # its own screw moved, the right way and about the right distance.
        # Stops at the first fault, after putting that screw back.
        self._check_move_size([turns], "Use a smaller TURNS.")
        tpm = self._screw_thread_turns_per_mm()
        if tpm is None:
            raise self.gcode.error(
                "STAA: The motor test needs screw_thread set in [screws_tilt_adjust]")
        self.turns_per_mm = tpm
        expected = turns / tpm
        if abs(expected) < 2 * TEST_NOISE_MULTIPLE * self.reading_tolerance:
            raise self.gcode.error(
                "STAA: A %.3f turn test only moves the bed %.3fmm, too little to tell"
                " from the probe's noise. Use a bigger TURNS." % (turns, abs(expected)))
        self._info("Testing motors" + (" (continuous)" if self.continuous else ""))
        self._home_if_necessary()
        self._connect_to_board()
        try:
            if self.continuous:
                count = len(self._lookup_screws_tilt_adjust().screws)
                self._check_motor_count(count)
                for screw in range(count):
                    self._test_screw_continuous(screw, count, turns, expected)
            else:
                self._measure_bed_tilt()
                count = len(self.heights)
                self._check_motor_count(count)
                for screw in range(count):
                    self._test_screw_full(screw, count, turns, expected)
        finally:
            self._disconnect_from_board()
        self._info("Homing Z as bed level will have changed")
        self.gcode.run_script_from_command("G28 Z")
        self._info("All %d motors passed the test" % (count,))

    def _check_motor_count(self, count):
        if count > MAX_MOTORS:
            raise self.gcode.error(
                "STAA: [screws_tilt_adjust] has %d screws, but the board only drives %d"
                " motors" % (count, MAX_MOTORS))

    def _test_screw_full(self, screw, count, turns, expected):
        # Measure the whole bed after the turn, so a motor plugged into the
        # wrong port shows up as the wrong screw moving
        before = list(self.heights)
        self._debug("Testing motor %d" % (screw + 1,))
        self._turn_motors(self._single_motor(screw, turns, count))
        # Klipper's table after every test turn is noise unless debugging
        with self._quietly():
            self._measure_bed_tilt()
        moved = [a - b for a, b in zip(self.heights, before)]
        fault = None
        biggest = max(range(count), key=lambda i: abs(moved[i]))
        if self._test_moved(moved[biggest]) and biggest != screw:
            fault = ("STAA: Motor %d moved screw %d instead of screw %d. Check which port"
                     " each motor is plugged into." % (screw + 1, biggest + 1, screw + 1))
        else:
            fault = self._test_fault(screw, moved[screw], expected)
        # Put the screw back whatever happened
        after = list(self.heights)
        self._turn_motors(self._single_motor(screw, -turns, count))
        with self._quietly():
            self._measure_bed_tilt()
        if fault is not None:
            raise self.gcode.error(fault)
        self._check_returned(screw, after[screw] - self.heights[screw], expected)

    def _test_screw_continuous(self, screw, count, turns, expected):
        # Watch the screw with the probe while its motor turns. If it doesn't
        # move, try the other motors to find which one does move it.
        with self._scan_screw(screw) as read:
            self._debug("Testing motor %d" % (screw + 1,))
            before = read()
            self._turn_motors(self._single_motor(screw, turns, count))
            after = read()
            fault = self._test_fault(screw, after - before, expected)
            self._turn_motors(self._single_motor(screw, -turns, count))
            back = read()
            if fault is None:
                self._check_returned(screw, after - back, expected)
                return
            if not self._test_moved(after - before):
                for other in range(count):
                    if other == screw:
                        continue
                    before = read()
                    self._turn_motors(self._single_motor(other, turns, count))
                    after = read()
                    self._turn_motors(self._single_motor(other, -turns, count))
                    if self._test_moved(after - before):
                        fault = ("STAA: Screw %d is turned by motor %d, not motor %d. Check"
                                 " which port each motor is plugged into." % (
                                     screw + 1, other + 1, screw + 1))
                        break
            raise self.gcode.error(fault)

    def _test_fault(self, screw, moved, expected):
        # What's wrong with a test turn of a screw's own motor, or None
        self._debug("Screw %d moved %.3fmm (expected %.3fmm)" % (
            screw + 1, moved, expected))
        if not self._test_moved(moved):
            return ("STAA: Screw %d barely moved when motor %d turned (%.3fmm, expected"
                    " %.3fmm). Check the motor is plugged in, and its coupling." % (
                        screw + 1, screw + 1, abs(moved), abs(expected)))
        if moved * expected < 0:
            return ("STAA: Motor %d turns screw %d the wrong way. Swap the motor's two"
                    " wires." % (screw + 1, screw + 1))
        if abs(moved) > TEST_MAX_FRACTION * abs(expected):
            return ("STAA: Screw %d moved %.3fmm, far more than the %.3fmm expected."
                    " Check full_turn_time_in_seconds, or run"
                    " SCREWS_TILT_ADJUST_AUTO_CALIBRATE TEST=0." % (
                        screw + 1, abs(moved), abs(expected)))
        return None

    def _test_moved(self, moved):
        return abs(moved) > TEST_NOISE_MULTIPLE * self.reading_tolerance

    def _check_returned(self, screw, moved_back, expected):
        # Turning back should undo the test turn. A motor that only works one
        # way, or slips against the springs, won't.
        if not self._test_moved(moved_back) or moved_back * expected < 0:
            raise self.gcode.error(
                "STAA: Screw %d didn't come back when motor %d turned counter-clockwise"
                " (%.3fmm, expected %.3fmm). Check the motor and its coupling." % (
                    screw + 1, screw + 1, moved_back, expected))
        self._info("Motor %d OK (%s)" % (screw + 1, self._screw_name(screw)))


class _NoConfig:
    def getfloat(self, name, default=None, **kwargs):
        return default


def load_config(config):
    return ScrewsTiltAdjustAuto(config)

