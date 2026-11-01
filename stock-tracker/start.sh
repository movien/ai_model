#!/usr/bin/env bash
# Stock Tracker launcher for macOS / Linux:  ./start.sh   (extra options go to `tracker.py gui`)
# First run: creates a private Python environment in .venv and installs the 3 packages.
# Then it opens the dashboard in your browser. Keep this window open while tracking.
cd "$(dirname "$0")" || exit 1

pause_exit() { echo; read -r -p "Press Enter to close..." _; exit 1; }

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.9 or newer is needed."
  echo "  macOS:  download it from https://www.python.org/downloads/  (or: brew install python)"
  echo "  Linux:  sudo apt install python3 python3-venv   (or your distro's equivalent)"
  pause_exit
fi

if [ ! -x .venv/bin/python ]; then
  echo "Setting up for the first time. This takes a minute..."
  if ! "$PY" -m venv .venv; then
    rm -rf .venv
    echo "Could not create the Python environment. On Debian/Ubuntu run: sudo apt install python3-venv"
    pause_exit
  fi
fi

if ! .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt; then
  echo "Installing the required packages failed. Check your internet connection and try again."
  pause_exit
fi

exec .venv/bin/python tracker.py gui "$@"
