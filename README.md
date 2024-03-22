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

## Printing and assembly
_Coming soon._
