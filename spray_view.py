#!/usr/bin/env python3
"""
spray_view.py — visualize recorded CS2 sprays with optional weapon overlay.

Four panels:

  1. Trajectory of one spray, colored by time   — the "shape" of your control
  2. All sprays overlaid, aligned at start       — consistency at a glance
  3. dx / dy over time (selected spray)          — when / which axis you moved
  4. Cumulative pull vs ideal weapon pattern     — how close you are to perfect

With --weapon, a dashed line shows where the ideal mouse trajectory would be
for perfectly controlling that weapon's recoil. The gap between your line and
the ideal is your error. Below the figure you get a consistency score
(how tight your sprays are relative to each other) and, if a weapon is given,
an accuracy score (how close your average spray is to the ideal pattern).

Weapon data is cumulative recoil angles in degrees (pitch = up, yaw = right).
The conversion to raw mouse counts is:  counts = degrees / (m_yaw * sensitivity)
where m_yaw defaults to 0.022 (CS2 default). This is DPI-independent because
both evdev and CS2 see the same raw counts.

Usage:
    python3 spray_view.py
    python3 spray_view.py --weapon ak47 --sensitivity 1.5
    python3 spray_view.py --weapon m4a4 --sensitivity 2.0 --last 20
    python3 spray_view.py --weapon m4a1s --save out.png
    python3 spray_view.py --list
    python3 spray_view.py --dir ~/cs2-sprays
"""

import argparse
import glob
import json
import os
import sys

# ---------------------------------------------------------------------------
# CS2 weapon recoil patterns
# ---------------------------------------------------------------------------
# Cumulative recoil from the first bullet, in degrees.
# Index 0 is always (0.0, 0.0) — the first shot before spray deviation builds.
# (cumulative_pitch_up, cumulative_yaw_right)
#   pitch > 0 = crosshair rises  → compensate by pulling mouse DOWN  (+dy in plot)
#   yaw   > 0 = crosshair goes R → compensate by pushing mouse LEFT  (−dx in plot)
#
# Values are based on CS2 community spray-pattern documentation.
# Fire rates: AK47 600 RPM (0.100s/bullet), M4A4 666 RPM (0.090s/bullet),
#             M4A1-S 600 RPM (0.100s/bullet).
#
# If the overlay looks scaled wrong for your setup, adjust --sensitivity:
#   higher sensitivity → fewer counts needed → overlay shrinks vertically.

