# Klipper Extra: Screws Tilt Adjust Auto
_Automatically adjust your bed screws with a development board and motors._

## Installation

### Set up your dev board
Open `screws-tilt-adjust-auto.ino` in Arduino IDE, and use the Library Manager to install [Adafruit Motor Shield library by Adafruit](https://github.com/adafruit/Adafruit-Motor-Shield-library) (not V2). Connect your board, compile and upload the code.

### Install the Klipper extra
On your Klipper computer's terminal, run these commands as the user Klipper runs as (usually `pi`):
```sh
git clone https://github.com/nicole-ashley/klipper-screws-tilt-adjust-auto ~/screws-tilt-adjust-auto
~/screws-tilt-adjust-auto/install.sh
```
The install script links the extra into `~/klipper/klippy/extras` and its macros file into `~/printer_data/config`, tells Klipper's git checkout to ignore the extra's link so Klipper updates aren't affected, and restarts Klipper. If Klipper isn't in `~/klipper`, set `KLIPPER_PATH` first, for example `KLIPPER_PATH=~/my-klipper ~/screws-tilt-adjust-auto/install.sh`. If `printer.cfg` isn't in `~/printer_data/config`, set `KLIPPER_CONFIG_PATH` the same way. You only need to run it once: updates to this repo are picked up through the link. To remove it, run `~/screws-tilt-adjust-auto/install.sh --uninstall`.

Then add a section for your dev board to `printer.cfg`, alongside your existing `[screws_tilt_adjust]` section:
```ini
[screws_tilt_adjust_auto]
serial_device: /dev/serial/by-id/usb-your-board-here
serial_baud: 115200
```

To get a "Level bed screws" button in Mainsail, Fluidd and KlipperScreen, also add this line to `printer.cfg`:
```ini
[include screws_tilt_adjust_auto.cfg]
```
The button needs a `[respond]` section, which most configs already have. If yours doesn't, add an empty `[respond]` line too. Don't edit the included file, as updates replace it. To customise it, copy it under another name and include that instead.

And for automatic updates, add this section to your `moonraker.conf`:
```ini
[update_manager screws-tilt-adjust-auto]
type: git_repo
path: ~/screws-tilt-adjust-auto
origin: https://github.com/nicole-ashley/klipper-screws-tilt-adjust-auto
primary_branch: main
managed_services: klipper
```

## Usage
- `LEVEL_BED_SCREWS` opens a dialog in Mainsail, Fluidd and KlipperScreen, and is listed with your other macros. It shows which heaters are on, then offers:
  - **Level**: the standard mode.
  - **One screw at a time**: `ONE_BY_ONE=1`.
  - **Continuous (eddy probe)**: `CONTINUOUS=1`, only shown when you have a `[probe_eddy_current]` probe.

  Every option runs with `WAIT_FOR_HEATERS=1`, so if the heaters are warming up it levels now and again at temperature. With no heaters on, or all at temperature, it levels once. While it runs, a dialog with a **Cancel** button stays open. Needs the `[include]` line above.
- `CANCEL_LEVEL_BED_SCREWS` (or `SCREWS_TILT_ADJUST_AUTO_CANCEL`) stops a running levelling, calibration or manual turn. See [Cancelling a run](#cancelling-a-run).
- `SCREWS_TILT_ADJUST_AUTO` measures the bed and turns the screws until it is level, then homes Z if any screw was moved.
- `SCREWS_TILT_ADJUST_AUTO WAIT_FOR_HEATERS=1 [HEATERS=heater_bed,extruder]` levels while the heaters warm up, then again once they're at temperature to cater for heat flex:
  1. If the heaters are already at their targets, it levels once and stops.
  2. Otherwise it levels straight away while they heat.
  3. If the heaters were already at their targets when the measurement that found the bed level was taken, it stops.
  4. Otherwise it waits for them to reach their targets, then levels again.

  "At target" uses the same test Klipper's own heat-and-wait commands (`M109`, `M190`) use. Only heaters with a target set are waited on. `HEATERS` limits which heaters are checked and defaults to all of them. Z is homed at the end of every levelling pass that moved a screw. If the first pass can't reach level while the bed is still heating, that's not an error; the pass at temperature is the one that has to succeed.
- `SCREWS_TILT_ADJUST_AUTO ONE_BY_ONE=1` levels one screw at a time instead of moving every screw after each full measurement:
  1. Measure every screw.
  2. For each screw that needs moving: turn it, then probe just that screw and compare it to the height the full measurement says it should be at. Repeat until it's level.
  3. Go back to step 1, until a full measurement shows the bed is level.

  How far to turn per millimetre is worked out from Klipper's own full measurement, so it follows whatever `screw_thread` you've set. It can be combined with `WAIT_FOR_HEATERS=1`. If a screw can't be levelled within `maximum_attempts` turns, the run stops with an error.
- `SCREWS_TILT_ADJUST_AUTO CONTINUOUS=1` is for eddy current probes, which can read the bed without touching it. It works like `ONE_BY_ONE=1`, but instead of probing a screw again after every turn, it parks the probe over the screw and keeps it there, reading the bed while the motor turns back and forth until that screw is level:
  1. Measure every screw.
  2. For each screw that needs moving: move the probe over it at `scan_height`, then turn the screw and read it again, over and over without moving the toolhead, until it's level.
  3. Go back to step 1, until a full measurement shows the bed is level.

  Readings use Klipper's own `METHOD=scan` probing, so drift compensation and the probe's calibrated range all apply. Only how the reading changes is used, so it doesn't matter if it's slightly offset from the full measurement. It needs a `[probe_eddy_current]` probe and a Klipper version from mid 2024 or later. It can be combined with `WAIT_FOR_HEATERS=1`.
- `SCREWS_TILT_ADJUST_MANUAL SCREW=<n> DISTANCE=<turns> [FORCE=1]` turns one screw motor by the given number of turns (negative for counter-clockwise). Turns bigger than `max_turns_per_move` are refused unless you add `FORCE=1`.
- Add `DIALOG=1` to `SCREWS_TILT_ADJUST_AUTO`, `SCREWS_TILT_ADJUST_AUTO_CALIBRATE` or `SCREWS_TILT_ADJUST_MANUAL` to show a dialog with a Cancel button while it runs, as the `LEVEL_BED_SCREWS` dialog does.
- `SCREWS_TILT_ADJUST_AUTO_CALIBRATE` measures how fast each motor turns each way, and its backlash, so screws get level in fewer tries. See [Motor calibration and backlash](#motor-calibration-and-backlash).

Optional settings for continuous mode, in the `[screws_tilt_adjust_auto]` section:
```ini
scan_height: 2          # toolhead Z to read the bed from, within the probe's calibrated range
scan_sample_time: 0.5   # seconds of readings averaged for each check
```

Set your heater targets first (for example with `M140 S60` and `M104 S200`), then run the command.

### Safety limits
So that a bad reading or a motor wired the wrong way can't wind a screw out of the bed, every run stops with an error, and leaves the motors stopped, if:
- any single turn would be bigger than `max_turns_per_move`. Nothing moves if the first measurement asks for too much, so a badly out of level bed needs levelling by hand (or with `SCREWS_TILT_ADJUST_MANUAL`) first, or a higher limit.
- any screw would turn more than `max_turns_per_run` in total during one `SCREWS_TILT_ADJUST_AUTO`, counting every pass and both directions.
- the bed gets further from level after the screws are turned. In `ONE_BY_ONE` and `CONTINUOUS` modes this is checked after every turn of every screw, which also catches a screw that barely moves two turns in a row (a slipping coupler or a stalled motor).
- the measurement doesn't fit a real screw thread, or (on Klipper versions that expose it) doesn't match the `screw_thread` in `[screws_tilt_adjust]`.
- any screw is more than `max_deviation` mm off, using Klipper's own `MAX_DEVIATION` check.

The board also stops any motor that Klipper hasn't told to keep running in the last 5 seconds, so a crash or a dropped USB connection mid-turn can't leave a motor running. This needs the latest `screws-tilt-adjust-auto.ino` uploaded to the board; older firmware still works but doesn't have this protection.

```ini
max_turns_per_move: 1      # biggest single turn, in turns
max_turns_per_run: 3       # most any screw may turn in one run, in turns
max_deviation: 2           # mm, passed to SCREWS_TILT_CALCULATE
reading_tolerance: 0.02    # mm of probe noise allowed before a reading counts as getting worse
```

### Cancelling a run
Press **Cancel** in the dialog, or send `CANCEL_LEVEL_BED_SCREWS` or `SCREWS_TILT_ADJUST_AUTO_CANCEL` from the console or a macro button. Klipper normally runs one command at a time, so a cancel would wait until the run had finished. Instead these are caught as soon as they arrive from Mainsail, Fluidd, KlipperScreen or anything else using Moonraker, and the run stops:
- A motor that's turning stops within a tenth of a second, and the connection to the board is closed.
- A probe move that's already under way (a measurement, or `G28`) finishes first, then the run stops before turning anything.
- A wait for heaters stops straight away. The heaters are left as they are.
- Cancelling a print does the same, for a run started from a print's start G-code: Mainsail's and Fluidd's Cancel button, or `CANCEL_PRINT`. The run stops, then the print cancel goes ahead as usual.
- Nothing more is measured or homed. If any screw was turned, it says so: home Z (`G28 Z`) before printing, since the bed has moved. Learned motor speeds are kept, and a cancelled calibration saves nothing.

The run ends with a "Cancelled" error, so a macro that started it stops too. A cancel from inside another macro or a print file waits its turn like any other command, so it can't stop a run.

### Motor calibration and backlash
Each motor runs at its own speed, and at a different speed each way, because one way works against the bed springs. Motors also take a moment to get going, and there's some slack to take up when a screw changes direction (backlash). Without calibration every motor is timed with `full_turn_time_in_seconds` and none of this is allowed for, so screws take more tries to get level, and small turns might not move at all.

`SCREWS_TILT_ADJUST_AUTO_CALIBRATE` measures all of this for each motor:
1. Levels the bed first, so the screws are calibrated where they normally sit. Motors run slower the tighter a screw is, so this matters. Add `LEVEL=0` to skip it.
2. For each screw, rocks it back and forth around where it sits and measures it after every move: a clockwise move to take up the slack, then two more clockwise moves of different lengths, then the same counter-clockwise. The two moves in a row give the speed and spin-up time for that direction. How far the first move after changing direction falls short gives the backlash.
3. Puts the screw back where it started, using what it just measured.
4. Shows the results. Run `SAVE_CONFIG` to keep them, as with Klipper's own calibration commands.

Options: `SCREW=<n>` calibrates just one screw. `CONTINUOUS=1` watches the screw with an eddy current probe instead of probing it after every move, which is quicker and more precise. `TURNS=<turns>` sets the size of the shortest move (default `calibration_turns`); use a bigger one if it says the backlash is more than the calibration moves. `CYCLES=<n>` sets how many times it goes back and forth (default `calibration_cycles`).

It needs to know how far a turn moves the bed. Klipper versions that say which `screw_thread` is set in `[screws_tilt_adjust]` give that directly. With older versions, calibrate with `LEVEL=0` while the bed is slightly out of level.

The values it saves, which you can also set by hand (the defaults are `full_turn_time_in_seconds`, no spin-up and no backlash):
```ini
motor1_cw_seconds_per_turn: 2.8
motor1_ccw_seconds_per_turn: 3.4
motor1_cw_spin_up_time: 0.1     # seconds before the motor starts turning; negative if it coasts on after stopping
motor1_ccw_spin_up_time: 0.15
motor1_backlash: 0.05           # turns of slack taken up when the motor changes direction
```
Keep these in `printer.cfg` itself, not in a file it includes, so `SAVE_CONFIG` can update them.

Backlash is added to the first turn after a motor changes direction. This needs to know which way the motor last turned, which is only kept across Klipper restarts if you have a `[save_variables]` section.

**Learning.** In `ONE_BY_ONE` and `CONTINUOUS` modes, every turn is followed by a reading, so it compares how far the screw actually moved with how far it was asked to, and moves that direction's speed part of the way toward what the turn suggests. That follows the motors as screws tighten or loosen over time. Turns too small to tell from the probe's noise, and readings that suggest a speed more than twice or less than half the current one, are skipped. With a `[save_variables]` section, learned speeds are kept for the next run, and are used in place of the ones in `printer.cfg`. Running the calibration again starts learning over from the new values. Default mode turns every screw together, so it doesn't learn.
```ini
learn_motor_speed: True     # set to False to only use calibrated values
calibration_turns: 0.125    # shortest calibration move, in turns
calibration_cycles: 2       # times calibration goes back and forth
```

## Running the tests
```sh
python3 -m unittest discover -s tests
```

## Printing and assembly
_Coming soon._
