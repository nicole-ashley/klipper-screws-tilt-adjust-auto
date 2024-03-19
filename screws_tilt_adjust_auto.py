# Automatically adjust bed screws using Screws Tilt Adjust and screw motors
#
# Copyright (C) 2024  Nicole Ashley <nicole@ashley.kiwi>
#
# This file may be distributed under the terms of the MIT license.
import re
import serial
import time

class ScrewsTiltAdjustAuto:
    def __init__(self, config):
        self.active = False
        self.measurements = []
        self.serial_device = config.get("serial_device")
        self.serial_baud = config.get("serial_baud")
        self.full_turn_time_in_seconds = config.getfloat("full_turn_time_in_seconds", 3)
        self.maximum_attempts = config.getint("maximum_attempts", 10)

        self.printer = config.get_printer()
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
            match = re.search(r":[^:]+: adjust (C?CW) (\d+):(\d+)", output)
            if match is not None:
                self.measurements.append({
                    "direction": match.group(1),
                    "hours": match.group(2),
                    "minutes": match.group(3)
                })

    def _connect_to_board(self):
        self.board = serial.Serial(
            port=self.serial_device,
            baudrate=self.serial_baud
        )

    def _disconnect_from_board(self):
        self.board.close()

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
        time.sleep(5)
        self.gcode.respond_info("STAA: " + ",".join(map(str, directions)))
        self._turn_motors(directions)
        self._disconnect_from_board()

    cmd_SCREWS_TILT_ADJUST_AUTO_help = "Tool to automatically adjust bed level screws"
    def cmd_SCREWS_TILT_ADJUST_AUTO(self, gcmd):
        self.gcode.respond_info("STAA: Starting")

        self._home_if_necessary()
        self._connect_to_board()

        for i in range(self.maximum_attempts):
            self._measure_bed_tilt()
            movements = self._calculate_motor_movements()
            self.gcode.respond_info("STAA: Motor movements: " + ", ".join(map(str, movements)))
            self._turn_motors(movements)
            if all(m == 0 for m in movements):
                self._disconnect_from_board()
                self.gcode.run_script_from_command("G28 Z")
                self.gcode.respond_info("STAA: Done")
                return

        self._disconnect_from_board()
        raise gcmd.error(
            "Error on '%s': STAA: Unable to reach level after %s attempts. Please check manually and retry." % (
                gcmd.get_commandline(), self.maximum_attempts,))

    def _home_if_necessary(self):
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.printer.get_reactor().monotonic()
        kin_status = toolhead.get_kinematics().get_status(curtime)
        if kin_status['homed_axes'] != 'xyz':
            self.gcode.respond_info("STAA: Homing toolhead")
            self.gcode.run_script_from_command("G28")

    def _measure_bed_tilt(self):
        self.active = True
        self.measurements = []
        self.gcode.respond_info("STAA: Measuring bed tilt")
        self.gcode.run_script_from_command("SCREWS_TILT_CALCULATE")
        self.active = False

    def _calculate_motor_movements(self):
        movements = []
        for m in self.measurements:
            direction = 1 if m["direction"] == "CW" else -1
            turns = float(m["hours"]) + (float(m["minutes"]) / 60)
            movements.append(turns * direction)

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
            time.sleep(wait_time)

        self.gcode.respond_info("STAA: Stopping motors.")
        self.board.write("0".encode())


def load_config(config):
    return ScrewsTiltAdjustAuto(config)

