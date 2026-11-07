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

  Every option runs with `WAIT_FOR_HEATERS=1`, so if the heaters are warming up it levels now and again at temperature. With no heaters on, or all at temperature, it levels once. Needs the `[include]` line above.
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

## Running the tests
```sh
python3 -m unittest discover -s tests
```

## Printing and assembly
_Coming soon._
