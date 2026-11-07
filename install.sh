#!/usr/bin/env bash
# Links the Screws Tilt Adjust Auto extra into Klipper.
#
# Usage:
#   ./install.sh               install, then restart Klipper
#   ./install.sh --uninstall   remove the link, then restart Klipper
#
# Set KLIPPER_PATH if Klipper isn't in ~/klipper, and KLIPPER_CONFIG_PATH if
# printer.cfg isn't in ~/printer_data/config.
set -euo pipefail

MODULE="screws_tilt_adjust_auto.py"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_PATH="${KLIPPER_PATH:-$HOME/klipper}"
EXTRAS_DIR="$KLIPPER_PATH/klippy/extras"
LINK="$EXTRAS_DIR/$MODULE"
EXCLUDE_FILE="$KLIPPER_PATH/.git/info/exclude"
EXCLUDE_ENTRY="klippy/extras/$MODULE"
MACROS="screws_tilt_adjust_auto.cfg"
CONFIG_PATH="${KLIPPER_CONFIG_PATH:-$HOME/printer_data/config}"
MACROS_LINK="$CONFIG_PATH/$MACROS"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as the user Klipper runs as, not as root." >&2
    exit 1
fi

if [ ! -d "$EXTRAS_DIR" ]; then
    echo "Couldn't find Klipper's extras folder at $EXTRAS_DIR." >&2
    echo "Set KLIPPER_PATH to your Klipper folder and try again." >&2
    exit 1
fi

restart_klipper() {
    if command -v systemctl >/dev/null 2>&1 \
        && systemctl list-unit-files klipper.service >/dev/null 2>&1; then
        echo "Restarting Klipper..."
        sudo systemctl restart klipper
    else
        echo "Restart Klipper to apply the change."
    fi
}

if [ "${1:-}" = "--uninstall" ]; then
    if [ -L "$LINK" ]; then
        rm "$LINK"
        echo "Removed $LINK"
    else
        echo "Nothing to remove at $LINK"
    fi
    if [ -f "$EXCLUDE_FILE" ]; then
        sed -i "\#^${EXCLUDE_ENTRY}\$#d" "$EXCLUDE_FILE"
    fi
    if [ -L "$MACROS_LINK" ]; then
        rm "$MACROS_LINK"
        echo "Removed $MACROS_LINK. Remove [include $MACROS] from printer.cfg too."
    fi
    restart_klipper
    exit 0
fi

if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
    echo "$LINK already exists and isn't a link. Move it out of the way and try again." >&2
    exit 1
fi

ln -sfn "$SRC_DIR/$MODULE" "$LINK"
echo "Linked $LINK -> $SRC_DIR/$MODULE"

# The macros file goes next to printer.cfg so it can be included by name.
if [ -d "$CONFIG_PATH" ]; then
    if [ -e "$MACROS_LINK" ] && [ ! -L "$MACROS_LINK" ]; then
        echo "$MACROS_LINK already exists and isn't a link, so it was left alone." >&2
    else
        ln -sfn "$SRC_DIR/$MACROS" "$MACROS_LINK"
        echo "Linked $MACROS_LINK -> $SRC_DIR/$MACROS"
    fi
else
    echo "Couldn't find your config folder at $CONFIG_PATH, so the macros weren't linked." >&2
    echo "Set KLIPPER_CONFIG_PATH to the folder with printer.cfg and run this again." >&2
fi

# Keep Klipper's own git checkout clean so its updates aren't affected.
if [ -d "$KLIPPER_PATH/.git" ]; then
    mkdir -p "$(dirname "$EXCLUDE_FILE")"
    touch "$EXCLUDE_FILE"
    grep -qxF "$EXCLUDE_ENTRY" "$EXCLUDE_FILE" || echo "$EXCLUDE_ENTRY" >> "$EXCLUDE_FILE"
fi

restart_klipper
