"""Static figures for verified equal-budget comparisons, with CSV sources in report.py."""

from __future__ import annotations

import math
from collections import defaultdict
from itertools import groupby
from textwrap import fill

DECAY = "#48678B"
WEIGHTED = "#AF432B"
SUMMARY = "#255844"
GRID = "#DEE4E8"
TEXT = "#203039"

FIGURES = [
    ("Equal-budget D2 reduction", "best_depth.png",
     [("Case winners", "cases.csv"), ("Group summaries", "summaries.csv")]),
    ("D2 reduction at matched seeds", "paired_depth.png",
     [("Paired trial data", "seed_pairs.csv"), ("Case medians and quartiles", "cases.csv")]),
    ("Search convergence by compilation count", "convergence_budget.png", [("Search history", "search.csv")]),
    ("Search convergence by compile time", "convergence_time.png", [("Search history", "search.csv")]),
    ("Native two-qubit depth and gate count", "depth_vs_count.png", [("Trial points and winners", "search.csv")]),
]


def _group_rows(cases, summaries=()):
    rows = []
    for target, group in groupby(cases, key=lambda c: c["target"]):
        for family, members in groupby(group, key=lambda c: c["family"]):
            rows.append(("heading", f"{target}  ·  {family}", None))
            rows.extend(("case", c["label"], c) for c in members)
    if summaries:
        rows.append(("heading", "Group geometric means", None))
        for s in summaries:
            label = s["target"] or s["family"] or "All cases"
            rows.append(("summary", label, s))
    return rows


def _row_axes(plt, rows, title, xlabel, *, depths=False):
    fig_h = 1.65 + .39 * len(rows)
    fig, ax = plt.subplots(figsize=(13.4, fig_h))
    fig.subplots_adjust(left=.365, right=.855 if depths else .965, top=1 - .88 / fig_h,
                        bottom=.7 / fig_h)
    fig.text(.035, 1 - .32 / fig_h, title, ha="left", va="top", fontsize=16, weight="bold")
    ax.set_xlabel(xlabel, labelpad=8)
    ax.set_ylim(len(rows) - .35, -.75)
    ax.axvline(0, linewidth=1.1, color="#78878A", zorder=1)
    ax.grid(axis="x", color=GRID, linewidth=.6, zorder=0)
    ax.tick_params(axis="y", length=0, pad=9)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(["" if kind == "heading" else fill(label, width=59)
                        for kind, label, _ in rows], fontsize=8.7)
    for i, (kind, label, _) in enumerate(rows):
        if kind == "heading":
            ax.axhspan(i - .4, i + .4, color="#EFF4F3", zorder=0)
            fig.text(.035, ax.transData.transform((0, i))[1] / fig.bbox.height,
                     label, fontsize=9.4, color=SUMMARY, weight="bold", va="center")
    ax.spines[["left", "top", "right"]].set_visible(False)
    return fig, ax


def _save(plt, fig, path):
    try:
        fig.savefig(path, dpi=150, facecolor="white")
    finally:
        plt.close(fig)


def _depth_plot(plt, path, cases, summaries):
    from matplotlib.ticker import PercentFormatter

    selected = [s for s in summaries if s["scope"] in ("all", "target", "family") and s["complete"]]
    rows = _group_rows(cases, selected)
    fig, ax = _row_axes(plt, rows, "Equal-budget native two-qubit depth",
                        "D2 reduction from decay to wait time (%)", depths=True)
    ax.text(1.055, 1.015, "Decay → weighted D2", transform=ax.transAxes, size=8.6,
            color=TEXT, weight="bold", ha="left")
    for i, (kind, _, record) in enumerate(rows):
        if kind == "heading":
            continue
        val = record["d2_reduction_pct"]
        if kind == "case":
            color = WEIGHTED if val >= 0 else DECAY
            ax.hlines(i, min(0, val), max(0, val), color=color, lw=2.5)
            ax.scatter(val, i, s=33, color=color, zorder=3)
            ax.text(1.055, i, f"{record['decay_d2']:,} → {record['weighted_d2']:,}",
                    transform=ax.get_yaxis_transform(), va="center", fontsize=8.6)
        else:
            ax.scatter(val, i, s=41, color=SUMMARY, marker="D", zorder=3)
    ax.margins(x=.12)
    ax.xaxis.set_major_formatter(PercentFormatter(xmax=100, decimals=0))
    _save(plt, fig, path)


def _distribution(plt, path, cases, pairs):
    from matplotlib.ticker import PercentFormatter

    rows = _group_rows(cases)
    by_case = defaultdict(list)
    for pair in pairs:
        by_case[pair["case_id"]].append(pair["d2_reduction_pct"])
    fig, ax = _row_axes(plt, rows, "Paired-seed depth reductions",
                        "D2 reduction per shared seed (%)  ·  thick line shows Q1–Q3, diamond shows median")
    extent = max(1., max(abs(p["d2_reduction_pct"]) for p in pairs) * 1.06)
    ax.set_xlim(-extent, extent)
    for i, (kind, _, record) in enumerate(rows):
        if kind != "case":
            continue
        values = by_case[record["case_id"]]
        offsets = [i + (((k * .618033989) % 1) - .5) * .42 for k in range(len(values))]
        ax.scatter(values, offsets, s=11, color=DECAY, alpha=.47, linewidths=0, zorder=2)
        ax.hlines(i, record["paired_q1_pct"], record["paired_q3_pct"], color=SUMMARY, lw=3.6, zorder=3)
        ax.scatter(record["paired_median_pct"], i, s=33, marker="D", color=WEIGHTED,
                   edgecolors="white", linewidths=.65, zorder=4)
    ax.xaxis.set_major_formatter(PercentFormatter(xmax=100, decimals=0))
    _save(plt, fig, path)