WEAPON_DATA = {
    "ak47": {
        "name": "AK-47",
        "rpm": 600,
        "mag": 30,
        "color": "tomato",
        # (cumulative_pitch_up_deg, cumulative_yaw_right_deg)
        "pattern": [
            (0.00,  0.00),   # 1
            (0.90,  0.00),   # 2
            (2.40,  0.00),   # 3
            (4.00,  0.10),   # 4
            (5.50,  0.40),   # 5
            (6.70,  0.90),   # 6
            (7.60,  1.60),   # 7
            (8.30,  2.10),   # 8
            (8.90,  2.40),   # 9
            (9.30,  2.30),   # 10
            (9.50,  1.90),   # 11
            (9.60,  1.30),   # 12
            (9.70,  0.50),   # 13
            (9.70, -0.40),   # 14
            (9.80, -1.10),   # 15
            (9.80, -1.70),   # 16
            (9.80, -2.10),   # 17
            (9.70, -2.20),   # 18
            (9.60, -2.10),   # 19
            (9.50, -1.70),   # 20
            (9.40, -1.20),   # 21
            (9.30, -0.60),   # 22
            (9.20,  0.00),   # 23
            (9.10,  0.50),   # 24
            (9.00,  0.90),   # 25
            (8.90,  1.00),   # 26
            (8.80,  0.80),   # 27
            (8.70,  0.50),   # 28
            (8.60,  0.00),   # 29
            (8.50, -0.50),   # 30
        ],
    },
    "m4a4": {
        "name": "M4A4",
        "rpm": 666,
        "mag": 30,
        "color": "dodgerblue",
        "pattern": [
            (0.00,  0.00),   # 1
            (0.70,  0.00),   # 2
            (1.80,  0.00),   # 3
            (3.10,  0.10),   # 4
            (4.20,  0.30),   # 5
            (5.20,  0.60),   # 6
            (6.00,  1.00),   # 7
            (6.70,  1.30),   # 8
            (7.20,  1.40),   # 9
            (7.50,  1.30),   # 10
            (7.70,  1.00),   # 11
            (7.80,  0.60),   # 12
            (7.90,  0.10),   # 13
            (7.90, -0.40),   # 14
            (7.90, -0.80),   # 15
            (7.80, -1.10),   # 16
            (7.70, -1.30),   # 17
            (7.60, -1.40),   # 18
            (7.50, -1.30),   # 19
            (7.40, -1.00),   # 20
            (7.30, -0.60),   # 21
            (7.20, -0.20),   # 22
            (7.10,  0.20),   # 23
            (7.00,  0.50),   # 24
            (6.90,  0.70),   # 25
            (6.80,  0.80),   # 26
            (6.70,  0.70),   # 27
            (6.60,  0.50),   # 28
            (6.50,  0.10),   # 29
            (6.40, -0.30),   # 30
        ],
    },
    "m4a1s": {
        "name": "M4A1-S",
        "rpm": 600,
        "mag": 20,
        "color": "mediumseagreen",
        "pattern": [
            (0.00,  0.00),   # 1
            (0.60,  0.00),   # 2
            (1.60,  0.00),   # 3
            (2.80,  0.00),   # 4
            (3.80,  0.20),   # 5
            (4.70,  0.40),   # 6
            (5.40,  0.70),   # 7
            (5.90,  0.80),   # 8
            (6.30,  0.80),   # 9
            (6.50,  0.60),   # 10
            (6.60,  0.30),   # 11
            (6.70,  0.00),   # 12
            (6.70, -0.30),   # 13
            (6.70, -0.60),   # 14
            (6.60, -0.80),   # 15
            (6.50, -0.90),   # 16
            (6.40, -0.80),   # 17
            (6.30, -0.60),   # 18
            (6.20, -0.30),   # 19
            (6.10,  0.00),   # 20
        ],
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_sprays(directory):
    files = sorted(glob.glob(os.path.join(directory, "spray_*.json")))
    sprays = []
    for f in files:
        try:
            with open(f) as fh:
                data = json.load(fh)
            data["_file"] = os.path.basename(f)
            if data.get("samples"):
                sprays.append(data)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  skipping {f}: {e}", file=sys.stderr)
    return sprays


def cumulative(spray):
    """Return (t_list, cx_list, cy_list) — cumulative mouse movement in counts."""
    t, x, y = [], [], []
    cx = cy = 0
    for s in spray["samples"]:
        cx += s["dx"]
        cy += s["dy"]
        t.append(s["t"])
        x.append(cx)
        y.append(cy)
    return t, x, y


def resample_to_grid(t, vals, n, duration):
    """Linear-resample vals onto n evenly-spaced points over [0, duration]."""
    import numpy as np
    if duration <= 0 or len(t) < 2:
        return np.zeros(n)
    return np.interp(np.linspace(0, duration, n), t, vals)


def pattern_to_counts(weapon_key, sensitivity, m_yaw=0.022, max_duration=None):
    """
    Convert a weapon recoil pattern to ideal mouse counts.

    Returns (times, ideal_dx, ideal_dy) where:
      ideal_dy > 0 = pull down (compensates gun rising)
      ideal_dx < 0 = push left (compensates gun going right)
    """
    wdata = WEAPON_DATA[weapon_key]
    pattern = wdata["pattern"]
    interval = 60.0 / wdata["rpm"]

    if max_duration is not None:
        n = min(len(pattern), int(max_duration / interval) + 2)
        pattern = pattern[:n]

    times = [i * interval for i in range(len(pattern))]
    ideal_dx = [-p[1] / (m_yaw * sensitivity) for p in pattern]   # oppose yaw
    ideal_dy = [ p[0] / (m_yaw * sensitivity) for p in pattern]   # oppose pitch
    return times, ideal_dx, ideal_dy


def consistency_score(sprays, n=100):
    """Mean per-time-step spread across all sprays (in counts). Lower = tighter."""
    import numpy as np
    if len(sprays) < 2:
        return None
    xs, ys = [], []
    for sp in sprays:
        t, x, y = cumulative(sp)
        dur = sp.get("duration") or (t[-1] if t else 0)
        xs.append(resample_to_grid(t, x, n, dur))
        ys.append(resample_to_grid(t, y, n, dur))
    xs, ys = np.array(xs), np.array(ys)
    spread = np.sqrt(xs.std(axis=0) ** 2 + ys.std(axis=0) ** 2)
    return float(spread.mean())


def accuracy_score(sprays, weapon_key, sensitivity, m_yaw=0.022, n=100):
    """
    RMS distance (in counts) between the average recorded spray and the ideal
    weapon pattern, evaluated over the common time window. Lower = more accurate.
    """
    import numpy as np
    durations = [sp.get("duration") or 0 for sp in sprays]
    common_dur = min(durations)
    if common_dur <= 0:
        return None

    wdata = WEAPON_DATA[weapon_key]
    max_pattern_dur = (len(wdata["pattern"]) - 1) * 60.0 / wdata["rpm"]
    window = min(common_dur, max_pattern_dur)
    if window <= 0:
        return None

    it, ix, iy = pattern_to_counts(weapon_key, sensitivity, m_yaw, max_duration=window)

    grid = np.linspace(0, window, n)
    ideal_x_g = np.interp(grid, it, ix)
    ideal_y_g = np.interp(grid, it, iy)

    spray_xs, spray_ys = [], []
    for sp in sprays:
        t, x, y = cumulative(sp)
        # clip spray to window
        spray_xs.append(resample_to_grid(t, x, n, window))
        spray_ys.append(resample_to_grid(t, y, n, window))

    mean_x = np.array(spray_xs).mean(axis=0)
    mean_y = np.array(spray_ys).mean(axis=0)

    rms = float(np.sqrt(((mean_x - ideal_x_g) ** 2 + (mean_y - ideal_y_g) ** 2).mean()))
    return rms


def do_list(sprays):
    print(f"{len(sprays)} spray(s):")
    for i, sp in enumerate(sprays):
        print(f"  [{i:3d}] {sp['_file']:36s}  "
              f"{sp.get('duration', 0)*1000:6.0f} ms  "
              f"{sp.get('n_samples', 0):4d} samples  "
              f"net ({sp.get('net_dx', 0):+5d},{sp.get('net_dy', 0):+5d})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Visualize recorded CS2 sprays.")
    ap.add_argument("--dir", default="sprays",
                    help="directory of spray JSON files (default: ./sprays)")
    ap.add_argument("--index", type=int, default=-1,
                    help="which spray to detail (default: -1 = latest)")
    ap.add_argument("--last", type=int, default=0,
                    help="only use the N most recent sprays (0 = all)")
    ap.add_argument("--weapon", choices=list(WEAPON_DATA), metavar="WEAPON",
                    help="overlay ideal pattern: ak47, m4a4, m4a1s")
    ap.add_argument("--sensitivity", type=float, default=1.0,
                    help="CS2 in-game sensitivity (default: 1.0)")
    ap.add_argument("--m-yaw", type=float, default=0.022, dest="m_yaw",
                    help="CS2 m_yaw value (default: 0.022)")
    ap.add_argument("--list", action="store_true",
                    help="list sprays and exit")
    ap.add_argument("--save", help="save figure to this path instead of showing")
    args = ap.parse_args()

    directory = os.path.expanduser(args.dir)
    sprays = load_sprays(directory)
    if not sprays:
        sys.exit(f"No sprays found in {directory}/  (record some with spray_record.py first)")

    if args.last > 0:
        sprays = sprays[-args.last:]

    if args.list:
        do_list(sprays)
        return

    try:
        sel = sprays[args.index]
    except IndexError:
        sys.exit(f"--index {args.index} out of range (have {len(sprays)} sprays)")

    import matplotlib
    if args.save:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    import numpy as np

    wdata = WEAPON_DATA.get(args.weapon) if args.weapon else None

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    weapon_label = f" — {wdata['name']}" if wdata else ""
    fig.suptitle(
        f"Spray analysis{weapon_label}   sens {args.sensitivity}   "
        f"({len(sprays)} spray(s) from {directory}/)",
        fontsize=12,
    )

    # ------------------------------------------------------------------
    # Panel 1: selected spray trajectory + weapon ideal overlay
    # ------------------------------------------------------------------
    ax = axes[0, 0]
    t, x, y = cumulative(sel)
    pts = np.array([x, y]).T.reshape(-1, 1, 2)
    segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
    lc = LineCollection(segs, cmap="viridis", linewidth=2, zorder=3)
    lc.set_array(np.array(t))
    ax.add_collection(lc)
    fig.colorbar(lc, ax=ax).set_label("time (s)")
    ax.plot(x[0], y[0], "go", ms=9, zorder=5, label="start (bullet 1)")
    ax.plot(x[-1], y[-1], "rs", ms=8, zorder=5, label="end")

    if wdata:
        it, ix, iy = pattern_to_counts(
            args.weapon, args.sensitivity, args.m_yaw,
            max_duration=sel.get("duration"),
        )
        ax.plot(ix, iy, "--", color=wdata["color"], lw=2, zorder=4,
                label=f"ideal {wdata['name']}")
        ax.plot(ix[0], iy[0], "^", color=wdata["color"], ms=8, zorder=5)

    dur_ms = sel.get("duration", 0) * 1000
    ax.set_title(f"Trajectory — {sel['_file']}\n{dur_ms:.0f} ms, {len(t)} samples")
    ax.set_xlabel("horizontal (counts ←L  R→)")
    ax.set_ylabel("vertical (counts ↑up  down↓)")
    ax.invert_yaxis()
    ax.axhline(0, color="gray", lw=0.5)
    ax.axvline(0, color="gray", lw=0.5)
    ax.set_aspect("equal", adjustable="datalim")
    ax.legend(fontsize=8, loc="best")
    ax.margins(0.1)

    # ------------------------------------------------------------------
    # Panel 2: all sprays overlaid + weapon ideal
    # ------------------------------------------------------------------
    ax = axes[0, 1]
    for sp in sprays:
        _, xx, yy = cumulative(sp)
        is_sel = sp is sel
        ax.plot(xx, yy,
                color="crimson" if is_sel else "steelblue",
                alpha=0.9 if is_sel else 0.30,
                lw=2 if is_sel else 0.8,
                zorder=3 if is_sel else 1)
    ax.plot(0, 0, "go", ms=8, zorder=5)

    if wdata:
        max_dur = max((sp.get("duration") or 0) for sp in sprays)
        it, ix, iy = pattern_to_counts(
            args.weapon, args.sensitivity, args.m_yaw, max_duration=max_dur
        )
        ax.plot(ix, iy, "--", color=wdata["color"], lw=2.5, zorder=4,
                label=f"ideal {wdata['name']}")
        ax.plot(ix[0], iy[0], "^", color=wdata["color"], ms=8, zorder=5)
        ax.legend(fontsize=8)

    c_score = consistency_score(sprays)
    title = "All sprays overlaid"
    if c_score is not None:
        title += f"\nconsistency spread: {c_score:.0f} counts (lower = tighter)"
    ax.set_title(title)
    ax.set_xlabel("horizontal (counts)")
    ax.set_ylabel("vertical (counts)")
    ax.invert_yaxis()
    ax.axhline(0, color="gray", lw=0.5)
    ax.axvline(0, color="gray", lw=0.5)
    ax.set_aspect("equal", adjustable="datalim")
    ax.margins(0.1)

    # ------------------------------------------------------------------
    # Panel 3: per-sample dx / dy over time
    # ------------------------------------------------------------------
    ax = axes[1, 0]
    dxs = [s["dx"] for s in sel["samples"]]
    dys = [s["dy"] for s in sel["samples"]]
    ax.plot(t, dxs, label="dx (horizontal)", color="tab:blue", lw=1)
    ax.plot(t, dys, label="dy (vertical)", color="tab:orange", lw=1)
    ax.axhline(0, color="gray", lw=0.5)
    ax.set_title("Per-sample movement over time (selected spray)")
    ax.set_xlabel("time (s)")
    ax.set_ylabel("counts / sample")
    ax.legend(fontsize=8)

    # ------------------------------------------------------------------
    # Panel 4: cumulative pull over time + ideal weapon pattern
    # ------------------------------------------------------------------
    ax = axes[1, 1]
    ax.plot(t, x, label="your horizontal", color="tab:blue", lw=1.5)
    ax.plot(t, y, label="your vertical (pull-down)", color="tab:orange", lw=1.5)

    if wdata:
        it, ix, iy = pattern_to_counts(
            args.weapon, args.sensitivity, args.m_yaw,
            max_duration=sel.get("duration"),
        )
        ax.plot(it, ix, "--", color="lightblue", lw=1.5,
                label=f"ideal H ({wdata['name']})")
        ax.plot(it, iy, "--", color="bisque", lw=1.5,
                label=f"ideal V ({wdata['name']})")

    ax.axhline(0, color="gray", lw=0.5)
    ax.set_title("Cumulative pull over time")
    ax.set_xlabel("time (s)")
    ax.set_ylabel("counts")
    ax.legend(fontsize=8)

    fig.tight_layout(rect=(0, 0.03, 1, 0.96))

    # Scores printed below figure
    score_parts = []
    if c_score is not None:
        score_parts.append(f"Consistency spread: {c_score:.0f} counts (lower = tighter)")
    if args.weapon:
        a_score = accuracy_score(sprays, args.weapon, args.sensitivity, args.m_yaw)
        if a_score is not None:
            wname = WEAPON_DATA[args.weapon]["name"]
            score_parts.append(
                f"Accuracy vs {wname} ideal: {a_score:.0f} counts avg error (lower = closer to ideal)"
            )
    if score_parts:
        fig.text(0.5, 0.01, "   |   ".join(score_parts), ha="center",
                 fontsize=9, color="dimgray")

    if args.save:
        fig.savefig(args.save, dpi=120)
        print(f"Saved figure to {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
