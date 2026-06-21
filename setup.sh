#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "Creating venv..."
python3.12 -m venv .venv

echo "Installing deps..."
.venv/bin/pip install --quiet evdev matplotlib numpy

echo "Done. Run with:  .venv/bin/python3 spray_gui.py"
