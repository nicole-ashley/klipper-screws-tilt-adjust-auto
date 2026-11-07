# Klipper Extra: Screws Tilt Adjust Auto
_Automatically adjust your bed screws with a development board and motors._

## Installation

### Set up your dev board
Open `screws-tilt-adjust-auto.ino` in Arduino IDE, and use the Library Manager to install [Adafruit Motor Shield library by Adafruit](https://github.com/adafruit/Adafruit-Motor-Shield-library) (not V2). Connect your board, compile and upload the code.

### Install the Klipper extra
On your Klipper computer's terminal, run these commands:
```sh
git clone https://github.com/nicole-ashley/klipper-screws-tilt-adjust-auto ~/screws-tilt-adjust-auto
ln -s ~/klipper/klippy/extras/screws-tilt-adjust-auto.py ~/screws-tilt-adjust-auto/screws-tilt-adjust-auto.py
```
And for automatic updates, add this section to your `moonraker.conf`:
```yml
[update_manager screws-tilt-adjust-auto]
type: git_repo
path: ~/screws-tilt-adjust-auto
origin: https://github.com/nicole-ashley/klipper-screws-tilt-adjust-auto
primary_branch: main
managed_services: klipper
```

## Usage
- `SCREWS_TILT_ADJUST_AUTO` measures the bed and turns the screws until it is level, then homes Z if any screw was moved.
- `SCREWS_TILT_ADJUST_AUTO WAIT_FOR_HEATERS=1 [HEATERS=heater_bed,extruder]` levels while the heaters warm up, then again once they're at temperature to cater for heat flex:
  1. If the heaters are already at their targets, it levels once and stops.
  2. Otherwise it levels straight away while they heat.
  3. If the heaters reached their targets during that, it stops.
  4. Otherwise it waits for them to reach their targets, then levels again.

  "At target" uses the same test Klipper's own heat-and-wait commands (`M109`, `M190`) use. Only heaters with a target set are waited on. `HEATERS` limits which heaters are checked and defaults to all of them. Z is homed at the end of every levelling pass that moved a screw. If the first pass can't reach level while the bed is still heating, that's not an error; the pass at temperature is the one that has to succeed.
- `SCREWS_TILT_ADJUST_AUTO ONE_BY_ONE=1` levels one screw at a time instead of moving every screw after each full measurement:
  1. Measure every screw.
  2. For each screw that needs moving: turn it, then measure just that screw against a reference screw that didn't need moving, and repeat until it's level.
  3. Go back to step 1, until a full measurement shows the bed is level.

  Measuring one screw probes only it and the reference, which is quicker than a full pass. It can be combined with `WAIT_FOR_HEATERS=1`. If a screw can't be levelled within `maximum_attempts` turns, the run stops with an error.
- `SCREWS_TILT_ADJUST_MANUAL SCREW=<n> DISTANCE=<turns>` turns one screw motor by the given number of turns (negative for counter-clockwise).

Set your heater targets first (for example with `M140 S60` and `M104 S200`), then run the command.

## Running the tests
```sh
python3 -m unittest discover -s tests
```

## Printing and assembly
_Coming soon._
