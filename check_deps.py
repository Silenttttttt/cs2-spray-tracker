#!/usr/bin/env python3
"""Run this first to see what's missing and get the exact install command."""
import sys
import importlib

REQUIRED = [
    ("packaging",   "python-packaging",   True),
    ("evdev",       "python-evdev",       True),
    ("numpy",       "python-numpy",       True),
    ("matplotlib",  "python-matplotlib",  True),
    ("tkinter",     "tk",                 True),
]

missing_pacman = []
ok = []
for mod, pkg, needed in REQUIRED:
    try:
        importlib.import_module(mod)
        ok.append(mod)
    except ImportError:
        missing_pacman.append(pkg)

print(f"Python {sys.version}")
print()
if ok:
    print("OK:", ", ".join(ok))
if missing_pacman:
    print("\nMISSING — install with:")
    print(f"  sudo pacman -S {' '.join(missing_pacman)}")
else:
    print("\nAll dependencies present — spray_gui.py should run.")
