"""Charts for the Monte Carlo stress test (deck / dashboard PNG).

Palette: dataviz reference categorical slots 1-2 (#2a78d6 blue, #008300 green),
validated (all checks pass, light surface #fcfcfb).
"""
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BLUE, GREEN = "#2a78d6", "#008300"
SURF, INK, INK2, MUTED, GRID, BASE = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"

T = pd.read_csv("monte_carlo_stress.csv")
CLEAN = 170

plt.rcParams.update({
    "font.family": "sans-serif", "text.color": INK,
    "axes.edgecolor": BASE, "axes.labelcolor": INK2,
    "xtick.color": MUTED, "ytick.color": MUTED, "axes.titlecolor": INK})

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12.5, 4.6), facecolor=SURF)

# ---- Panel A: shipments served across futures (frozen vs re-optimised)
lo = int(min(T["frozen_served"].min(), T["reopt_served"].min()))
vals = np.arange(lo, CLEAN + 1)
fz = T["frozen_served"].value_counts().reindex(vals, fill_value=0)
ro = T["reopt_served"].value_counts().reindex(vals, fill_value=0)
w = 0.38
ax1.bar(vals - w/2 - 0.02, fz.values, width=w, color=BLUE, label="Frozen plan")
ax1.bar(vals + w/2 + 0.02, ro.values, width=w, color=GREEN, label="Re-optimised")
ax1.axvline(CLEAN + 0.55, color=MUTED, lw=1, ls=(0, (4, 3)))
ax1.text(CLEAN + 0.45, ax1.get_ylim()[1]*0.97, "clean plan: 170 served",
         ha="right", va="top", fontsize=9, color=INK2)
ax1.set_title("Shipments served across 300 random futures", fontsize=11, loc="left")
ax1.set_xlabel("internal shipments served (of 240)")
ax1.set_ylabel("trials")
ax1.legend(frameon=False, fontsize=9, loc="upper left")

# ---- Panel B: coverage recovered by re-optimising
rec = T["coverage_recovered"]
rv = np.arange(int(rec.min()), int(rec.max()) + 1)
cnt = rec.value_counts().reindex(rv, fill_value=0)
ax2.bar(rv, cnt.values, width=0.78, color=BLUE)
mean = rec.mean()
ax2.axvline(mean, color=INK2, lw=1, ls=(0, (4, 3)))
ax2.text(mean + 0.25, ax2.get_ylim()[1]*0.86, f"mean {mean:.1f}\nrecovered/trial",
         fontsize=9, color=INK2)
ax2.set_title("Value of re-optimising: shipments recovered per trial", fontsize=11, loc="left")
ax2.set_xlabel("extra shipments served vs frozen plan")
ax2.set_ylabel("trials")

for ax in (ax1, ax2):
    ax.set_facecolor(SURF)
    ax.grid(axis="y", color=GRID, lw=0.7)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)

fig.suptitle("Monte Carlo stress test — official v7 internal baseline, 300 trials "
             "(cost + lead shocks, random hub cuts, route outages)",
             fontsize=10, color=INK2, x=0.01, ha="left", y=1.0)
fig.tight_layout(rect=(0, 0, 1, 0.94))
fig.savefig("monte_carlo_stress.png", dpi=180, facecolor=SURF, bbox_inches="tight")
print("wrote monte_carlo_stress.png")