def _facets(plt, path, cases, search, key):
    from matplotlib.lines import Line2D
    from matplotlib.ticker import MaxNLocator

    by_arm = defaultdict(list)
    for point in search:
        by_arm[point["case_id"], point["arm"]].append(point)
    title, xlabel, ylabel = {
        "budget": ("Best native two-qubit depth by search budget", "Compilations", "Best D2"),
        "cumulative_seconds": ("Best native two-qubit depth by compile time", "Cumulative compile seconds", "Best D2"),
        "n2": ("Depth and entangling gate count", "CZ count (N2)", "Native two-qubit depth (D2)"),
    }[key]
    ncols = min(3, len(cases))
    nrows = math.ceil(len(cases) / ncols)
    height = 1.4 + nrows * 2.85
    fig, axes = plt.subplots(nrows, ncols, figsize=(max(6.8, ncols * 4.2 + .3), height), squeeze=False)
    fig.subplots_adjust(left=.09 if ncols == 1 else .066, right=.982, top=1 - 1.15 / height,
                        bottom=.65 / height, wspace=.26, hspace=.72)
    fig.text(.035, 1 - .18 / height, title, fontsize=15 if ncols == 1 else 17,
             va="top", weight="bold")
    scatter = key == "n2"
    handles = []
    for label, color, linestyle in (("Decay", DECAY, "-"), ("Wait time", WEIGHTED, "--")):
        handles.append(Line2D([], [], color=color, lw=1.7,
                              linestyle="None" if scatter else linestyle,
                              marker="o" if scatter else None, label=label))
    if scatter:
        handles.append(Line2D([], [], color=TEXT, ls="None", marker="*", ms=10,
                              markerfacecolor="white", label="Arm winner"))
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(.03, 1 - .43 / height),
               borderaxespad=0, frameon=False, ncol=len(handles), fontsize=9)
    for ax, case in zip(axes.flat, cases):
        ax.set_title(fill(f"{case['target']} · {case['label']}", width=38),
                     fontsize=9.5, weight="bold", loc="left", pad=6)
        for arm, color, ls in (("decay", DECAY, "-"), ("weighted", WEIGHTED, "--")):
            points = by_arm[case["case_id"], arm]
            if scatter:
                ax.scatter([p["n2"] for p in points], [p["d2"] for p in points],
                           s=14, alpha=.55, color=color, linewidths=0)
                winner = next(p for p in points if p["arm_winner"])
                # Different sizes preserve evidence of a tie when both stars coincide.
                ax.scatter(winner["n2"], winner["d2"], color=color, marker="*",
                           s=165 if arm == "decay" else 83, edgecolors=TEXT, linewidths=.6, zorder=4)
            else:
                ax.step([p[key] for p in points], [p["best_d2"] for p in points], where="post",
                        color=color, linestyle=ls, lw=1.7)
                ax.plot(points[-1][key], points[-1]["best_d2"], marker="o", color=color, ms=3)
        ax.grid(color=GRID, linewidth=.6)
        ax.set_axisbelow(True)
        ax.spines[["right", "top"]].set_visible(False)
        ax.tick_params(axis="both", labelsize=8)
        ax.xaxis.set_major_locator(MaxNLocator(5, integer=(key != "cumulative_seconds")))
        ax.yaxis.set_major_locator(MaxNLocator(5, integer=True))
        ax.ticklabel_format(style="plain", useOffset=False)
        ax.set_xlabel(xlabel, fontsize=8.5)
        ax.set_ylabel(ylabel, fontsize=8.5)
    for ax in list(axes.flat)[len(cases):]:
        ax.set_visible(False)
    _save(plt, fig, path)


def write_figures(directory, results, pairs, search):
    cases = [c for c in results["cases"] if c["complete"]]
    if not cases:
        for _, filename, _ in FIGURES:
            (directory / filename).unlink(missing_ok=True)
        return []
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    directory.mkdir(parents=True, exist_ok=True)
    with plt.rc_context({"font.family": "DejaVu Sans", "text.color": TEXT,
                         "axes.labelcolor": TEXT, "xtick.color": TEXT, "ytick.color": TEXT}):
        _depth_plot(plt, directory / FIGURES[0][1], cases, results["summaries"])
        _distribution(plt, directory / FIGURES[1][1], cases, pairs)
        for figure, key in zip(FIGURES[2:], ("budget", "cumulative_seconds", "n2")):
            _facets(plt, directory / figure[1], cases, search, key)
    return FIGURES
