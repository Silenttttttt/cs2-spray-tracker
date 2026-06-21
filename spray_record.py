#!/usr/bin/env python3
"""
spray_record.py — record CS2 spray (mouse) input on Linux/X11.

Reads RAW mouse motion straight from the kernel via evdev (/dev/input/event*),
so it works no matter which window has focus — including when CS2 has an
exclusive pointer grab. It captures raw counts (REL_X / REL_Y), which is what
actually represents your recoil-compensation movement, instead of the OS cursor
position (which clamps at the screen edge and is useless in-game).

Each time you press-and-hold the configured mouse button, one "spray" is
recorded: a timestamped series of (dx, dy) deltas from press to release.
Sprays are saved as individual JSON files you can plot with spray_view.py.

No sudo needed if your user is in the `input` group (you are). It does NOT
grab the device, so the game still receives every event normally.

Usage:
    python3 spray_record.py                 # auto-detect mouse, save to ./sprays
    python3 spray_record.py --device /dev/input/event3
    python3 spray_record.py --button right
    python3 spray_record.py --out ~/cs2-sprays

Press Ctrl+C to stop.
"""

import argparse
import json
import os
import select
import sys
import time
from datetime import datetime

try:
    from evdev import InputDevice, categorize, ecodes, list_devices
except ImportError:
    sys.exit(
        "Missing dependency: python-evdev.\n"
        "  Manjaro/Arch:  sudo pacman -S python-evdev\n"
        "  pip:           pip install evdev"
    )

BUTTONS = {"left": ecodes.BTN_LEFT, "right": ecodes.BTN_RIGHT, "middle": ecodes.BTN_MIDDLE}


def is_mouse(dev):
    """True if the device emits relative X/Y motion and has the left button."""
    caps = dev.capabilities()
    rel = caps.get(ecodes.EV_REL, [])
    keys = caps.get(ecodes.EV_KEY, [])
    return (ecodes.REL_X in rel and ecodes.REL_Y in rel and ecodes.BTN_LEFT in keys)


def find_mice():
    mice = []
    for path in sorted(list_devices()):
        try:
            dev = InputDevice(path)
        except (PermissionError, OSError):
            continue
        if is_mouse(dev):
            mice.append(dev)
        else:
            dev.close()
    return mice


def autodetect(mice):
    """Return the first mouse to move. Lets the user pick by wiggling it."""
    if len(mice) == 1:
        print(f"Using the only mouse found: {mice[0].name}  ({mice[0].path})")
        return mice[0]

    print("Multiple mice found. Move the one you'll be spraying with...")
    for m in mice:
        print(f"   - {m.name}  ({m.path})")
    fd_to_dev = {m.fd: m for m in mice}
    while True:
        r, _, _ = select.select(fd_to_dev, [], [])
        for fd in r:
            dev = fd_to_dev[fd]
            for ev in dev.read():
                if ev.type == ecodes.EV_REL and ev.code in (ecodes.REL_X, ecodes.REL_Y) and ev.value != 0:
                    print(f"Selected: {dev.name}  ({dev.path})")
                    return dev


def open_device(args):
    if args.device:
        try:
            dev = InputDevice(args.device)
        except PermissionError:
            sys.exit(
                f"Permission denied opening {args.device}.\n"
                "Add yourself to the `input` group (then log out/in):\n"
                "  sudo usermod -aG input $USER\n"
                "...or run this script with sudo."
            )
        except OSError as e:
            sys.exit(f"Could not open {args.device}: {e}")
        if not is_mouse(dev):
            print(f"Warning: {dev.name} doesn't look like a mouse, using it anyway.")
        print(f"Using device: {dev.name}  ({dev.path})")
        return dev

    mice = find_mice()
    if not mice:
        sys.exit(
            "No mouse devices found (or no permission to read them).\n"
            "Make sure you're in the `input` group:  sudo usermod -aG input $USER"
        )
    return autodetect(mice)


