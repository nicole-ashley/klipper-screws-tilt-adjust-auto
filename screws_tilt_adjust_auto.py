# Automatically adjust bed screws using Screws Tilt Adjust and screw motors
#
# Copyright (C) 2024  Nicole Ashley <nicole@ashley.kiwi>
#
# This file may be distributed under the terms of the MIT license.
import re
import serial

HANDSHAKE_TIMEOUT = 30.
HEATER_REPORT_INTERVAL = 10.

class ScrewsTiltAdjustAuto:
    def __init__(self, config):
        self.active = False
        self.measurements = []
        self.serial_device = config.get("serial_device")
        self.serial_baud = config.get("serial_baud")
        self.full_turn_time_in_seconds = config.getfloat("full_turn_time_in_seconds", 3)
        self.maximum_attempts = config.getint("maximum_attempts", 10)
        self.board = None
        self.one_by_one = False
        self.heights = []

        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command("SCREWS_TILT_ADJUST_AUTO",
                                    self.cmd_SCREWS_TILT_ADJUST_AUTO,
                                    desc=self.cmd_SCREWS_TILT_ADJUST_AUTO_help)
        self.gcode.register_command("SCREWS_TILT_ADJUST_MANUAL",
                                    self.cmd_SCREWS_TILT_ADJUST_MANUAL,
                                    desc=self.cmd_SCREWS_TILT_ADJUST_MANUAL_help)
        self.gcode.register_output_handler(self._handle_output)

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

    def _pause(self, seconds):
        # Let Klipper keep running (heater control in particular) while we
        # wait. A blocking sleep here stalls the host, and if heaters are on
        # for more than a few seconds Klipper shuts the printer down.
        self.reactor.pause(self.reactor.monotonic() + seconds)

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
                break
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

        directions = [0] * screw
        directions[screw - 1] = distance

        self._connect_to_board()
        try:
            self.gcode.respond_info("STAA: " + ",".join(map(str, directions)))
            self._turn_motors(directions)
        finally:
            self._disconnect_from_board()

    cmd_SCREWS_TILT_ADJUST_AUTO_help = "Tool to automatically adjust bed level screws"
    def cmd_SCREWS_TILT_ADJUST_AUTO(self, gcmd):
        self.one_by_one = bool(gcmd.get_int("ONE_BY_ONE", 0, minval=0, maxval=1))
        if gcmd.get_int("WAIT_FOR_HEATERS", 0, minval=0, maxval=1):
            self._adjust_while_heating(gcmd)
        else:
            self._adjust_or_fail(gcmd)

    def _adjust_or_fail(self, gcmd):
        if not self._auto_adjust():
            raise gcmd.error(
                "Error on '%s': STAA: Unable to reach level after %s attempts. Please check manually and retry." % (
                    gcmd.get_commandline(), self.maximum_attempts,))

    def _auto_adjust(self):
        # Returns True once the bed is level, or False if it ran out of attempts
        self.gcode.respond_info("STAA: Starting" + (" (one by one)" if self.one_by_one else ""))

        self._home_if_necessary()
        self._connect_to_board()
        adjustments_made = False
        level = False

        try:
            for i in range(self.maximum_attempts):
                self._measure_bed_tilt()
                movements = self._calculate_motor_movements()
                if all(m == 0 for m in movements):
                    level = True
                    break
                self.gcode.respond_info("STAA: Motor movements: " + ", ".join(map(str, movements)))
                adjustments_made = True
                if not self.one_by_one:
                    self._turn_motors(movements)
                elif not self._adjust_one_by_one(movements):
                    break
        finally:
            self._disconnect_from_board()

        if adjustments_made:
            self.gcode.respond_info("STAA: Homing Z as bed level will have changed")
            self.gcode.run_script_from_command("G28 Z")
        if level:
            self.gcode.respond_info("STAA: Done")
        return level

    def _adjust_one_by_one(self, movements):
        # Settle each screw in turn, probing only that screw between turns
        # and comparing it to the height the full measurement says it should
        # be at (that of a screw that doesn't need moving). The caller does a
        # full measurement again afterwards, since moving one screw can
        # slightly change the others. Returns False if a screw couldn't be
        # settled within maximum_attempts.
        if len(self.heights) != len(movements):
            raise self.gcode.error(
                "STAA: Expected %d screw heights but got %d" % (len(movements), len(self.heights)))
        target = self.heights[movements.index(0)]
        self._learn_turns_per_mm()
        for screw, movement in enumerate(movements):
            if movement == 0:
                continue
            for attempt in range(self.maximum_attempts):
                self.gcode.respond_info("STAA: Screw %d movement: %s" % (screw + 1, movement))
                self._turn_motors(self._single_motor(screw, movement, len(movements)))
                movement = self._height_to_turns(target - self._measure_screw(screw))
                if movement == 0:
                    break
            else:
                self.gcode.respond_info("STAA: Unable to level screw %d after %s attempts" % (
                    screw + 1, self.maximum_attempts))
                return False
            self.gcode.respond_info("STAA: Screw %d level" % (screw + 1,))
        return True

    def _single_motor(self, screw, movement, count):
        movements = [0] * count
        movements[screw] = movement
        return movements

    def _adjust_while_heating(self, gcmd):
        heaters = self._lookup_heaters(gcmd)

        if not self._heating_heaters(heaters):
            self.gcode.respond_info(
                "STAA: No heaters have a target temperature, adjusting once")
            self._adjust_or_fail(gcmd)
            return

        if self._heaters_at_target(heaters):
            self.gcode.respond_info(
                "STAA: Heaters already at target, adjusting once")
            self._adjust_or_fail(gcmd)
            return

        self.gcode.respond_info("STAA: Adjusting while heaters reach target")
        level = self._auto_adjust()

        if level and self._heaters_at_target(heaters):
            self.gcode.respond_info(
                "STAA: Heaters reached target during adjustment, finished")
            return
        if not level:
            # The bed is still moving as it heats, so this can happen. The
            # pass at temperature below is the one that has to succeed.
            self.gcode.respond_info(
                "STAA: Could not reach level while heating, will adjust again at target")

        self._wait_for_heaters(gcmd, heaters)
        self.gcode.respond_info("STAA: Heaters at target, adjusting for heat flex")
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
        self.gcode.respond_info("STAA: Waiting for heaters to reach target")
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
                self.gcode.respond_info("STAA: Waiting for " + ", ".join(temps))
                next_report = eventtime + HEATER_REPORT_INTERVAL
            self._pause(1.)

    def _home_if_necessary(self):
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.printer.get_reactor().monotonic()
        kin_status = toolhead.get_kinematics().get_status(curtime)
        if kin_status['homed_axes'] != 'xyz':
            self.gcode.respond_info("STAA: Homing toolhead")
            self.gcode.run_script_from_command("G28")

    def _measure_bed_tilt(self):
        self.gcode.respond_info("STAA: Measuring bed tilt")
        self._run_screws_tilt_calculate()

    def _run_screws_tilt_calculate(self):
        self.active = True
        self.measurements = []
        self.heights = []
        try:
            self.gcode.run_script_from_command("SCREWS_TILT_CALCULATE MAX_DEVIATION=10")
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
        self.gcode.respond_info("STAA: Measuring screw %d" % (screw + 1,))
        try:
            sta.screws = [screws[screw]]
            sta.probe_helper.probe_points = [coord for coord, name in sta.screws]
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
        self.turns_per_mm = sum(diff * turns for diff, turns in pairs) / total

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

    def _turn_motors(self, movements):
        for motor, distance in enumerate(movements, start=1):
            if distance == 0:
                continue
            elif distance < 0:
                motor *= -1

            wait_time = abs(distance) * self.full_turn_time_in_seconds
            self.gcode.respond_info("STAA: " + str(motor) + " for " + str(wait_time) + " seconds.")
            self.board.write(str(motor).encode())
            self._pause(wait_time)

        self.gcode.respond_info("STAA: Stopping motors.")
        self.board.write("0".encode())


def load_config(config):
    return ScrewsTiltAdjustAuto(config)

