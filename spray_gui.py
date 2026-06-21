#!/usr/bin/env python3
"""
spray_gui.py — live CS2 spray tracker GUI.

Single window with:
  • Sidebar: spray history list, weapon selector, sensitivity slider,
             device selector, start/stop recording button.
  • Main view: 4 plot panels that update as new sprays arrive.
  • Status bar: recording indicator, sample count, current spray stats.

The recorder runs in a daemon thread and posts new sprays to a queue.
The GUI polls that queue every 500 ms and refreshes automatically.

Usage:
    python3 spray_gui.py
    python3 spray_gui.py --device /dev/input/event3
    python3 spray_gui.py --out ~/cs2-sprays

Deps (one Manjaro command):
    sudo pacman -S python-evdev python-matplotlib python-packaging python-numpy tk
"""

import argparse
import json
import os
import queue
import select
import subprocess
import sys
import threading
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# Weapon data (same as spray_view.py)
# ---------------------------------------------------------------------------

WEAPON_DATA = {
    "None": None,
    "AK-47": {
        "key": "ak47", "rpm": 600, "mag": 30, "color": "tomato",
        "pattern": [
            (0.00,  0.00), (0.90,  0.00), (2.40,  0.00), (4.00,  0.10),
            (5.50,  0.40), (6.70,  0.90), (7.60,  1.60), (8.30,  2.10),
            (8.90,  2.40), (9.30,  2.30), (9.50,  1.90), (9.60,  1.30),
            (9.70,  0.50), (9.70, -0.40), (9.80, -1.10), (9.80, -1.70),
            (9.80, -2.10), (9.70, -2.20), (9.60, -2.10), (9.50, -1.70),
            (9.40, -1.20), (9.30, -0.60), (9.20,  0.00), (9.10,  0.50),
            (9.00,  0.90), (8.90,  1.00), (8.80,  0.80), (8.70,  0.50),
            (8.60,  0.00), (8.50, -0.50),
        ],
    },
    "M4A4": {
        "key": "m4a4", "rpm": 666, "mag": 30, "color": "dodgerblue",
        "pattern": [
            (0.00,  0.00), (0.70,  0.00), (1.80,  0.00), (3.10,  0.10),
            (4.20,  0.30), (5.20,  0.60), (6.00,  1.00), (6.70,  1.30),
            (7.20,  1.40), (7.50,  1.30), (7.70,  1.00), (7.80,  0.60),
            (7.90,  0.10), (7.90, -0.40), (7.90, -0.80), (7.80, -1.10),
            (7.70, -1.30), (7.60, -1.40), (7.50, -1.30), (7.40, -1.00),
            (7.30, -0.60), (7.20, -0.20), (7.10,  0.20), (7.00,  0.50),
            (6.90,  0.70), (6.80,  0.80), (6.70,  0.70), (6.60,  0.50),
            (6.50,  0.10), (6.40, -0.30),
        ],
    },
    "M4A1-S": {
        "key": "m4a1s", "rpm": 600, "mag": 20, "color": "mediumseagreen",
        "pattern": [
            (0.00,  0.00), (0.60,  0.00), (1.60,  0.00), (2.80,  0.00),
            (3.80,  0.20), (4.70,  0.40), (5.40,  0.70), (5.90,  0.80),
            (6.30,  0.80), (6.50,  0.60), (6.60,  0.30), (6.70,  0.00),
            (6.70, -0.30), (6.70, -0.60), (6.60, -0.80), (6.50, -0.90),
            (6.40, -0.80), (6.30, -0.60), (6.20, -0.30), (6.10,  0.00),
        ],
    },
    "Galil AR": {
        "key": "galilar", "rpm": 666, "mag": 35, "color": "darkorange",
        "pattern": [
            (0.00,  0.00), (0.85,  0.00), (2.20,  0.00), (3.80,  0.10),
            (5.20,  0.35), (6.40,  0.80), (7.30,  1.50), (8.00,  2.00),
            (8.55,  2.20), (8.90,  2.10), (9.10,  1.70), (9.20,  1.10),
            (9.30,  0.40), (9.30, -0.30), (9.30, -1.00), (9.30, -1.60),
            (9.20, -2.00), (9.10, -2.10), (9.00, -1.90), (8.90, -1.50),
            (8.80, -0.90), (8.70, -0.20), (8.60,  0.50), (8.50,  1.00),
            (8.40,  1.30), (8.30,  1.20), (8.20,  0.90), (8.10,  0.50),
            (8.00,  0.00), (7.90, -0.40), (7.80, -0.80), (7.70, -1.00),
            (7.60, -0.90), (7.50, -0.60), (7.40, -0.20),
        ],
    },
    "FAMAS": {
        "key": "famas", "rpm": 800, "mag": 25, "color": "mediumpurple",
        "pattern": [
            (0.00,  0.00), (0.65,  0.00), (1.75,  0.00), (3.00,  0.05),
            (4.10,  0.25), (5.00,  0.50), (5.70,  0.65), (6.25,  0.70),
            (6.60,  0.55), (6.80,  0.25), (6.85,  0.00), (6.85, -0.30),
            (6.80, -0.60), (6.70, -0.85), (6.55, -0.95), (6.40, -0.85),
            (6.25, -0.60), (6.10, -0.30), (6.00,  0.05), (5.90,  0.35),
            (5.80,  0.55), (5.70,  0.60), (5.60,  0.45), (5.50,  0.20),
            (5.40,  0.00),
        ],
    },
}

# ---------------------------------------------------------------------------
# evdev helpers
# ---------------------------------------------------------------------------

def _try_import_evdev():
    try:
        import evdev
        return evdev
    except ImportError:
        return None


def find_mice(evdev):
    from evdev import ecodes
    mice = []
    for path in sorted(evdev.list_devices()):
        try:
            dev = evdev.InputDevice(path)
        except (PermissionError, OSError):
            continue
        caps = dev.capabilities()
        rel = caps.get(ecodes.EV_REL, [])
        keys = caps.get(ecodes.EV_KEY, [])
        if ecodes.REL_X in rel and ecodes.REL_Y in rel and ecodes.BTN_LEFT in keys:
            mice.append((dev.name, dev.path))
        dev.close()
    return mice


def pattern_to_counts(wdata, sensitivity, m_yaw=0.022, max_duration=None):
    """Return (times_s, ideal_dx_counts, ideal_dy_counts)."""
    pattern = wdata["pattern"]
    interval = 60.0 / wdata["rpm"]
    if max_duration is not None:
        n = min(len(pattern), int(max_duration / interval) + 2)
        pattern = pattern[:n]
    times = [i * interval for i in range(len(pattern))]
    ideal_dx = [-p[1] / (m_yaw * sensitivity) for p in pattern]
    ideal_dy = [ p[0] / (m_yaw * sensitivity) for p in pattern]
    return times, ideal_dx, ideal_dy


def cumulative_xy(spray):
    t, x, y = [], [], []
    cx = cy = 0
    for s in spray["samples"]:
        cx += s["dx"]
        cy += s["dy"]
        t.append(s["t"])
        x.append(cx)
        y.append(cy)
    return t, x, y


def chain_cumulative_xy(sprays):
    """Concatenate samples from multiple sprays end-to-end, continuing cumulative position."""
    t_all, x_all, y_all = [], [], []
    cx = cy = t_offset = 0.0
    for i, spray in enumerate(sprays):
        for s in spray["samples"]:
            cx += s["dx"]
            cy += s["dy"]
            t_all.append(s["t"] + t_offset)
            x_all.append(cx)
            y_all.append(cy)
        if i < len(sprays) - 1:
            t_offset += spray.get("duration", 0) + 0.05
    return t_all, x_all, y_all


def load_sprays(directory):
    import glob
    files = sorted(glob.glob(os.path.join(directory, "spray_*.json")))
    sprays = []
    for f in files:
        try:
            with open(f) as fh:
                data = json.load(fh)
            data["_file"] = os.path.basename(f)
            if data.get("samples"):
                sprays.append(data)
        except Exception:
            pass
    return sprays


def save_spray(out_dir, device_name, t0_epoch, samples, weapon="None"):
    duration = samples[-1]["t"] if samples else 0.0
    net_dx = sum(s["dx"] for s in samples)
    net_dy = sum(s["dy"] for s in samples)
    path_len = sum((s["dx"] ** 2 + s["dy"] ** 2) ** 0.5 for s in samples)
    stamp = datetime.fromtimestamp(t0_epoch).strftime("%Y%m%d_%H%M%S_%f")[:-3]
    fname = os.path.join(out_dir, f"spray_{stamp}.json")
    data = {
        "version": 1,
        "device": device_name,
        "weapon": weapon,
        "start_time": t0_epoch,
        "duration": duration,
        "n_samples": len(samples),
        "net_dx": net_dx,
        "net_dy": net_dy,
        "path_length": round(path_len, 1),
        "samples": samples,
    }
    with open(fname, "w") as f:
        json.dump(data, f)
    return fname


# ---------------------------------------------------------------------------
# Recorder thread
# ---------------------------------------------------------------------------