def save_spray(out_dir, device_name, button, t0_epoch, samples):
    duration = samples[-1]["t"] if samples else 0.0
    net_dx = sum(s["dx"] for s in samples)
    net_dy = sum(s["dy"] for s in samples)
    path_len = 0.0
    for s in samples:
        path_len += (s["dx"] ** 2 + s["dy"] ** 2) ** 0.5

    stamp = datetime.fromtimestamp(t0_epoch).strftime("%Y%m%d_%H%M%S_%f")[:-3]
    fname = os.path.join(out_dir, f"spray_{stamp}.json")
    data = {
        "version": 1,
        "device": device_name,
        "button": button,
        "start_time": t0_epoch,
        "duration": duration,
        "n_samples": len(samples),
        "net_dx": net_dx,
        "net_dy": net_dy,
        "path_length": round(path_len, 1),
        "samples": samples,  # each: {"t": seconds_since_press, "dx": int, "dy": int}
    }
    with open(fname, "w") as f:
        json.dump(data, f)
    return fname, duration, net_dx, net_dy, path_len


def main():
    ap = argparse.ArgumentParser(description="Record CS2 spray mouse input via evdev.")
    ap.add_argument("--device", help="input event device, e.g. /dev/input/event3 (default: auto-detect)")
    ap.add_argument("--button", choices=BUTTONS, default="left", help="which mouse button is the trigger (default: left)")
    ap.add_argument("--out", default="sprays", help="directory to save spray JSON files (default: ./sprays)")
    ap.add_argument("--min-duration", type=float, default=0.10,
                    help="ignore holds shorter than this many seconds (default: 0.10)")
    ap.add_argument("--min-samples", type=int, default=3,
                    help="ignore holds with fewer than this many motion samples (default: 3)")
    args = ap.parse_args()

    out_dir = os.path.expanduser(args.out)
    os.makedirs(out_dir, exist_ok=True)

    dev = open_device(args)
    trigger = BUTTONS[args.button]

    print(f"\nRecording {args.button}-click sprays -> {out_dir}/")
    print("Hold the button to spray; release to save. Ctrl+C to stop.\n")

    recording = False
    t0 = 0.0
    samples = []
    pend_dx = pend_dy = 0
    count = 0

    try:
        for ev in dev.read_loop():
            if ev.type == ecodes.EV_KEY and ev.code == trigger:
                if ev.value == 1 and not recording:          # press
                    recording = True
                    t0 = ev.timestamp()
                    samples = []
                    pend_dx = pend_dy = 0
                elif ev.value == 0 and recording:            # release
                    recording = False
                    if len(samples) >= args.min_samples and (samples[-1]["t"] if samples else 0) >= args.min_duration:
                        count += 1
                        fname, dur, ndx, ndy, plen = save_spray(
                            out_dir, dev.name, args.button, t0, samples)
                        print(f"[{count:3d}] {dur*1000:6.0f} ms  "
                              f"{len(samples):4d} samples  "
                              f"net ({ndx:+5d},{ndy:+5d})  "
                              f"path {plen:6.0f}  ->  {os.path.basename(fname)}")
                    else:
                        print("      (ignored short click)")

            elif ev.type == ecodes.EV_REL and recording:
                if ev.code == ecodes.REL_X:
                    pend_dx += ev.value
                elif ev.code == ecodes.REL_Y:
                    pend_dy += ev.value

            elif ev.type == ecodes.EV_SYN and ev.code == ecodes.SYN_REPORT and recording:
                if pend_dx or pend_dy:
                    samples.append({"t": round(ev.timestamp() - t0, 6), "dx": pend_dx, "dy": pend_dy})
                    pend_dx = pend_dy = 0

    except KeyboardInterrupt:
        print(f"\nStopped. Recorded {count} spray(s) in {out_dir}/")
    finally:
        dev.close()


if __name__ == "__main__":
    main()