class Recorder(threading.Thread):
    """Reads evdev in a daemon thread; posts finished sprays to out_queue."""

    MIN_SAMPLES = 5

    def __init__(self, device_path, out_dir, out_queue, min_duration=0.50,
                 gui_hover_fn=None):
        super().__init__(daemon=True)
        self.device_path = device_path
        self.out_dir = out_dir
        self.out_queue = out_queue
        self._stop_evt = threading.Event()
        self.status = "idle"         # "idle" | "recording" | "error:<msg>"
        self.live_samples = 0        # count of samples in current spray
        self.weapon = "None"         # set by GUI; captured at LMB-down
        self.min_duration = min_duration
        self.gui_hover_fn = gui_hover_fn  # callable → True when mouse is over GUI
        self._live_lock = threading.Lock()
        self._live_active = False
        self._live_t0 = 0.0
        self._live_weapon = "None"
        self._live_ref = []          # reference to in-progress samples list

    def stop(self):
        self._stop_evt.set()

    def get_live_snapshot(self):
        with self._live_lock:
            if not self._live_active:
                return None
            snap_samples = list(self._live_ref)
            t0 = self._live_t0
            weapon = self._live_weapon
        return {"t0": t0, "weapon": weapon, "samples": snap_samples}

    def run(self):
        evdev = _try_import_evdev()
        if not evdev:
            self.status = "error:python-evdev not installed"
            return
        from evdev import ecodes

        try:
            dev = evdev.InputDevice(self.device_path)
        except (PermissionError, OSError) as e:
            self.status = f"error:{e}"
            return

        self.status = "idle"
        recording = False
        t0 = 0.0
        samples = []
        pend_dx = pend_dy = 0

        try:
            while not self._stop_evt.is_set():
                try:
                    r, _, _ = select.select([dev.fd], [], [], 0.2)
                except (OSError, ValueError):
                    self.status = "error:device disconnected"
                    return
                if not r:
                    continue
                try:
                    events = dev.read()
                except OSError:
                    self.status = "error:device disconnected"
                    return
                for ev in events:
                    if ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_LEFT:
                        if ev.value == 1 and not recording:
                            # Skip if click is inside the GUI window
                            if self.gui_hover_fn and self.gui_hover_fn():
                                pass
                            else:
                                recording = True
                                t0 = ev.timestamp()
                                samples = []
                                pend_dx = pend_dy = 0
                                spray_weapon = self.weapon
                                self.status = "recording"
                                self.live_samples = 0
                                with self._live_lock:
                                    self._live_active = True
                                    self._live_t0 = t0
                                    self._live_weapon = spray_weapon
                                    self._live_ref = samples
                        elif ev.value == 0 and recording:
                            recording = False
                            self.status = "idle"
                            self.live_samples = 0
                            with self._live_lock:
                                self._live_active = False
                            duration = samples[-1]["t"] if samples else 0
                            if (len(samples) >= self.MIN_SAMPLES and
                                    duration >= self.min_duration):
                                fname = save_spray(self.out_dir, dev.name, t0, samples,
                                                   weapon=spray_weapon)
                                self.out_queue.put(fname)

                    elif ev.type == ecodes.EV_REL and recording:
                        if ev.code == ecodes.REL_X:
                            pend_dx += ev.value
                        elif ev.code == ecodes.REL_Y:
                            pend_dy += ev.value

                    elif ev.type == ecodes.EV_SYN and ev.code == ecodes.SYN_REPORT and recording:
                        if pend_dx or pend_dy:
                            samples.append({
                                "t": round(ev.timestamp() - t0, 6),
                                "dx": pend_dx,
                                "dy": pend_dy,
                            })
                            self.live_samples = len(samples)
                            pend_dx = pend_dy = 0
        finally:
            dev.close()
            self.status = "stopped"


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class SprayApp:
    POLL_MS = 500          # how often to check for new sprays (ms)
    LIVE_POLL_MS = 80      # live-view refresh interval (~12 fps)
    MAX_HISTORY = 200      # max sprays shown in the list

    def __init__(self, root, out_dir, initial_device=None):
        import tkinter as tk
        from tkinter import ttk
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.collections import LineCollection
        import numpy as np

        self.tk = tk
        self.ttk = ttk
        self.plt = plt
        self.LineCollection = LineCollection
        self.FigureCanvasTkAgg = FigureCanvasTkAgg
        self.NavigationToolbar2Tk = NavigationToolbar2Tk
        self.np = np

        self.root = root
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)

        self.sprays = []          # all loaded sprays
        self.selected_idx = -1    # index into self.sprays
        self.recorder = None
        self.rec_queue = queue.Queue()
        self.current_device = initial_device or ""
        self._sens_per_weapon = {name: 1.0 for name in WEAPON_DATA}
        self._settings_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "settings.json")
        self._replay_active = False
        self._replay_after_id = None
        self._live_override = None   # synthetic spray dict during live view
        self._live_prev_count = 0    # sample count at last live render
        self._live_poll_running = False
        self._live_pattern_cache = None  # (cache_key, it, ix, iy)
        self._clip_notify_queue = queue.Queue()

        # PIL-based clip player state
        self._clip_paused    = True
        self._clip_speed     = 1.0
        self._clip_fps       = 30.0
        self._clip_duration  = 0.0
        self._clip_frame_num = 0
        self._clip_tick_id   = None
        self._clip_ffmpeg    = None          # decode subprocess
        self._clip_stop_evt  = threading.Event()
        self._frame_queue    = queue.Queue(maxsize=300)
        self._clip_photo     = None          # keep PhotoImage reference
        self._clip_vid_w     = 1
        self._clip_vid_h     = 1
        self._current_clip_path = None
        self._clip_muted            = True   # audio off by default
        self._clip_audio_proc       = None   # ffplay subprocess for audio
        self._clip_play_start_wall  = 0.0    # wall-clock reference for sync
        self._clip_play_start_frame = 0      # frame number at last play/resume
        self._mouse_in_gui = False  # updated by _poll_hover every 50 ms

        # Spray chaining state
        self.chains = []             # list of lists of spray indices
        self.spray_chain_id = {}     # spray_idx -> chain_idx
        self.selected_chain = []     # spray indices of selected chain (for plot)
        self._pending_clip_chain = []  # spray indices accumulating for debounced clip
        self._pending_clip_timer = None  # root.after id for debounce

        root.title("CS2 Spray Tracker")
        root.configure(bg="#1e1e1e")
        self._build_ui()
        self._apply_settings(self._load_settings())
        self._load_existing_sprays()
        self._schedule_poll()
        self._poll_hover()

    # ------------------------------------------------------------------
    # Clip helpers
    # ------------------------------------------------------------------

    @staticmethod
    @staticmethod
    def _find_gsr_replay_dir():
        """Parse the live gsr process cmdline for its -o output directory."""
        try:
            pids = subprocess.check_output(
                ["pgrep", "-f", "gpu-screen-recorder"], text=True).split()
            for pid in pids:
                try:
                    with open(f"/proc/{pid.strip()}/cmdline", "rb") as fh:
                        args = [a.decode("utf-8", errors="replace")
                                for a in fh.read().split(b"\x00")]
                    if "-o" in args:
                        idx = args.index("-o")
                        path = args[idx + 1]
                        if os.path.isdir(path):
                            return path, [int(p) for p in pids]
                except Exception:
                    pass
        except Exception:
            pass
        # Fallback: config file
        cfg = os.path.expanduser(
            "~/.var/app/com.dec05eba.gpu_screen_recorder/config/gpu-screen-recorder/config")
        try:
            with open(cfg) as fh:
                for line in fh:
                    if line.startswith("replay.save_directory "):
                        d = line.split(" ", 1)[1].strip()
                        if os.path.isdir(d):
                            return d, []
        except Exception:
            pass
        return None, []

    def _bullet_count_estimate(self, spray):
        """How many bullets were likely fired (RPM × duration, capped at mag)."""
        wdata = WEAPON_DATA.get(spray.get("weapon", "None"))
        if not wdata:
            return 0, 0
        mag = wdata["mag"]
        rpm = wdata["rpm"]
        dur = spray.get("duration", 0)
        n = min(mag, int(dur * rpm / 60) + 1)
        return n, mag

    def _maybe_save_clip(self, spray):
        """Single-spray gate check + save. Chain mode uses _schedule_clip instead."""
        n_shots, mag = self._bullet_count_estimate(spray)
        gate = getattr(self, "bullet_pct_var", None)
        threshold = (gate.get() / 100.0) if gate else 0.4
        if mag == 0 or n_shots < threshold * mag:
            return
        self._do_save_clip(spray)

    def _clip_worker(self, spray, replay_dir, before, sig_time):
        import glob, shutil
        deadline = time.time() + 15
        new_file = None
        while time.time() < deadline:
            time.sleep(0.5)
            after = set(glob.glob(os.path.join(replay_dir, "*.mp4")))
            candidates = {f for f in after - before
                          if os.path.getmtime(f) >= sig_time - 1.0}
            if candidates:
                new_file = max(candidates, key=os.path.getmtime)
                break
        if not new_file:
            return
        clips_dir = os.path.join(self.out_dir, "clips")
        os.makedirs(clips_dir, exist_ok=True)
        clip_name = f"clip_{os.path.splitext(spray['_file'])[0]}.mp4"
        dest = os.path.join(clips_dir, clip_name)
        try:
            shutil.move(new_file, dest)
        except Exception:
            return
        # Persist in spray JSON
        spray_path = os.path.join(self.out_dir, spray["_file"])
        try:
            with open(spray_path) as f:
                data = json.load(f)
            data["clip"] = clip_name
            with open(spray_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        spray["clip"] = clip_name
        # Update the live in-memory spray object (spray may be a copy from _fire_clip_chain)
        fname = spray.get("_file", "")
        for sp in self.sprays:
            if sp.get("_file") == fname:
                sp["clip"] = clip_name
                break
        self._clip_notify_queue.put(fname)

    # ------------------------------------------------------------------
    # Clip player (PIL + ffmpeg pipe)
    # ------------------------------------------------------------------

    def _play_clip(self):
        """Sidebar button — toggle play/pause for current clip."""
        if not self.sprays:
            return
        idx = max(0, min(self.selected_idx, len(self.sprays) - 1))
        clip_name = self.sprays[idx].get("clip", "")
        if not clip_name:
            return
        clip_path = os.path.join(self.out_dir, "clips", clip_name)
        if not os.path.exists(clip_path):
            return
        spray_dur = self.sprays[idx].get("duration", 0)
        if self._current_clip_path != clip_path:
            self._clip_load(clip_path, spray_duration=spray_dur)
        else:
            self._clip_toggle_play()

    def _clip_load(self, clip_path, spray_duration=0):
        """Probe the clip, show first frame, reset player."""
        self._clip_stop()
        self._current_clip_path = clip_path
        self._clip_spray_dur = spray_duration

        def _probe():
            try:
                result = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-print_format", "json",
                     "-show_streams", "-show_format", clip_path],
                    capture_output=True, text=True)
                info = json.loads(result.stdout)
                vs = next((s for s in info.get("streams", [])
                           if s.get("codec_type") == "video"), {})
                fps_raw = vs.get("r_frame_rate", "30/1")
                a, b = fps_raw.split("/")
                fps = float(a) / max(1, float(b))
                dur = float(vs.get("duration")
                            or info.get("format", {}).get("duration", 0))
            except Exception:
                fps, dur = 30.0, 0.0
            self._clip_fps      = max(1, fps)
            self._clip_duration = dur
            self.root.after(0, lambda: self._clip_finish_load(clip_path))

        threading.Thread(target=_probe, daemon=True).start()

    def _clip_trim_window(self):
        """Return (ss, to) trim points in seconds based on spray duration.

        GSR saves the last N seconds ending right when we sent the signal,
        which is at most ~0.5s after the spray ended.  We want:
          2 s before spray start … spray end … 1 s after (capped by clip).
        """
        clip_dur  = self._clip_duration
        spray_dur = max(0.1, self._clip_spray_dur)
        buf_before = getattr(self, "clip_before_var", None)
        buf_after  = getattr(self, "clip_after_var",  None)
        before = buf_before.get() if buf_before else 3.0
        after  = buf_after.get()  if buf_after  else 3.0
        poll_lag = after + 0.5   # intentional delay + up to 0.5 s poll cycle

        spray_end   = clip_dur - poll_lag
        spray_start = spray_end - spray_dur
        ss = max(0.0, spray_start - before)
        to = min(clip_dur, spray_end + after)
        return ss, to

    def _clip_finish_load(self, clip_path):
        """Called in main thread after probe — compute trim, show thumbnail."""
        self.root.update()
        w = self._clip_vid_label.winfo_width()
        h = self._clip_vid_label.winfo_height()
        if w < 4:
            w, h = 300, 180
        self._clip_vid_w = w
        self._clip_vid_h = h

        ss, to = self._clip_trim_window()
        self._clip_ss = ss
        self._clip_to = to
        trimmed_dur = to - ss
        self._clip_trimmed_dur = trimmed_dur   # used by _clip_tick for display

        def _fmt(s):
            s = max(0, int(s))
            return f"{s // 60}:{s % 60:02d}"
        self._clip_time_var.set(f"0:00 / {_fmt(trimmed_dur)}")

        def _thumb():
            try:
                from PIL import Image, ImageTk as _ITk
                proc = subprocess.Popen(
                    ["ffmpeg", "-ss", str(ss), "-i", clip_path,
                     "-frames:v", "1",
                     "-vf", f"scale={w}:{h}",
                     "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                data = proc.stdout.read(w * h * 3)
                proc.wait()
                if len(data) == w * h * 3:
                    img = Image.frombytes("RGB", (w, h), data)
                    photo = _ITk.PhotoImage(img)
                    self.root.after(0, lambda: self._clip_show_photo(photo))
            except Exception:
                pass

        threading.Thread(target=_thumb, daemon=True).start()
        self._update_clip_btn()

    def _clip_show_photo(self, photo):
        self._clip_photo = photo   # keep reference — PhotoImage gets GC'd otherwise
        self._clip_vid_label.configure(image=photo, text="")
        self._clip_pp_var.set("▶")

    def _clip_toggle_play(self):
        if not self._current_clip_path:
            return
        if self._clip_paused:
            self._clip_paused = False
            self._clip_pp_var.set("⏸")
            if not hasattr(self, "_clip_decode_started") or not self._clip_decode_started:
                self._clip_start_decode()
            # Wall-clock reference: video and audio both anchored to this moment.
            # +0.3 s gives ffplay ~300 ms to start; audio seeks 0.15 s earlier in
            # the file so audio position at t=0.3 matches video frame 0.
            self._clip_play_start_wall  = time.monotonic() + 0.3
            self._clip_play_start_frame = self._clip_frame_num
            self._clip_audio_start(self._clip_frame_num)
            self._clip_tick()
        else:
            self._clip_paused = True
            self._clip_pp_var.set("▶")
            self._clip_audio_stop()

    def _clip_toggle_fullscreen(self):
        self._clip_fullscreen = not self._clip_fullscreen
        was_playing = not self._clip_paused
        # Pause during resize to avoid stale-frame-size decode
        if was_playing:
            self._clip_paused = True
            self._clip_pp_var.set("▶")
        self._clip_stop()
        # Drop the current PhotoImage before resizing the container.
        # A label holding a large image doesn't shrink with the container —
        # it overflows downward and hides the control bar.
        self._clip_vid_label.configure(image="")
        self._clip_photo = None

        if self._clip_fullscreen:
            self._clip_container.place(relx=0, rely=0, anchor="nw",
                                       relwidth=1.0, relheight=1.0)
            self._clip_fs_var.set("⤡")
        else:
            self._clip_container.place(relx=1.0, rely=1.0, anchor="se",
                                       relwidth=0.375, relheight=0.46)
            self._clip_fs_var.set("⤢")

        # Reload at new dimensions after layout settles
        if self._current_clip_path:
            spray_dur = getattr(self, "_clip_spray_dur", 0)
            self.root.after(80, lambda: self._clip_load(
                self._current_clip_path, spray_duration=spray_dur))

    def _clip_toggle_mute(self):
        self._clip_muted = not self._clip_muted
        self._clip_mute_var.set("🔇" if self._clip_muted else "🔊")
        if self._clip_muted:
            self._clip_audio_stop()
        elif not self._clip_paused:
            self._clip_audio_start()

    def _clip_audio_start(self, frame_offset=0):
        """Start ffplay audio at the position matching frame_offset into the trim window."""
        self._clip_audio_stop()
        path = self._current_clip_path
        if not path or self._clip_muted:
            return
        clip_ss = getattr(self, "_clip_ss", 0.0)
        clip_to = getattr(self, "_clip_to", None)
        # Target position in the file for the current frame
        audio_ss = clip_ss + frame_offset / max(1.0, self._clip_fps)
        # Seek 0.15 s BEFORE the target so audio is at the right position when
        # video starts advancing (video is delayed +0.3 s, startup ≈ 0.15 s,
        # net: audio position matches video position at t = play_start + 0.3 s).
        # Clamp to 0.0 (not clip_ss) so we can seek before the trim window.
        audio_ss = max(0.0, audio_ss - 0.15)
        duration  = (clip_to - audio_ss) if clip_to is not None else None
        cmd = ["ffplay", "-nodisp", "-autoexit", "-ss", str(audio_ss)]
        if duration is not None:
            cmd += ["-t", str(max(0.0, duration))]
        cmd += [path]
        self._clip_audio_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _clip_audio_stop(self):
        if self._clip_audio_proc:
            try:
                self._clip_audio_proc.kill()
            except Exception:
                pass
            self._clip_audio_proc = None

    def _clip_save_trimmed(self):
        path = self._current_clip_path
        if not path or not os.path.exists(path):
            return
        ss = getattr(self, "_clip_ss", 0.0)
        to = getattr(self, "_clip_to", None)
        stem = os.path.splitext(os.path.basename(path))[0]
        out_path = os.path.join(self.out_dir, "clips", f"{stem}_trimmed.mp4")
        self.status_var.set("Exporting trimmed clip…")

        def _export():
            cmd = ["ffmpeg", "-y", "-ss", str(ss), "-i", path]
            if to is not None:
                cmd += ["-t", str(to - ss)]
            cmd += ["-c", "copy", out_path]
            try:
                subprocess.run(cmd, capture_output=True, check=True)
                self.root.after(0, lambda: self.status_var.set(
                    f"Saved → clips/{stem}_trimmed.mp4"))
            except subprocess.CalledProcessError:
                self.root.after(0, lambda: self.status_var.set("Export failed"))

        threading.Thread(target=_export, daemon=True).start()

    def _clip_start_decode(self):
        self._clip_decode_started = True
        self._clip_stop_evt.clear()
        w, h = self._clip_vid_w, self._clip_vid_h
        path = self._current_clip_path

        # Drain stale frames
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except Exception:
                break

        ss = getattr(self, "_clip_ss", 0.0)
        to = getattr(self, "_clip_to", None)
        cmd = ["ffmpeg", "-ss", str(ss), "-i", path]
        if to is not None:
            cmd += ["-t", str(to - ss)]   # duration, not end-time — unambiguous across ffmpeg versions
        cmd += ["-vf", f"scale={w}:{h}",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        self._clip_ffmpeg = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        frame_size = w * h * 3
        stop = self._clip_stop_evt
        q    = self._frame_queue
        proc = self._clip_ffmpeg

        def _reader():
            try:
                from PIL import Image
                while not stop.is_set():
                    data = proc.stdout.read(frame_size)
                    if len(data) < frame_size:
                        q.put(None)   # end-of-stream sentinel
                        break
                    img = Image.frombytes("RGB", (w, h), data)
                    while not stop.is_set():
                        try:
                            q.put(img, timeout=0.1)
                            break
                        except Exception:
                            pass
            except Exception:
                pass

        threading.Thread(target=_reader, daemon=True).start()

    def _clip_tick(self):
        if self._clip_paused:
            return

        fps   = max(1.0, self._clip_fps)
        speed = max(0.01, self._clip_speed)

        # How many frames should have been shown by now, based on wall clock.
        elapsed      = max(0.0, time.monotonic() - self._clip_play_start_wall) * speed
        target_frame = self._clip_play_start_frame + int(elapsed * fps)

        # Drain the queue until we reach the target frame (skip stale frames).
        item = None
        while self._clip_frame_num <= target_frame:
            try:
                candidate = self._frame_queue.get_nowait()
            except Exception:
                break   # queue not ready yet; show whatever we have and retry
            if candidate is None:
                # End of stream — loop back to the beginning
                self._clip_stop_evt.set()
                if self._clip_ffmpeg:
                    try:
                        self._clip_ffmpeg.kill()
                    except Exception:
                        pass
                self._clip_ffmpeg = None
                self._clip_frame_num     = 0
                self._clip_play_start_wall  = time.monotonic() + 0.3
                self._clip_play_start_frame = 0
                self._clip_decode_started = False
                self._clip_start_decode()
                self._clip_audio_start()
                self._clip_tick_id = self.root.after(16, self._clip_tick)
                return
            item = candidate
            self._clip_frame_num += 1

        if item is not None:
            from PIL import ImageTk as _ITk
            photo = _ITk.PhotoImage(item)
            self._clip_photo = photo
            self._clip_vid_label.configure(image=photo, text="")

        pos   = self._clip_frame_num / fps
        total = getattr(self, "_clip_trimmed_dur", self._clip_duration)

        def _fmt(s):
            s = max(0, int(s))
            return f"{s // 60}:{s % 60:02d}"
        self._clip_time_var.set(f"{_fmt(pos)} / {_fmt(total)}")

        # Schedule the next tick to fire exactly when the next frame is due.
        next_frame_wall = (self._clip_play_start_wall
                           + (self._clip_frame_num - self._clip_play_start_frame + 1)
                           / (fps * speed))
        interval = max(1, int((next_frame_wall - time.monotonic()) * 1000))
        self._clip_tick_id = self.root.after(interval, self._clip_tick)

    def _clip_set_speed(self, s):
        self._clip_speed = s
        # Highlight active speed button
        for spd, btn in self._clip_spd_btns.items():
            btn.config(style="Accent.TButton" if spd == s else "TButton")

    def _clip_stop(self):
        """Stop playback, kill ffmpeg, reset state."""
        self._clip_paused = True
        self._clip_stop_evt.set()
        if self._clip_tick_id:
            try:
                self.root.after_cancel(self._clip_tick_id)
            except Exception:
                pass
            self._clip_tick_id = None
        if self._clip_ffmpeg:
            try:
                self._clip_ffmpeg.kill()
            except Exception:
                pass
            self._clip_ffmpeg = None
        # Drain queue
        while not self._frame_queue.empty():
            try:
                self._frame_queue.get_nowait()
            except Exception:
                break
        self._clip_frame_num = 0
        self._clip_decode_started = False
        self._clip_pp_var.set("▶")
        self._clip_audio_stop()

    def _on_close(self):
        self._clip_stop()
        self.root.destroy()

    def _update_clip_btn(self):
        if not hasattr(self, "_clip_play_btn"):
            return
        if not self.sprays:
            self._clip_play_btn.config(state="disabled", text="▶  No clip")
            self._clip_set_label("no clip for this spray")
            return
        # Find clip across all sprays in the selected chain
        chain = [i for i in getattr(self, "selected_chain", []) if i < len(self.sprays)]
        if not chain:
            idx = max(0, min(self.selected_idx, len(self.sprays) - 1))
            chain = [idx]
        clip_name = next(
            (self.sprays[i].get("clip", "") for i in chain if self.sprays[i].get("clip")),
            "")
        has_clip = bool(clip_name)
        self._clip_play_btn.config(
            state="normal" if has_clip else "disabled",
            text="▶  Play clip" if has_clip else "▶  No clip",
        )
        if has_clip:
            clip_path = os.path.join(self.out_dir, "clips", clip_name)
            if os.path.exists(clip_path) and clip_path != self._current_clip_path:
                # Combined duration covers the whole chain for trim calculation
                spray_dur = sum(self.sprays[i].get("duration", 0) for i in chain)
                self._clip_load(clip_path, spray_duration=spray_dur)
        else:
            self._clip_stop()
            self._clip_set_label("no clip for this spray")
            self._current_clip_path = None

    def _clip_set_label(self, text):
        if hasattr(self, "_clip_vid_label"):
            self._clip_vid_label.configure(image="", text=text)

    # ------------------------------------------------------------------
    # UI build
    # ------------------------------------------------------------------

    def _build_ui(self):
        tk = self.tk
        ttk = self.ttk

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background="#1e1e1e", foreground="#d4d4d4",
                        fieldbackground="#2d2d2d", bordercolor="#3c3c3c")
        style.configure("TButton", padding=4)
        style.configure("TLabel", background="#1e1e1e", foreground="#d4d4d4")
        style.configure("TLabelframe", background="#1e1e1e", foreground="#9cdcfe")
        style.configure("TLabelframe.Label", background="#1e1e1e", foreground="#9cdcfe")
        style.configure("TNotebook", background="#1e1e1e", borderwidth=0)
        style.configure("TNotebook.Tab", background="#2d2d2d", foreground="#9d9d9d",
                        padding=[10, 4])
        style.map("TNotebook.Tab",
                  background=[("selected", "#1e1e1e")],
                  foreground=[("selected", "#569cd6")])
        style.configure("TCombobox", fieldbackground="#2d2d2d", foreground="#d4d4d4",
                        selectbackground="#094771")
        style.configure("Treeview", background="#252526", foreground="#cccccc",
                        fieldbackground="#252526", rowheight=22)
        style.configure("Treeview.Heading", background="#3c3c3c", foreground="#d4d4d4")
        style.map("Treeview", background=[("selected", "#094771")])

        # Top-level layout: sidebar (left) + plot area (right)
        pane = tk.PanedWindow(self.root, orient=tk.HORIZONTAL,
                              bg="#1e1e1e", sashwidth=6, sashrelief=tk.FLAT)
        pane.pack(fill=tk.BOTH, expand=True)

        sidebar_outer = tk.Frame(pane, bg="#1e1e1e", width=240)
        pane.add(sidebar_outer, minsize=200)

        # Scrollable sidebar
        sb_scroll = ttk.Scrollbar(sidebar_outer, orient="vertical")
        sb_canvas = tk.Canvas(sidebar_outer, bg="#1e1e1e", highlightthickness=0,
                              yscrollcommand=sb_scroll.set)
        sb_scroll.configure(command=sb_canvas.yview)
        sb_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        sb_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        sidebar = tk.Frame(sb_canvas, bg="#1e1e1e")
        _win = sb_canvas.create_window((0, 0), window=sidebar, anchor="nw")

        def _on_sidebar_resize(e):
            sb_canvas.configure(scrollregion=sb_canvas.bbox("all"))
        def _on_canvas_resize(e):
            sb_canvas.itemconfig(_win, width=e.width)
        sidebar.bind("<Configure>", _on_sidebar_resize)
        sb_canvas.bind("<Configure>", _on_canvas_resize)

        def _on_mousewheel(e):
            sb_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        sb_canvas.bind_all("<Button-4>",
                           lambda e: sb_canvas.yview_scroll(-1, "units"))
        sb_canvas.bind_all("<Button-5>",
                           lambda e: sb_canvas.yview_scroll(1, "units"))

        plot_frame = tk.Frame(pane, bg="#1e1e1e")
        pane.add(plot_frame, minsize=600)

        self._build_sidebar(sidebar)

        self._build_plot_area(plot_frame)
        self._build_status_bar()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_sidebar(self, parent):
        tk = self.tk
        ttk = self.ttk

        parent.columnconfigure(0, weight=1)
        row = 0

        # --- Device ---
        lf = ttk.LabelFrame(parent, text="Mouse device")
        lf.grid(row=row, column=0, sticky="ew", padx=6, pady=(8, 3))
        lf.columnconfigure(0, weight=1)
        row += 1

        self.device_var = tk.StringVar(value=self.current_device)
        self.device_combo = ttk.Combobox(lf, textvariable=self.device_var, width=26)
        self.device_combo.grid(row=0, column=0, padx=4, pady=3)
        ttk.Button(lf, text="Refresh devices", command=self._refresh_devices).grid(
            row=1, column=0, padx=4, pady=(0, 4))
        self._refresh_devices()

        # --- Record ---
        lf2 = ttk.LabelFrame(parent, text="Recorder  (arm once → hold LMB per spray)")
        lf2.grid(row=row, column=0, sticky="ew", padx=6, pady=3)
        lf2.columnconfigure(0, weight=1)
        row += 1

        self.rec_btn_text = tk.StringVar(value="▶  Arm recorder")
        self.rec_btn = ttk.Button(lf2, textvariable=self.rec_btn_text,
                                  command=self._toggle_recording, width=22)
        self.rec_btn.grid(row=0, column=0, padx=6, pady=6)

        self.rec_indicator = tk.Label(lf2, text="○  off", bg="#1e1e1e",
                                      fg="#666666", font=("monospace", 9))
        self.rec_indicator.grid(row=1, column=0)

        self.min_hold_var = tk.IntVar(value=500)
        tk.Scale(lf2, variable=self.min_hold_var, from_=100, to=2000, resolution=50,
                 orient=tk.HORIZONTAL, bg="#1e1e1e", fg="#d4d4d4",
                 troughcolor="#3c3c3c", highlightbackground="#1e1e1e",
                 showvalue=False, length=190,
                 command=self._on_min_hold_change).grid(row=2, column=0, padx=4, pady=2)
        self.min_hold_label = tk.Label(lf2, text="min hold: 500 ms",
                                       bg="#1e1e1e", fg="#555555", font=("monospace", 8))
        self.min_hold_label.grid(row=3, column=0, pady=(0, 2))

        self.live_mode_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(lf2, text="Live view  (update while spraying)",
                        variable=self.live_mode_var,
                        command=self._on_live_toggle).grid(
            row=4, column=0, sticky="w", padx=8, pady=(0, 2))

        self.clip_auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(lf2, text="Auto-clip  (via gsr)",
                        variable=self.clip_auto_var,
                        command=self._save_settings).grid(
            row=5, column=0, sticky="w", padx=8, pady=(0, 2))

        self.bullet_pct_var = tk.IntVar(value=40)
        tk.Scale(lf2, variable=self.bullet_pct_var, from_=0, to=100, resolution=5,
                 orient=tk.HORIZONTAL, bg="#1e1e1e", fg="#d4d4d4",
                 troughcolor="#3c3c3c", highlightbackground="#1e1e1e",
                 showvalue=False, length=190,
                 command=lambda _: self._on_bullet_pct_change()).grid(
            row=6, column=0, padx=4, pady=2)
        self.bullet_pct_label = tk.Label(lf2, text="clip gate: ≥40% bullets",
                                         bg="#1e1e1e", fg="#555555",
                                         font=("monospace", 8))
        self.bullet_pct_label.grid(row=7, column=0, pady=(0, 2))

        self.clip_before_var = tk.DoubleVar(value=3.0)
        tk.Scale(lf2, variable=self.clip_before_var, from_=0, to=10, resolution=0.5,
                 orient=tk.HORIZONTAL, bg="#1e1e1e", fg="#d4d4d4",
                 troughcolor="#3c3c3c", highlightbackground="#1e1e1e",
                 showvalue=False, length=190,
                 command=lambda _: self._on_clip_buf_change()).grid(
            row=8, column=0, padx=4, pady=2)
        self.clip_before_label = tk.Label(lf2, text="clip: 3.0 s before spray",
                                          bg="#1e1e1e", fg="#555555",
                                          font=("monospace", 8))
        self.clip_before_label.grid(row=9, column=0, pady=(0, 2))

        self.clip_after_var = tk.DoubleVar(value=3.0)
        tk.Scale(lf2, variable=self.clip_after_var, from_=0, to=10, resolution=0.5,
                 orient=tk.HORIZONTAL, bg="#1e1e1e", fg="#d4d4d4",
                 troughcolor="#3c3c3c", highlightbackground="#1e1e1e",
                 showvalue=False, length=190,
                 command=lambda _: self._on_clip_buf_change()).grid(
            row=10, column=0, padx=4, pady=2)
        self.clip_after_label = tk.Label(lf2, text="clip: 3.0 s after spray",
                                         bg="#1e1e1e", fg="#555555",
                                         font=("monospace", 8))
        self.clip_after_label.grid(row=11, column=0, pady=(0, 2))

        self.chain_gap_var = tk.DoubleVar(value=1.0)
        tk.Scale(lf2, variable=self.chain_gap_var, from_=0, to=5, resolution=0.5,
                 orient=tk.HORIZONTAL, bg="#1e1e1e", fg="#d4d4d4",
                 troughcolor="#3c3c3c", highlightbackground="#1e1e1e",
                 showvalue=False, length=190,
                 command=lambda _: self._on_chain_gap_change()).grid(
            row=12, column=0, padx=4, pady=2)
        self.chain_gap_label = tk.Label(lf2, text="chain gap: 1.0 s (links nearby sprays)",
                                        bg="#1e1e1e", fg="#555555",
                                        font=("monospace", 8))
        self.chain_gap_label.grid(row=13, column=0, pady=(0, 5))

        # --- Weapon ---
        lf3 = ttk.LabelFrame(parent, text="Weapon")
        lf3.grid(row=row, column=0, sticky="ew", padx=6, pady=3)
        lf3.columnconfigure(0, weight=1)
        lf3.columnconfigure(1, weight=1)
        row += 1

        self.weapon_var = tk.StringVar(value="None")
        weapon_fg = {
            "None":     "#888888",
            "AK-47":   "#e07060",
            "M4A4":    "#5b9bd5",
            "M4A1-S":  "#4ec994",
            "Galil AR": "#e09040",
            "FAMAS":   "#b080e0",
        }
        self._weapon_btns = {}
        names = list(WEAPON_DATA.keys())
        cols = 2
        for i, name in enumerate(names):
            btn = tk.Button(
                lf3, text=name, font=("monospace", 9, "bold"),
                relief=tk.FLAT, bd=0, padx=6, pady=4, cursor="hand2",
                command=lambda n=name: self._select_weapon(n),
            )
            btn.grid(row=i // cols, column=i % cols, sticky="ew", padx=3, pady=2)
            self._weapon_btns[name] = btn
        self._select_weapon("None", refresh=False)

        # --- Sensitivity (per weapon) ---
        lf4 = ttk.LabelFrame(parent, text="Sensitivity  (saved per weapon)")
        lf4.grid(row=row, column=0, sticky="ew", padx=6, pady=3)
        lf4.columnconfigure(0, weight=1)
        row += 1

        self.sens_var = tk.DoubleVar(value=1.0)
        tk.Scale(lf4, variable=self.sens_var, from_=0.2, to=5.0, resolution=0.05,
                 orient=tk.HORIZONTAL, bg="#1e1e1e", fg="#d4d4d4",
                 troughcolor="#3c3c3c", highlightbackground="#1e1e1e",
                 showvalue=False, length=190,
                 command=self._on_sens_change).grid(
            row=0, column=0, padx=4, pady=2)
        self.sens_label = ttk.Label(lf4, text="1.0")
        self.sens_label.grid(row=1, column=0, pady=(0, 4))

        # --- Analysis ---
        lf_analysis = ttk.LabelFrame(parent, text="Analysis")
        lf_analysis.grid(row=row, column=0, sticky="ew", padx=6, pady=3)
        lf_analysis.columnconfigure(0, weight=1)
        row += 1

        self.detrend_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(lf_analysis, text="Spray only  (remove tracking)",
                        variable=self.detrend_var,
                        command=self._on_detrend_toggle).grid(
            row=0, column=0, sticky="w", padx=8, pady=(4, 0))

        self.detrend_win_var = tk.IntVar(value=150)
        tk.Scale(lf_analysis, variable=self.detrend_win_var,
                 from_=30, to=600, resolution=10,
                 orient=tk.HORIZONTAL, bg="#1e1e1e", fg="#d4d4d4",
                 troughcolor="#3c3c3c", highlightbackground="#1e1e1e",
                 showvalue=False, length=190,
                 command=lambda _: self._on_detrend_slide()).grid(
            row=1, column=0, padx=4, pady=2)
        self.detrend_win_label = tk.Label(lf_analysis, text="window: 150 ms",
                                          bg="#1e1e1e", fg="#555555",
                                          font=("monospace", 8))
        self.detrend_win_label.grid(row=2, column=0, pady=(0, 2))

        self.vfocus_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(lf_analysis, text="Vertical focus  (error vs ideal)",
                        variable=self.vfocus_var,
                        command=self._on_vfocus_toggle).grid(
            row=3, column=0, sticky="w", padx=8, pady=(0, 5))

        # --- Replay ---
        lf_replay = ttk.LabelFrame(parent, text="Replay")
        lf_replay.grid(row=row, column=0, sticky="ew", padx=6, pady=3)
        lf_replay.columnconfigure(0, weight=1)
        row += 1

        self._replay_btn_text = tk.StringVar(value="▶  Play")
        ttk.Button(lf_replay, textvariable=self._replay_btn_text,
                   command=self._toggle_replay, width=22).grid(
            row=0, column=0, padx=6, pady=(6, 2))

        self.replay_speed_var = tk.DoubleVar(value=0.5)
        tk.Scale(lf_replay, variable=self.replay_speed_var, from_=0.1, to=4.0,
                 resolution=0.1, orient=tk.HORIZONTAL, bg="#1e1e1e", fg="#d4d4d4",
                 troughcolor="#3c3c3c", highlightbackground="#1e1e1e",
                 showvalue=False, length=190,
                 command=self._on_replay_speed_change).grid(
            row=1, column=0, padx=4, pady=2)
        self.replay_speed_label = tk.Label(lf_replay, text="speed: 0.5×",
                                           bg="#1e1e1e", fg="#555555",
                                           font=("monospace", 8))
        self.replay_speed_label.grid(row=2, column=0, pady=(0, 2))


        # --- Spray history ---
        lf5 = ttk.LabelFrame(parent, text="Spray history")
        lf5.grid(row=row, column=0, sticky="ew", padx=6, pady=3)
        lf5.columnconfigure(0, weight=1)
        row += 1

        cols = ("wep", "dur", "samples", "net_y")
        self.history_tree = ttk.Treeview(lf5, columns=cols, show="headings", height=12)
        self.history_tree.heading("wep", text="weapon")
        self.history_tree.heading("dur", text="ms")
        self.history_tree.heading("samples", text="smp")
        self.history_tree.heading("net_y", text="net-Y")
        self.history_tree.column("wep", width=58, anchor="w")
        self.history_tree.column("dur", width=48, anchor="e")
        self.history_tree.column("samples", width=38, anchor="e")
        self.history_tree.column("net_y", width=48, anchor="e")
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf5, orient="vertical", command=self.history_tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.history_tree.configure(yscrollcommand=sb.set)
        self.history_tree.bind("<<TreeviewSelect>>", self._on_history_select)

        self._clip_play_btn = ttk.Button(lf5, text="▶  No clip",
                                         command=self._play_clip, state="disabled")
        self._clip_play_btn.grid(row=1, column=0, columnspan=2,
                                 sticky="ew", padx=6, pady=(4, 6))

        # --- Clear buttons ---
        clr_frame = tk.Frame(parent, bg="#1e1e1e")
        clr_frame.grid(row=row, column=0, sticky="ew", padx=6, pady=(2, 8))
        clr_frame.columnconfigure(0, weight=1)
        clr_frame.columnconfigure(1, weight=1)
        ttk.Button(clr_frame, text="Clear selected",
                   command=self._clear_selected).grid(row=0, column=0, sticky="ew", padx=(0, 2))
        ttk.Button(clr_frame, text="Clear all",
                   command=self._clear_all).grid(row=0, column=1, sticky="ew", padx=(2, 0))

    def _build_plot_area(self, parent):
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.gridspec import GridSpec

        self.fig = plt.figure(figsize=(10, 7), facecolor="#1e1e1e")
        gs = GridSpec(2, 2, figure=self.fig,
                      width_ratios=[1.65, 1],
                      left=0.07, right=0.97, top=0.91, bottom=0.07,
                      hspace=0.38, wspace=0.42)

        self.ax_traj = self.fig.add_subplot(gs[:, 0])   # left: full height, trajectory
        self.ax_all  = self.fig.add_subplot(gs[0, 1])   # top-right: all sprays
        # gs[1, 1] (bottom-right) is left empty — video panel sits there

        # Create colorbar ONCE here with a placeholder so ax_traj is only shrunk once
        import matplotlib.cm as cm
        import matplotlib.colors as mcolors
        _sm = cm.ScalarMappable(cmap="viridis", norm=mcolors.Normalize(0, 1))
        self._colorbar = self.fig.colorbar(_sm, ax=self.ax_traj,
                                           fraction=0.035, pad=0.02)
        self._colorbar.ax.tick_params(labelcolor="#9d9d9d", labelsize=7)
        self._colorbar.set_label("time (s)", color="#9d9d9d", fontsize=7)

        for ax in (self.ax_traj, self.ax_all):
            ax.set_facecolor("#252526")
            ax.tick_params(colors="#9d9d9d", labelsize=7)
            ax.xaxis.label.set_color("#9d9d9d")
            ax.yaxis.label.set_color("#9d9d9d")
            ax.title.set_color("#d4d4d4")
            for spine in ax.spines.values():
                spine.set_edgecolor("#3c3c3c")

        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        canvas_tk = self.canvas.get_tk_widget()
        canvas_tk.pack(fill=self.tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, parent)
        toolbar.update()
        toolbar.configure(bg="#1e1e1e")

        # Video player panel in the bottom-right slot (where ax_cum was)
        tk  = self.tk
        ttk = self.ttk

        self._clip_container = tk.Frame(canvas_tk, bg="#1e1e1e", bd=1, relief=tk.SUNKEN)
        self._clip_container.place(relx=1.0, rely=1.0, anchor="se",
                                   relwidth=0.375, relheight=0.46)
        self._clip_fullscreen = False
        self._canvas_tk = canvas_tk   # needed for fullscreen toggle

        # Video display area — shows PIL frames or "no clip" text
        self._clip_vid_label = tk.Label(self._clip_container, bg="black",
                                        text="no clip for this spray",
                                        fg="#333333", font=("monospace", 8),
                                        anchor="center")
        self._clip_vid_label.pack(fill=tk.BOTH, expand=True)

        # Controls bar
        ctrl = tk.Frame(self._clip_container, bg="#252526")
        ctrl.pack(fill=tk.X, side=tk.BOTTOM)

        row1 = tk.Frame(ctrl, bg="#252526")
        row1.pack(fill=tk.X, padx=4, pady=(3, 1))

        self._clip_pp_var = tk.StringVar(value="▶")
        ttk.Button(row1, textvariable=self._clip_pp_var, width=3,
                   command=self._clip_toggle_play).pack(side=tk.LEFT, padx=(0, 4))

        self._clip_time_var = tk.StringVar(value="0:00 / 0:00")
        tk.Label(row1, textvariable=self._clip_time_var, bg="#252526",
                 fg="#9d9d9d", font=("monospace", 8)).pack(side=tk.LEFT)

        self._clip_fs_var = tk.StringVar(value="⤢")
        ttk.Button(row1, textvariable=self._clip_fs_var, width=3,
                   command=self._clip_toggle_fullscreen).pack(side=tk.RIGHT)

        self._clip_mute_var = tk.StringVar(value="🔇")
        ttk.Button(row1, textvariable=self._clip_mute_var, width=3,
                   command=self._clip_toggle_mute).pack(side=tk.RIGHT, padx=(0, 2))

        row2 = tk.Frame(ctrl, bg="#252526")
        row2.pack(fill=tk.X, padx=4, pady=(1, 3))

        tk.Label(row2, text="Speed:", bg="#252526", fg="#666666",
                 font=("monospace", 7)).pack(side=tk.LEFT)
        self._clip_spd_btns = {}
        for spd in (0.1, 0.25, 0.5, 1.0):
            lbl = f"{spd}×"
            b = ttk.Button(row2, text=lbl, width=5,
                           command=lambda s=spd: self._clip_set_speed(s))
            b.pack(side=tk.LEFT, padx=1)
            self._clip_spd_btns[spd] = b

        ttk.Button(row2, text="💾", width=3,
                   command=self._clip_save_trimmed).pack(side=tk.RIGHT)

    def _build_status_bar(self):
        tk = self.tk
        self.status_var = tk.StringVar(value="Ready")
        bar = tk.Label(self.root, textvariable=self.status_var, bg="#007acc",
                       fg="white", anchor="w", font=("monospace", 8), padx=6)
        bar.pack(fill=tk.X, side=tk.BOTTOM)

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------

    def _load_settings(self):
        try:
            with open(self._settings_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _apply_settings(self, s):
        if not s:
            return
        # Per-weapon sensitivity
        for name, val in s.get("sens_per_weapon", {}).items():
            if name in self._sens_per_weapon:
                self._sens_per_weapon[name] = float(val)
        # Detrend
        if "detrend_enabled" in s:
            self.detrend_var.set(bool(s["detrend_enabled"]))
        if "detrend_win" in s:
            self.detrend_win_var.set(int(s["detrend_win"]))
            self.detrend_win_label.config(text=f"window: {int(s['detrend_win'])} ms")
        # Weapon (also loads that weapon's sensitivity into slider)
        saved_weapon = s.get("weapon", "None")
        if saved_weapon in WEAPON_DATA:
            self._select_weapon(saved_weapon, refresh=False)
        # Min hold time
        if "min_hold_ms" in s:
            ms = int(s["min_hold_ms"])
            self.min_hold_var.set(ms)
            self.min_hold_label.config(text=f"min hold: {ms} ms")
        # Vertical focus toggle
        if "vfocus_enabled" in s:
            self.vfocus_var.set(bool(s["vfocus_enabled"]))
        # Live mode toggle
        if "live_mode" in s:
            self.live_mode_var.set(bool(s["live_mode"]))
        # Auto-clip toggle
        if "clip_auto" in s:
            self.clip_auto_var.set(bool(s["clip_auto"]))
        # Bullet % gate slider
        if "clip_bullet_pct" in s:
            pct = int(s["clip_bullet_pct"])
            self.bullet_pct_var.set(pct)
            self.bullet_pct_label.config(text=f"clip gate: ≥{pct}% bullets")
        if "clip_before_s" in s:
            v = float(s["clip_before_s"])
            self.clip_before_var.set(v)
            self.clip_before_label.config(text=f"clip: {v:g} s before spray")
        if "clip_after_s" in s:
            v = float(s["clip_after_s"])
            self.clip_after_var.set(v)
            self.clip_after_label.config(text=f"clip: {v:g} s after spray")
        if "chain_gap_s" in s:
            v = float(s["chain_gap_s"])
            self.chain_gap_var.set(v)
            self.chain_gap_label.config(text=f"chain gap: {v:g} s (links nearby sprays)")
        # Device (override CLI --device only if no device was passed on the CLI)
        if not self.current_device and s.get("device"):
            saved = s["device"]
            # Handle old format where we stored the full label instead of path
            if not saved.startswith("/dev/"):
                import re
                m = re.search(r'\[(/dev/input/\S+)\]', saved)
                saved = m.group(1) if m else ""
            if saved:
                self.current_device = saved
                self._refresh_devices()

    def _save_settings(self):
        # Snapshot current slider value into the current weapon's slot first
        if hasattr(self, "sens_var"):
            self._sens_per_weapon[self.weapon_var.get()] = self.sens_var.get()
        s = {
            "weapon":          self.weapon_var.get(),
            "sens_per_weapon": dict(self._sens_per_weapon),
            "detrend_enabled": self.detrend_var.get(),
            "detrend_win":     self.detrend_win_var.get(),
            "min_hold_ms":     self.min_hold_var.get(),
            "device":          self._selected_device_path(),
            "vfocus_enabled":  self.vfocus_var.get(),
            "live_mode":       self.live_mode_var.get(),
            "clip_auto":       self.clip_auto_var.get(),
            "clip_bullet_pct":  self.bullet_pct_var.get(),
            "clip_before_s":    self.clip_before_var.get(),
            "clip_after_s":     self.clip_after_var.get(),
            "chain_gap_s":      self.chain_gap_var.get(),
        }
        try:
            with open(self._settings_path, "w") as f:
                json.dump(s, f, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Device management
    # ------------------------------------------------------------------

    def _refresh_devices(self):
        evdev = _try_import_evdev()
        if not evdev:
            self.device_combo["values"] = ["(evdev not installed)"]
            return
        mice = find_mice(evdev)
        vals = [f"{name}  [{path}]" for name, path in mice]
        paths = [path for _, path in mice]
        self.device_combo["values"] = vals
        self._mouse_paths = paths
        if vals and not self.device_var.get():
            self.device_combo.current(0)
        elif vals and self.current_device:
            for i, p in enumerate(paths):
                if p == self.current_device:
                    self.device_combo.current(i)
                    break

    def _selected_device_path(self):
        try:
            idx = list(self.device_combo["values"]).index(self.device_var.get())
            return self._mouse_paths[idx]
        except (ValueError, AttributeError, IndexError):
            v = self.device_var.get()
            if v.startswith("/dev/input/"):
                return v
            if hasattr(self, "_mouse_paths") and self._mouse_paths:
                return self._mouse_paths[0]
            return ""

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _toggle_recording(self):
        if self.recorder and self.recorder.is_alive():
            self.recorder.stop()
            self.rec_btn_text.set("▶  Arm recorder")
            self.rec_indicator.config(text="○  off", fg="#666666")
            self.status_var.set("Recorder disarmed.")
        else:
            path = self._selected_device_path()
            if not path:
                self.status_var.set("No device selected — refresh and pick one.")
                return
            self.recorder = Recorder(path, self.out_dir, self.rec_queue,
                                     min_duration=self.min_hold_var.get() / 1000.0,
                                     gui_hover_fn=lambda: self._mouse_in_gui)
            self.recorder.weapon = self.weapon_var.get()
            self.recorder.start()
            self.rec_btn_text.set("■  Disarm recorder")
            self.status_var.set(f"Armed on {path}  —  hold LMB to record a spray")
            self._start_live_poll()

    # ------------------------------------------------------------------
    # Poll for new sprays
    # ------------------------------------------------------------------

    def _schedule_poll(self):
        self.root.after(self.POLL_MS, self._poll)

    def _poll_hover(self):
        """Track whether the mouse cursor is inside our window every 50 ms."""
        try:
            px = self.root.winfo_pointerx()
            py = self.root.winfo_pointery()
            wx = self.root.winfo_rootx()
            wy = self.root.winfo_rooty()
            ww = self.root.winfo_width()
            wh = self.root.winfo_height()
            self._mouse_in_gui = (wx <= px <= wx + ww and wy <= py <= wy + wh)
        except Exception:
            pass
        self.root.after(50, self._poll_hover)

    def _poll(self):
        new_files = []
        while not self.rec_queue.empty():
            new_files.append(self.rec_queue.get_nowait())

        if new_files:
            self._live_override = None
            self._live_prev_count = 0
            self._live_pattern_cache = None
            new_indices = []
            for f in new_files:
                try:
                    with open(f) as fh:
                        data = json.load(fh)
                    data["_file"] = os.path.basename(f)
                    if data.get("samples"):
                        self.sprays.append(data)
                        new_indices.append(len(self.sprays) - 1)
                except Exception:
                    pass
            if new_indices:
                self._compute_chains()
                self._rebuild_history()
                last_idx = new_indices[-1]
                self.selected_idx = last_idx
                ci = self.spray_chain_id.get(last_idx)
                if ci is not None and ci < len(self.chains):
                    self.selected_chain = list(self.chains[ci])
                else:
                    self.selected_chain = [last_idx]
                self._sync_history_selection()
                self._refresh_plot()
                self._update_clip_btn()
                # Debounced clip trigger: accumulates chain, fires once after chain_gap ms
                if self.clip_auto_var.get():
                    for idx in new_indices:
                        self._schedule_clip(idx)

        # Drain clip notify queue — update history rows when clips land
        while not self._clip_notify_queue.empty():
            fname = self._clip_notify_queue.get_nowait()
            for i, sp in enumerate(self.sprays):
                if sp.get("_file") == fname:
                    # The chain row iid is the last spray index in i's chain
                    ci = self.spray_chain_id.get(i)
                    if ci is not None and ci < len(self.chains):
                        row_iid = str(self.chains[ci][-1])
                    else:
                        row_iid = str(i)
                    if self.history_tree.exists(row_iid):
                        vals = list(self.history_tree.item(row_iid, "values"))
                        if "▶" not in vals[0]:
                            vals[0] = "▶" + vals[0]
                        self.history_tree.item(row_iid, values=tuple(vals))
                    break
            self._update_clip_btn()

        # Update recording indicator
        if self.recorder and self.recorder.is_alive():
            st = self.recorder.status
            if st == "recording":
                n = self.recorder.live_samples
                self.rec_indicator.config(
                    text=f"● recording — {n} samples", fg="#f48771")
            elif st.startswith("error:"):
                self.rec_indicator.config(text=f"✗ {st[6:]}", fg="#f48771")
                self.rec_btn_text.set("▶  Arm recorder")
            else:
                self.rec_indicator.config(text="● armed — hold LMB to spray", fg="#4ec994")
        else:
            self.rec_indicator.config(text="○  off", fg="#666666")

        self._schedule_poll()

    # ------------------------------------------------------------------
    # Live view
    # ------------------------------------------------------------------

    def _on_live_toggle(self):
        self._save_settings()
        if not self.live_mode_var.get():
            self._live_override = None
            self._live_prev_count = 0
            if self.sprays:
                self._refresh_plot()
        else:
            # Turned on while recorder already armed — restart poll if needed
            if self.recorder and self.recorder.is_alive():
                self._start_live_poll()

    def _start_live_poll(self):
        if not self._live_poll_running:
            self._live_poll_running = True
            self.root.after(self.LIVE_POLL_MS, self._live_poll)

    def _live_poll(self):
        if not self.recorder or not self.recorder.is_alive():
            self._live_poll_running = False
            self._live_override = None
            self._live_prev_count = 0
            return  # recorder gone — stop the loop

        if self.live_mode_var.get():
            snap = self.recorder.get_live_snapshot()

            if snap is not None:
                samples = snap["samples"]
                elapsed = samples[-1]["t"] if samples else 0.0
                min_sec = self.min_hold_var.get() / 1000.0
                n = len(samples)
                if elapsed >= min_sec and n > self._live_prev_count:
                    self._live_prev_count = n
                    self._live_override = {
                        "_file": "[live]",
                        "weapon": snap["weapon"],
                        "samples": samples,
                        "duration": elapsed,
                        "n_samples": n,
                    }
                    if self._replay_active:
                        self._stop_replay()
                    self._refresh_live_traj()
            else:
                if self._live_override is not None:
                    self._live_override = None
                    self._live_prev_count = 0

        self.root.after(self.LIVE_POLL_MS, self._live_poll)

    # ------------------------------------------------------------------
    # History list
    # ------------------------------------------------------------------

    def _load_existing_sprays(self):
        self.sprays = load_sprays(self.out_dir)
        self._compute_chains()
        self._rebuild_history()
        if self.sprays:
            last_idx = len(self.sprays) - 1
            self.selected_idx = last_idx
            ci = self.spray_chain_id.get(last_idx)
            if ci is not None and ci < len(self.chains):
                self.selected_chain = list(self.chains[ci])
            else:
                self.selected_chain = [last_idx]
            self._sync_history_selection()
            self._refresh_plot()

    _WEP_ABBREV = {
        "AK-47": "AK-47", "M4A4": "M4A4", "M4A1-S": "M4A1-S",
        "Galil AR": "Galil", "FAMAS": "FAMAS", "None": "",
    }

    def _add_history_row(self, idx, spray):
        dur_ms = int(spray.get("duration", 0) * 1000)
        smp = spray.get("n_samples", 0)
        ny = spray.get("net_dy", 0)
        wep = self._WEP_ABBREV.get(spray.get("weapon", ""), spray.get("weapon", "")[:6])
        if spray.get("clip"):
            wep = "▶" + wep
        iid = str(idx)
        if self.history_tree.exists(iid):
            return
        self.history_tree.insert("", "end", iid=iid,
                                 values=(wep, f"{dur_ms}", f"{smp}", f"{ny:+d}"))
        self.history_tree.see(iid)

    def _compute_chains(self):
        """Group sprays into chains where consecutive gap ≤ chain_gap_var."""
        if not self.sprays:
            self.chains = []
            self.spray_chain_id = {}
            return
        gap_var = getattr(self, "chain_gap_var", None)
        threshold = gap_var.get() if gap_var else 1.0
        chains = []
        current = [0]
        for i in range(1, len(self.sprays)):
            prev = self.sprays[i - 1]
            curr = self.sprays[i]
            prev_end = prev.get("start_time", 0) + prev.get("duration", 0)
            curr_start = curr.get("start_time", 0)
            gap = curr_start - prev_end
            if 0 <= gap <= threshold:
                current.append(i)
            else:
                chains.append(current)
                current = [i]
        chains.append(current)
        self.chains = chains
        self.spray_chain_id = {}
        for ci, chain in enumerate(chains):
            for si in chain:
                self.spray_chain_id[si] = ci

    def _add_chain_row(self, chain_idx):
        """Insert one treeview row for a chain. iid = str(last spray index)."""
        chain = self.chains[chain_idx]
        last_idx = chain[-1]
        iid = str(last_idx)
        if self.history_tree.exists(iid):
            return
        chain_sprays = [self.sprays[i] for i in chain if i < len(self.sprays)]
        total_dur_ms = int(sum(sp.get("duration", 0) for sp in chain_sprays) * 1000)
        total_smp = sum(sp.get("n_samples", 0) for sp in chain_sprays)
        total_ny = sum(sp.get("net_dy", 0) for sp in chain_sprays)
        last_sp = chain_sprays[-1]
        wep = self._WEP_ABBREV.get(last_sp.get("weapon", ""), last_sp.get("weapon", "")[:6])
        if any(sp.get("clip") for sp in chain_sprays):
            wep = "▶" + wep
        if len(chain) > 1:
            wep = f"⛓{len(chain)} " + wep
        self.history_tree.insert("", "end", iid=iid,
                                 values=(wep, f"{total_dur_ms}", f"{total_smp}", f"{int(total_ny):+d}"))
        self.history_tree.see(iid)

    def _rebuild_history(self):
        """Rebuild the treeview from scratch with one row per chain."""
        self.history_tree.delete(*self.history_tree.get_children())
        start = max(0, len(self.chains) - self.MAX_HISTORY)
        for ci in range(start, len(self.chains)):
            self._add_chain_row(ci)
        if self.selected_idx >= 0:
            self._sync_history_selection()

    def _schedule_clip(self, spray_idx):
        """Debounce clip trigger: wait chain_gap ms after last new spray, then fire once."""
        if self._pending_clip_timer is not None:
            self.root.after_cancel(self._pending_clip_timer)
        self._pending_clip_chain.append(spray_idx)
        gap_var = getattr(self, "chain_gap_var", None)
        gap_ms = int((gap_var.get() if gap_var else 1.0) * 1000) + 200
        self._pending_clip_timer = self.root.after(gap_ms, self._fire_clip_chain)

    def _fire_clip_chain(self):
        """Fire the clip save for the accumulated chain of sprays."""
        self._pending_clip_timer = None
        chain_indices = list(self._pending_clip_chain)
        self._pending_clip_chain.clear()
        if not chain_indices or not self.clip_auto_var.get():
            return
        # Sum bullets across all sprays (uncapped per spray so full-chain count is used)
        total_shots = 0.0
        total_mag = 0
        combined_dur = 0.0
        for idx in chain_indices:
            if idx >= len(self.sprays):
                continue
            sp = self.sprays[idx]
            wdata = WEAPON_DATA.get(sp.get("weapon", "None"))
            if wdata:
                total_shots += sp.get("duration", 0) * wdata["rpm"] / 60
                total_mag = max(total_mag, wdata["mag"])
            combined_dur += sp.get("duration", 0)
        gate = getattr(self, "bullet_pct_var", None)
        threshold = (gate.get() / 100.0) if gate else 0.4
        if total_mag == 0 or total_shots < threshold * total_mag:
            return
        last_idx = chain_indices[-1]
        if last_idx >= len(self.sprays):
            return
        combined_spray = dict(self.sprays[last_idx])
        combined_spray["duration"] = combined_dur
        self._do_save_clip(combined_spray)

    def _do_save_clip(self, spray):
        """Trigger GSR replay save after the configured delay. No bullet gate check here."""
        import glob, signal as _sig
        replay_dir, pids = self._find_gsr_replay_dir()
        if not replay_dir or not pids:
            return
        before = set(glob.glob(os.path.join(replay_dir, "*.mp4")))
        buf_after = getattr(self, "clip_after_var", None)
        delay_s = buf_after.get() if buf_after else 3.0
        self.status_var.set(f"Clip in {delay_s:g} s…")

        def _delayed_save():
            buf_after = getattr(self, "clip_after_var", None)
            time.sleep(buf_after.get() if buf_after else 3.0)
            sig_time = time.time()
            for pid in pids:
                try:
                    os.kill(pid, _sig.SIGRTMIN + 1)
                except Exception:
                    pass
            self.root.after(0, lambda: self.status_var.set("Saving clip…"))
            # Kill gsr-notify in a loop for 2 s to catch late-spawning instances
            def _suppress_notify():
                for _ in range(20):
                    time.sleep(0.1)
                    try:
                        subprocess.run(["pkill", "-f", "gsr-notify"], capture_output=True)
                    except Exception:
                        pass
            threading.Thread(target=_suppress_notify, daemon=True).start()
            self._clip_worker(spray, replay_dir, before, sig_time)

        threading.Thread(target=_delayed_save, daemon=True).start()

    def _sync_history_selection(self):
        iid = str(self.selected_idx)
        if self.history_tree.exists(iid):
            self.history_tree.selection_set(iid)
            self.history_tree.see(iid)

    def _on_history_select(self, _event=None):
        sel = self.history_tree.selection()
        if sel:
            if self._replay_active:
                self._stop_replay()
            last_spray_idx = int(sel[0])
            ci = self.spray_chain_id.get(last_spray_idx)
            if ci is not None and ci < len(self.chains):
                self.selected_chain = list(self.chains[ci])
            else:
                self.selected_chain = [last_spray_idx]
            self.selected_idx = last_spray_idx
            spray = self.sprays[self.selected_idx]
            wep = spray.get("weapon", "None") or "None"
            if wep in WEAPON_DATA:
                self._select_weapon(wep, refresh=False, retag=False)
            self._refresh_plot()
            self._update_clip_btn()

    def _select_weapon(self, name, refresh=True, retag=True):
        # Save old weapon's sensitivity, load new weapon's
        if hasattr(self, "sens_var"):
            self._sens_per_weapon[self.weapon_var.get()] = self.sens_var.get()
            self.sens_var.set(self._sens_per_weapon.get(name, 1.0))
            self.sens_label.config(text=f"{self.sens_var.get():.2f}")
        if self.recorder and self.recorder.is_alive():
            self.recorder.weapon = name

        fg_sel = {
            "None":     "#cccccc",
            "AK-47":   "#ff8070",
            "M4A4":    "#7bbde8",
            "M4A1-S":  "#6de0a8",
            "Galil AR": "#ffaa50",
            "FAMAS":   "#cc99ff",
        }
        bg_sel = {
            "None":     "#383838",
            "AK-47":   "#52150e",
            "M4A4":    "#0e2f48",
            "M4A1-S":  "#0b3320",
            "Galil AR": "#422800",
            "FAMAS":   "#301248",
        }
        tk = self.tk
        self.weapon_var.set(name)          # weapon_var must be set BEFORE _save_settings
        for n, btn in self._weapon_btns.items():
            if n == name:
                btn.config(bg=bg_sel.get(n, "#333"),
                           fg=fg_sel.get(n, "#ddd"),
                           relief=tk.GROOVE)
            else:
                btn.config(bg="#222222", fg="#555555", relief=tk.FLAT)
        if hasattr(self, "sens_var"):
            self._save_settings()
        if refresh:
            if retag:
                self._retag_selected_spray(name)
            self._refresh_plot()

    def _retag_selected_spray(self, weapon_name):
        """Update the currently viewed spray's weapon on disk and in the history table."""
        if not self.sprays:
            return
        idx = max(0, min(self.selected_idx, len(self.sprays) - 1))
        spray = self.sprays[idx]
        spray["weapon"] = weapon_name
        # Overwrite the JSON file
        fpath = os.path.join(self.out_dir, spray.get("_file", ""))
        if fpath and os.path.exists(fpath):
            try:
                with open(fpath) as f:
                    data = json.load(f)
                data["weapon"] = weapon_name
                with open(fpath, "w") as f:
                    json.dump(data, f)
            except Exception:
                pass
        # Refresh the history row in-place
        iid = str(idx)
        if self.history_tree.exists(iid):
            abbrev = self._WEP_ABBREV.get(weapon_name, weapon_name[:6])
            vals = self.history_tree.item(iid, "values")
            self.history_tree.item(iid, values=(abbrev,) + tuple(vals[1:]))

    def _clear_selected(self):
        sel = self.history_tree.selection()
        for iid in sel:
            self.history_tree.delete(iid)

    def _clear_all(self):
        self.history_tree.delete(*self.history_tree.get_children())

    def _on_sens_change(self, _=None):
        v = self.sens_var.get()
        self.sens_label.config(text=f"{v:.2f}")
        self._sens_per_weapon[self.weapon_var.get()] = v
        self._save_settings()
        self._refresh_plot()

    def _update_sens_label(self, *_):
        self.sens_label.config(text=f"{self.sens_var.get():.2f}")

    def _on_min_hold_change(self, _=None):
        ms = self.min_hold_var.get()
        self.min_hold_label.config(text=f"min hold: {ms} ms")
        if self.recorder and self.recorder.is_alive():
            self.recorder.min_duration = ms / 1000.0
        self._save_settings()

    def _on_bullet_pct_change(self):
        pct = self.bullet_pct_var.get()
        self.bullet_pct_label.config(text=f"clip gate: ≥{pct}% bullets")
        self._save_settings()

    def _on_clip_buf_change(self):
        before = self.clip_before_var.get()
        after  = self.clip_after_var.get()
        self.clip_before_label.config(text=f"clip: {before:g} s before spray")
        self.clip_after_label.config(text=f"clip: {after:g} s after spray")
        self._save_settings()

    def _on_chain_gap_change(self):
        gap = self.chain_gap_var.get()
        self.chain_gap_label.config(text=f"chain gap: {gap:g} s (links nearby sprays)")
        self._compute_chains()
        self._rebuild_history()
        if self.sprays:
            self._refresh_plot()
        self._save_settings()

    def _on_detrend_toggle(self):
        self._save_settings()
        self._refresh_plot()

    def _on_detrend_slide(self):
        ms = self.detrend_win_var.get()
        self.detrend_win_label.config(text=f"window: {ms} ms")
        self._save_settings()
        if self.detrend_var.get():
            self._refresh_plot()

    def _on_vfocus_toggle(self):
        self._save_settings()
        self._refresh_plot()

    def _on_replay_speed_change(self, _=None):
        v = self.replay_speed_var.get()
        self.replay_speed_label.config(text=f"speed: {v:.1f}×")

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    REPLAY_TICK_MS = 60   # ~16 fps render cadence

    def _toggle_replay(self):
        if self._replay_active:
            self._stop_replay()
        else:
            self._start_replay()

    def _start_replay(self):
        if not self.sprays:
            return
        idx = max(0, min(self.selected_idx, len(self.sprays) - 1))
        sel = self.sprays[idx]
        np = self.np

        t, x, y = cumulative_xy(sel)
        if self.detrend_var.get():
            x, y = self._detrend(t, x, y)

        wdata = WEAPON_DATA.get(self.weapon_var.get())
        sensitivity = self.sens_var.get()
        fire_t, fire_x, fire_y = self._bullet_positions(t, x, y, wdata)
        if not fire_t:
            return

        t_arr = np.array(t)
        x_arr = np.array(x, dtype=float)
        y_arr = np.array(y, dtype=float)

        it, ix, iy = [], [], []
        if wdata:
            it, ix, iy = pattern_to_counts(wdata, sensitivity, 0.022,
                                           max_duration=t[-1] if t else None)

        # Deviation of each bullet from its own ideal landing position.
        # dev[i] = (0,0) means perfect; spread shows compensation error.
        if wdata and it:
            dev_x = [fire_x[i] - float(np.interp(fire_t[i], it, ix))
                     for i in range(len(fire_t))]
            dev_y = [fire_y[i] - float(np.interp(fire_t[i], it, iy))
                     for i in range(len(fire_t))]
            path_x = x_arr - np.interp(t_arr, it, ix)
            path_y = y_arr - np.interp(t_arr, it, iy)
        else:
            dev_x, dev_y = list(fire_x), list(fire_y)
            path_x, path_y = x_arr, y_arr

        # Normal view: symmetric, locked to weapon pattern horizontal width
        if wdata and ix:
            r = max(abs(v) for v in ix) * 1.45
        else:
            r = max((max(abs(v) for v in dev_x + dev_y) if dev_x else 100), 50) * 1.5

        # Vertical-focus view: zoom to actual deviation spread
        all_dev = [abs(v) for v in dev_x + dev_y] if dev_x else [50]
        tight_r = max(max(all_dev) * 1.6, 15)

        end_t = max(t[-1] if len(t) else 0, fire_t[-1] if fire_t else 0) + 0.3

        self._replay_data = {
            't': t_arr, 'path_x': path_x, 'path_y': path_y,
            'fire_t': fire_t, 'dev_x': dev_x, 'dev_y': dev_y,
            'wdata': wdata, 'end_t': end_t,
            'r': r, 'tight_r': tight_r,
        }
        self._replay_sim_t = 0.0
        self._replay_active = True
        self._replay_btn_text.set("■  Stop")
        self._do_replay_frame()

    def _stop_replay(self):
        self._replay_active = False
        if self._replay_after_id is not None:
            self.root.after_cancel(self._replay_after_id)
            self._replay_after_id = None
        self._replay_btn_text.set("▶  Play")
        self._refresh_plot()

    def _do_replay_frame(self):
        if not self._replay_active:
            return
        d = self._replay_data
        self._draw_replay_frame(self._replay_sim_t, d)

        speed = max(self.replay_speed_var.get(), 0.01)
        self._replay_sim_t += self.REPLAY_TICK_MS / 1000.0 * speed

        if self._replay_sim_t > d['end_t']:
            # Show final frame then stop
            self._replay_active = False
            self._replay_btn_text.set("▶  Play")
            return

        self._replay_after_id = self.root.after(self.REPLAY_TICK_MS, self._do_replay_frame)

    def _draw_replay_frame(self, sim_t, d):
        np = self.np
        ax = self.ax_traj
        ax.cla()
        ax.set_facecolor("#252526")
        ax.tick_params(colors="#9d9d9d", labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor("#3c3c3c")

        t_arr   = d['t']
        path_x  = d['path_x']
        path_y  = d['path_y']
        fire_t  = d['fire_t']
        dev_x   = d['dev_x']
        dev_y   = d['dev_y']
        wdata   = d['wdata']
        n_total = len(fire_t)

        fired   = [i for i, ft in enumerate(fire_t) if ft <= sim_t]
        n_fired = len(fired)

        # ── Target = (0,0): where each bullet should land per the ideal ───
        ax.plot(0, 0, "+", color="#4ec994", ms=22, mew=2.5, zorder=12)

        # ── Deviation path: crosshair error vs ideal over time ────────────
        mask = t_arr <= sim_t
        px, py = path_x[mask], path_y[mask]
        if len(px) >= 2:
            ax.plot(px, py, color="#569cd6", lw=2, zorder=3, alpha=0.7)
        if len(px):
            ax.plot(px[-1], py[-1], "o", color="#569cd6", ms=7,
                    markeredgecolor="white", markeredgewidth=0.8, zorder=9)

        # ── Bullet deviation dots (actual - ideal per shot) ───────────────
        if fired:
            last = fired[-1]
            if len(fired) > 1:
                ax.scatter([dev_x[i] for i in fired[:-1]],
                           [dev_y[i] for i in fired[:-1]],
                           color="white", s=22, zorder=7,
                           edgecolors="#555", linewidths=0.4, alpha=0.65)
            ax.scatter([dev_x[last]], [dev_y[last]], color="white", s=65,
                       zorder=11, edgecolors="#4ec994", linewidths=2.0)
            ax.text(dev_x[last] - 9, dev_y[last], str(last + 1),
                    color="#cccccc", fontsize=7, va="center", ha="right", zorder=12)

        title = f"Shot {n_fired} / {n_total}  — error vs ideal" if n_fired else "Ready…"
        ax.set_title(title, color="#d4d4d4", fontsize=9)
        ax.set_xlabel("horizontal error (counts)", fontsize=7)
        ax.set_ylabel("vertical error (counts)", fontsize=7)
        r = d['r']
        ax.invert_yaxis()
        ax.axhline(0, color="#555", lw=0.8)
        ax.axvline(0, color="#555", lw=0.8)
        ax.set_xlim(-r, r)
        ax.set_ylim(-r, r)

        self.canvas.draw_idle()

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------

    def _bullet_positions(self, t, x, y, wdata):
        """Interpolate crosshair position at each bullet's fire time.
        Returns (fire_times, fire_x, fire_y) — where each shot actually lands, no spread."""
        np = self.np
        if not wdata or len(t) < 2:
            return [], [], []
        interval = 60.0 / wdata["rpm"]
        duration = t[-1]
        n = min(wdata["mag"], max(1, int(duration / interval) + 1))
        fire_t = [i * interval for i in range(n)]
        # t=0 → position (0,0) since spray begins at LMB press before first evdev event
        t_full = [0.0] + list(t)
        x_full = [0.0] + list(x)
        y_full = [0.0] + list(y)
        fire_x = np.interp(fire_t, t_full, x_full).tolist()
        fire_y = np.interp(fire_t, t_full, y_full).tolist()
        return fire_t, fire_x, fire_y

    def _detrend(self, t, x, y):
        """Remove tracking drift via Gaussian smoothing (handles non-linear tracking)."""
        np = self.np
        if len(t) < 4:
            return x, y
        sigma_s = self.detrend_win_var.get() / 1000.0   # ms → s
        t_arr = np.array(t)
        x_arr = np.array(x, dtype=float)
        y_arr = np.array(y, dtype=float)
        dt = float(np.median(np.diff(t_arr)))
        if dt <= 0:
            return x, y
        sigma_n = max(1.5, sigma_s / dt)                # sigma in samples
        r = int(3 * sigma_n)
        k = np.arange(-r, r + 1, dtype=float)
        kernel = np.exp(-k ** 2 / (2 * sigma_n ** 2))
        kernel /= kernel.sum()
        x_trend = np.convolve(np.pad(x_arr, r, mode='reflect'), kernel, mode='valid')
        y_trend = np.convolve(np.pad(y_arr, r, mode='reflect'), kernel, mode='valid')
        return (x_arr - x_trend).tolist(), (y_arr - y_trend).tolist()

    def _refresh_live_traj(self):
        """Fast live-mode redraw: only clears ax_traj, caches weapon pattern."""
        np = self.np
        snap = self._live_override
        if snap is None:
            return

        t, x, y = cumulative_xy(snap)
        weapon_name = self.weapon_var.get()
        wdata = WEAPON_DATA.get(weapon_name)
        sensitivity = self.sens_var.get()
        n = len(t)

        ax = self.ax_traj
        ax.cla()
        ax.set_facecolor("#252526")
        ax.tick_params(colors="#9d9d9d", labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor("#3c3c3c")

        if n >= 2:
            pts = np.array([x, y]).T.reshape(-1, 1, 2)
            segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
            lc = self.LineCollection(segs, cmap="viridis", linewidth=2.5, zorder=3)
            lc.set_array(np.array(t))
            ax.add_collection(lc)
            self._colorbar.update_normal(lc)
        if x:
            ax.plot(x[0], y[0], "go", ms=9, zorder=5, label="start")
            ax.plot(x[-1], y[-1], "rs", ms=8, zorder=5, label="now")

        if wdata:
            # Cache full pattern — recompute only when weapon or sensitivity changes
            cache_key = (weapon_name, round(sensitivity, 3))
            if (self._live_pattern_cache is None
                    or self._live_pattern_cache[0] != cache_key):
                it_c, ix_c, iy_c = pattern_to_counts(wdata, sensitivity, 0.022)
                self._live_pattern_cache = (cache_key, it_c, ix_c, iy_c)
            _, it, ix, iy = self._live_pattern_cache

            # Show ideal only up to current spray duration
            cur_dur = t[-1] if t else 0.0
            end = next((i for i, tv in enumerate(it) if tv > cur_dur), len(it))
            if end:
                ax.plot(ix[:end], iy[:end], "--", color=wdata["color"],
                        lw=1.5, zorder=4, alpha=0.7, label=f"ideal {weapon_name}")
                ax.scatter(ix[:end], iy[:end], color=wdata["color"],
                           s=18, zorder=6, edgecolors="white",
                           linewidths=0.4, alpha=0.85)

        dur_ms = (t[-1] if t else 0.0) * 1000
        ax.set_title(f"[LIVE]  {n} samples   {dur_ms:.0f} ms",
                     color="#4ec994", fontsize=9)
        ax.set_xlabel("horizontal (counts)", fontsize=7)
        ax.set_ylabel("vertical (counts)", fontsize=7)
        ax.invert_yaxis()
        ax.axhline(0, color="#444", lw=0.5)
        ax.axvline(0, color="#444", lw=0.5)
        ax.set_aspect("equal", adjustable="datalim")
        ax.margins(0.12)
        if x:
            ax.legend(fontsize=7, loc="best", facecolor="#2d2d2d",
                      edgecolor="#3c3c3c", labelcolor="#d4d4d4")

        self.canvas.draw_idle()
        self.status_var.set(
            f"[LIVE]  {n} samples   {dur_ms:.0f} ms"
            + (f"   |   {weapon_name}  sens {sensitivity:.1f}" if wdata else ""))

    def _refresh_plot(self):
        if self._live_override:
            self._refresh_live_traj()
            return

        np = self.np
        sprays = self.sprays
        if not sprays:
            return

        idx = max(0, min(self.selected_idx, len(sprays) - 1))
        sel = sprays[idx]

        # Resolve chain: collect all sprays to plot together
        chain_indices = [i for i in getattr(self, "selected_chain", [idx])
                         if i < len(sprays)]
        if not chain_indices:
            chain_indices = [idx]
        chain_sprays = [sprays[i] for i in chain_indices]
        combined_dur = sum(sp.get("duration", 0) for sp in chain_sprays)
        combined_smp = sum(sp.get("n_samples", 0) for sp in chain_sprays)
        is_chain = len(chain_sprays) > 1

        weapon_name = self.weapon_var.get()
        wdata = WEAPON_DATA.get(weapon_name)
        sensitivity = self.sens_var.get()
        m_yaw = 0.022
        do_detrend = self.detrend_var.get()

        fig = self.fig
        dt_label = "  [tracking removed]" if do_detrend else ""

        ax_traj = self.ax_traj
        ax_all  = self.ax_all

        for ax in (ax_traj, ax_all):
            ax.cla()
            ax.set_facecolor("#252526")
            ax.tick_params(colors="#9d9d9d", labelsize=7)
            for spine in ax.spines.values():
                spine.set_edgecolor("#3c3c3c")

        if is_chain:
            t, x, y = chain_cumulative_xy(chain_sprays)
        else:
            t, x, y = cumulative_xy(sel)
        if do_detrend:
            x, y = self._detrend(t, x, y)

        # Bullet positions must be computed before suptitle (need n_shots for label)
        fire_t, fire_x, fire_y = self._bullet_positions(t, x, y, wdata)
        n_shots = len(fire_t)

        title_prefix = (f"⛓{len(chain_sprays)}-spray chain" if is_chain
                        else sel["_file"])
        fig.suptitle(
            f"{title_prefix}   {combined_dur*1000:.0f} ms   {combined_smp} samples"
            + (f"   |   {weapon_name}  sens {sensitivity:.1f}"
               + (f"   —  {n_shots} shots" if n_shots else "")
               if wdata else "")
            + dt_label,
            color="#d4d4d4", fontsize=9,
        )

        # ----------------------------------------------------------------
        # LEFT: trajectory of selected spray vs ideal
        # ----------------------------------------------------------------
        vfocus = self.vfocus_var.get() and bool(wdata)

        if vfocus:
            it, ix, iy = pattern_to_counts(wdata, sensitivity, m_yaw,
                                           max_duration=combined_dur)
            t_arr = np.array(t)
            ix_arr = np.array(ix); iy_arr = np.array(iy); it_arr = np.array(it)
            x_dev = (np.array(x) - np.interp(t_arr, it_arr, ix_arr)).tolist()
            y_dev = (np.array(y) - np.interp(t_arr, it_arr, iy_arr)).tolist()
            fire_dx = [fire_x[i] - ix[i] for i in range(len(fire_t))]
            fire_dy = [fire_y[i] - iy[i] for i in range(len(fire_t))]

            if len(t) >= 2:
                pts = np.array([x_dev, y_dev]).T.reshape(-1, 1, 2)
                segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
                lc = self.LineCollection(segs, cmap="viridis", linewidth=2.5, zorder=3)
                lc.set_array(t_arr)
                ax_traj.add_collection(lc)
                self._colorbar.update_normal(lc)

            if fire_dx:
                ax_traj.scatter(fire_dx, fire_dy, color="white", s=18, zorder=8,
                                edgecolors="#555555", linewidths=0.4, alpha=0.92,
                                label=f"deviation ({n_shots})")
                for i, (px, py) in enumerate(zip(fire_dx, fire_dy)):
                    if i % 5 == 0 or i == n_shots - 1:
                        ax_traj.text(px - 7, py, str(i + 1), color="#cccccc",
                                     fontsize=6, va="center", ha="right", zorder=9)

            ax_traj.plot(0, 0, "+", color="#e05c5c", ms=14, mew=2, zorder=10,
                         label="ideal (origin)")

            all_vals = fire_dx + fire_dy + x_dev + y_dev
            tight_r = max(max(abs(v) for v in all_vals) * 1.15 if all_vals else 50, 15)
            title = "Deviation from ideal"
            if n_shots:
                title += f"   ({n_shots} shots)"
            if do_detrend:
                title += "  [tracking removed]"
            ax_traj.set_title(title, color="#d4d4d4", fontsize=9)
            ax_traj.set_xlabel("horizontal error (counts)", fontsize=7)
            ax_traj.set_ylabel("vertical error (counts)", fontsize=7)
            ax_traj.invert_yaxis()
            ax_traj.axhline(0, color="#444", lw=0.5)
            ax_traj.axvline(0, color="#444", lw=0.5)
            ax_traj.set_xlim(-tight_r, tight_r)
            ax_traj.set_ylim(-tight_r, tight_r)
            ax_traj.legend(fontsize=7, loc="best", facecolor="#2d2d2d",
                           edgecolor="#3c3c3c", labelcolor="#d4d4d4")
        else:
            if len(t) >= 2:
                pts = np.array([x, y]).T.reshape(-1, 1, 2)
                segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
                lc = self.LineCollection(segs, cmap="viridis", linewidth=2.5, zorder=3)
                lc.set_array(np.array(t))
                ax_traj.add_collection(lc)
                self._colorbar.update_normal(lc)
            if x:
                ax_traj.plot(x[0], y[0], "go", ms=9, zorder=5, label="start")
                ax_traj.plot(x[-1], y[-1], "rs", ms=8, zorder=5, label="end")

            if wdata:
                it, ix, iy = pattern_to_counts(wdata, sensitivity, m_yaw,
                                               max_duration=combined_dur)
                ax_traj.plot(ix, iy, "--", color=wdata["color"], lw=1.5, zorder=4,
                             alpha=0.7, label=f"ideal {weapon_name}")
                ax_traj.scatter(ix, iy, color=wdata["color"], s=22, zorder=6,
                                edgecolors="white", linewidths=0.5, alpha=0.9)
                for bi, (ibx, iby) in enumerate(zip(ix, iy)):
                    if bi > 0 and bi % 5 == 0:
                        ax_traj.text(ibx + 5, iby, str(bi), color=wdata["color"],
                                     fontsize=6, va="center", zorder=7)

            if fire_t:
                ax_traj.scatter(fire_x, fire_y, color="white", s=18, zorder=8,
                                edgecolors="#555555", linewidths=0.4, alpha=0.92,
                                label=f"your shots ({n_shots})")
                for i, (px, py) in enumerate(zip(fire_x, fire_y)):
                    if i % 5 == 0 or i == n_shots - 1:
                        ax_traj.text(px - 7, py, str(i + 1), color="#cccccc",
                                     fontsize=6, va="center", ha="right", zorder=9)

            title = "Your spray vs ideal"
            if n_shots:
                title += f"   ({n_shots} shots)"
            if do_detrend:
                title += "  [tracking removed]"
            ax_traj.set_title(title, color="#d4d4d4", fontsize=9)
            ax_traj.set_xlabel("horizontal (counts)", fontsize=7)
            ax_traj.set_ylabel("vertical (counts)", fontsize=7)
            ax_traj.invert_yaxis()
            ax_traj.axhline(0, color="#444", lw=0.5)
            ax_traj.axvline(0, color="#444", lw=0.5)
            ax_traj.legend(fontsize=7, loc="best", facecolor="#2d2d2d",
                           edgecolor="#3c3c3c", labelcolor="#d4d4d4")
            ax_traj.set_aspect("equal", adjustable="datalim")
            ax_traj.margins(0.12)

        # ----------------------------------------------------------------
        # TOP-RIGHT: all sprays overlaid (capped at 50 for performance)
        # ----------------------------------------------------------------
        ALL_CAP = 50
        visible = sprays[-ALL_CAP:] if len(sprays) > ALL_CAP else sprays
        if sel not in visible:
            visible = list(visible) + [sel]
        for sp in visible:
            tt, xx, yy = cumulative_xy(sp)
            if do_detrend:
                xx, yy = self._detrend(tt, xx, yy)
            is_sel = sp is sel
            ax_all.plot(xx, yy,
                        color="#e05c5c" if is_sel else "#4080b0",
                        alpha=0.9 if is_sel else 0.22,
                        lw=2 if is_sel else 0.8, zorder=3 if is_sel else 1)
        if wdata:
            max_dur = max((sp.get("duration") or 0) for sp in visible)
            it, ix, iy = pattern_to_counts(wdata, sensitivity, m_yaw,
                                           max_duration=max_dur)
            ax_all.plot(ix, iy, "--", color=wdata["color"], lw=2, zorder=4,
                        alpha=0.8, label=f"ideal {weapon_name}")
            ax_all.scatter(ix, iy, color=wdata["color"], s=12, zorder=6,
                           edgecolors="white", linewidths=0.3, alpha=0.85)
            ax_all.legend(fontsize=7, facecolor="#2d2d2d",
                          edgecolor="#3c3c3c", labelcolor="#d4d4d4")
        ax_all.plot(0, 0, "go", ms=6, zorder=5)
        title_all = f"All sprays  ({len(sprays)})"
        if len(sprays) > ALL_CAP:
            title_all += f"  [showing last {ALL_CAP}]"
        ax_all.set_title(title_all, color="#d4d4d4", fontsize=8)
        ax_all.set_xlabel("horizontal", fontsize=7)
        ax_all.set_ylabel("vertical", fontsize=7)
        ax_all.invert_yaxis()
        ax_all.axhline(0, color="#444", lw=0.5)
        ax_all.axvline(0, color="#444", lw=0.5)
        if wdata:
            x_half = max(abs(v) for v in ix) if ix else 200
            y_bot  = max(iy) if iy else 200
            pad_x  = x_half * 0.45
            pad_y  = y_bot  * 0.15
            ax_all.set_xlim(-x_half - pad_x, x_half + pad_x)
            ax_all.set_ylim(-pad_y, y_bot + pad_y)
        else:
            ax_all.set_aspect("equal", adjustable="datalim")
            ax_all.margins(0.1)

        self.canvas.draw_idle()

        # Status bar
        net_y = sel.get("net_dy", 0)
        status = f"Spray {idx+1}/{len(sprays)}   {sel.get('duration',0)*1000:.0f} ms   net-Y {net_y:+d}"
        if wdata:
            status += f"   |   {weapon_name}  sens {sensitivity:.1f}"
        if do_detrend:
            status += "   [tracking removed]"
        self.status_var.set(status)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _check_deps():
    """Exit with a clear message if any required package is missing."""
    missing_mod = []
    missing_pip = []
    checks = [
        ("numpy",      "numpy"),
        ("matplotlib", "matplotlib"),
        ("tkinter",    None),       # stdlib, can't pip-install
        ("evdev",      "evdev"),
    ]
    for mod, pip_name in checks:
        try:
            __import__(mod)
        except ImportError:
            missing_mod.append(mod)
            if pip_name:
                missing_pip.append(pip_name)

    if missing_mod:
        msg = f"Missing packages: {', '.join(missing_mod)}\n\n"
        if missing_pip:
            msg += f"Install with pip (use a venv):\n    .venv/bin/pip install {' '.join(missing_pip)}\n\n"
            msg += "Or run the setup script first:\n    bash setup.sh\n    .venv/bin/python3 spray_gui.py\n"
        if "tkinter" in missing_mod:
            msg += "\ntkinter is missing — install with:\n    sudo pacman -S tk\n"
        sys.exit(msg)


def main():
    os.nice(10)   # yield CPU to CS2 when both compete
    _check_deps()
    import tkinter as tk

    ap = argparse.ArgumentParser(description="CS2 spray tracker GUI.")
    ap.add_argument("--out", default="sprays",
                    help="directory for spray JSON files (default: ./sprays)")
    ap.add_argument("--device", help="mouse device path, e.g. /dev/input/event3")
    args = ap.parse_args()

    out_dir = os.path.expanduser(args.out)

    root = tk.Tk()
    root.geometry("1100x720")
    root.minsize(800, 560)

    app = SprayApp(root, out_dir, initial_device=args.device)   # noqa: F841

    root.mainloop()


if __name__ == "__main__":
    main()
