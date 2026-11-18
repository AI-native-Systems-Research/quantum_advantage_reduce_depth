"""Equal-budget and paired-seed analyses for the published circuit experiment."""

from __future__ import annotations

import csv
import math
import os
from collections import defaultdict
from pathlib import Path
from statistics import geometric_mean, median
from tempfile import TemporaryDirectory

from .storage import atomic_write, write_json

ARMS = ("decay", "weighted")
CASE_FIELDS = (
    "case_id", "target", "family", "circuit_id", "label", "complete",
    "stock_d2", "stock_n2", "expected_per_arm", "decay_ok", "weighted_ok",
    "decay_seconds", "weighted_seconds", "decay_success_seconds", "weighted_success_seconds",
    "decay_failed_seconds", "weighted_failed_seconds", "decay_failed_attempts", "weighted_failed_attempts",
    "decay_untimed_failures", "weighted_untimed_failures", "decay_seed", "decay_d2", "decay_n2",
    "weighted_seed", "weighted_d2", "weighted_n2", "winner_arm", "d2_ratio",
    "d2_reduction_pct", "n2_ratio", "n2_change", "n2_change_pct", "pairs",
    "paired_median_pct", "paired_q1_pct", "paired_q3_pct", "paired_iqr_pct",
    "seed_wins", "seed_ties", "seed_losses",
)
SUMMARY_FIELDS = (
    "scope", "target", "family", "complete", "cases_complete", "cases_expected",
    "d2_ratio_gmean", "d2_reduction_pct", "d2_reduction_min_pct",
    "d2_reduction_median_pct", "d2_reduction_max_pct", "case_wins", "case_ties",
    "case_losses", "n2_ratio_gmean", "n2_reduction_pct", "pairs", "seed_wins",
    "seed_ties", "seed_losses", "median_paired_median_pct", "decay_seconds",
    "weighted_seconds", "decay_failed_seconds", "weighted_failed_seconds",
    "decay_failed_attempts", "weighted_failed_attempts", "decay_untimed_failures", "weighted_untimed_failures",
)
PAIR_FIELDS = (
    "case_id", "target", "family", "circuit_id", "seed", "decay_d2", "weighted_d2",
    "decay_n2", "weighted_n2", "d2_reduction_pct",
)
SEARCH_FIELDS = (
    "case_id", "target", "family", "circuit_id", "arm", "seed", "budget", "seconds",
    "prior_failed_seconds", "prior_failed_attempts", "cumulative_seconds", "d2", "n2", "depth",
    "best_d2", "arm_winner",
)


def _quantile(sorted_values, p):
    """Interpolated sample quantile (R type 7, NumPy's linear method)."""
    i = (len(sorted_values) - 1) * p
    lo = math.floor(i)
    return sorted_values[lo] + (i - lo) * (sorted_values[math.ceil(i)] - sorted_values[lo])


def _key(row):
    return row["d2"], row["n2"], row["seed"]


def _counts(values):
    return sum(x > 0 for x in values), sum(x == 0 for x in values), sum(x < 0 for x in values)


def _attempt_cost(rows):
    """Account for failures in the original task and any subsequent attempts."""
    attempts = [attempt for row in rows for attempt in (*row.get("previous_attempts", ()), row)]
    failed = [a for a in attempts if a["status"] == "error"]
    success_seconds = math.fsum(r["seconds"] for r in attempts if r["status"] == "ok")
    failed_seconds = math.fsum(r.get("seconds") or 0 for r in failed)
    return dict(seconds=success_seconds + failed_seconds, success_seconds=success_seconds,
                failed_seconds=failed_seconds, failed_attempts=len(failed),
                untimed_failures=sum(r.get("seconds") is None for r in failed))


def _case(case, rows, seeds, verified):
    origin = {key: case[key] for key in ("case_id", "target", "family", "circuit_id")}
    record = dict(origin, label=case["label"], complete=verified["complete"],
                  issues=verified["issues"], artifacts=verified["artifacts"],
                  stock_d2=case["stock"]["d2"], stock_n2=case["stock"]["n2"],
                  expected_per_arm=len(seeds))
    trials = {}
    for arm in ARMS:
        arm_rows = [r for r in rows if r["arm"] == arm]
        trials[arm] = {r["seed"]: r for r in arm_rows if r["status"] == "ok"}
        record[f"{arm}_ok"] = len(set(seeds) & trials[arm].keys())
        for field, value in _attempt_cost(arm_rows).items():
            record[f"{arm}_{field}"] = value
    if not record["complete"]:
        return record, [], []
    winners = {arm: min(trials[arm].values(), key=_key) for arm in ARMS}
    decay, weighted = (winners[arm] for arm in ARMS)
    for arm, winner in winners.items():
        for metric in ("d2", "n2", "seed"):
            record[f"{arm}_{metric}"] = winner[metric]
    record["winner_arm"] = min(ARMS, key=lambda arm: _key(winners[arm])[:2])
    record["d2_ratio"] = weighted["d2"] / decay["d2"]
    record["d2_reduction_pct"] = 100 * (decay["d2"] - weighted["d2"]) / decay["d2"]
    record["n2_ratio"] = weighted["n2"] / decay["n2"]
    record["n2_change"] = weighted["n2"] - decay["n2"]
    record["n2_change_pct"] = 100 * record["n2_change"] / decay["n2"]

    pairs = []
    search = []
    for seed in sorted(seeds):
        base, candidate = (trials[arm][seed] for arm in ARMS)
        pairs.append(dict(origin, seed=seed, decay_d2=base["d2"], weighted_d2=candidate["d2"],
                          decay_n2=base["n2"], weighted_n2=candidate["n2"],
                          d2_reduction_pct=100 * (base["d2"] - candidate["d2"]) / base["d2"]))
    for arm, arm_trials in trials.items():
        cumulative_seconds = 0.0
        best_d2 = None
        for budget, seed in enumerate(sorted(seeds), 1):
            row = arm_trials[seed]
            cost = _attempt_cost([row])
            cumulative_seconds += cost["seconds"]
            best_d2 = row["d2"] if best_d2 is None else min(best_d2, row["d2"])
            search.append(dict(origin, **{k: row[k] for k in ("arm", "seed", "seconds", "d2", "n2", "depth")},
                               budget=budget, cumulative_seconds=cumulative_seconds, best_d2=best_d2,
                               prior_failed_seconds=cost["failed_seconds"], prior_failed_attempts=cost["failed_attempts"],
                               arm_winner=seed == winners[arm]["seed"]))
    values = sorted(p["d2_reduction_pct"] for p in pairs)
    record["pairs"] = len(values)
    record["paired_median_pct"] = median(values)
    record["paired_q1_pct"] = _quantile(values, .25)
    record["paired_q3_pct"] = _quantile(values, .75)
    record["paired_iqr_pct"] = record["paired_q3_pct"] - record["paired_q1_pct"]
    record["seed_wins"], record["seed_ties"], record["seed_losses"] = _counts(values)
    return record, pairs, search


def _summary(cases, *, scope, family=None, target=None):
    complete = sum(c["complete"] for c in cases)
    result = dict(scope=scope, target=target, family=family, complete=complete == len(cases),
                  cases_expected=len(cases), cases_complete=complete)
    if not result["complete"]:
        return result
    reductions = [c["d2_reduction_pct"] for c in cases]
    for metric in ("d2", "n2"):
        gm = geometric_mean(c[f"{metric}_ratio"] for c in cases)
        result[f"{metric}_ratio_gmean"] = gm
        result[f"{metric}_reduction_pct"] = 100 * (1 - gm)
    result["d2_reduction_median_pct"] = median(reductions)
    if len(cases) > 1:
        result["d2_reduction_min_pct"] = min(reductions)
        result["d2_reduction_max_pct"] = max(reductions)
    result["case_wins"], result["case_ties"], result["case_losses"] = _counts(reductions)
    for field in ("pairs", "seed_wins", "seed_ties", "seed_losses"):
        result[field] = sum(c[field] for c in cases)
    result["median_paired_median_pct"] = median(c["paired_median_pct"] for c in cases)
    for arm in ARMS:
        for field in ("seconds", "failed_seconds", "failed_attempts", "untimed_failures"):
            key = f"{arm}_{field}"
            result[key] = sum(c[key] for c in cases)
    return result


def analyze(plan, rows, verified_cases):
    """Analyze audited cases. Return results and the paired and convergence records."""
    by_case = defaultdict(list)
    for row in rows:
        by_case[row["case_id"]].append(row)
    cases, pairs, search = [], [], []
    # Stable target/family grouping, preserving manifest order within each family.
    for case in sorted(plan["cases"], key=lambda c: (c["target"], c["family"])):
        case_id = case["case_id"]
        record, case_pairs, case_search = _case(case, by_case[case_id], plan["seeds"],
                                                verified_cases[case_id])
        cases.append(record)
        pairs.extend(case_pairs)
        search.extend(case_search)
    summaries = [_summary(cases, scope="all")]
    for scope in ("target", "family"):
        for name in sorted({c[scope] for c in cases}):
            members = [c for c in cases if c[scope] == name]
            summaries.append(_summary(members, scope=scope, **{scope: name}))
            if scope == "family":
                for target in sorted({c["target"] for c in members}):
                    subset = [c for c in members if c["target"] == target]
                    summaries.append(_summary(subset, scope="family_target", family=name,
                                              target=target))
    results = dict(run_id=plan["run_id"], complete=summaries[0]["complete"], cases=cases,
                   summaries=summaries)
    return results, pairs, search


def _write_csv(path, fields, records):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def _pct(value):
    return f"{value:+.2f}%"


def _table(header, data):
    def row(items):
        return "| " + " | ".join(str(x).replace("|", r"\|") for x in items) + " |"
    return "\n".join([row(header), row(["---"] * len(header)), *(row(r) for r in data)])


def _summary_name(s):
    if s["scope"] == "all":
        return "All cases"
    return " · ".join(filter(None, (s["family"], s["target"])))


def _comparison_table(summaries):
    def cells(s):
        begin = [_summary_name(s), f"{s['cases_complete']}/{s['cases_expected']}"]
        if not s["complete"]:
            return begin + ["—"] * 8
        spread = _pct(s["d2_reduction_median_pct"])
        if s["cases_expected"] > 1:
            spread = " / ".join(_pct(s[k]) for k in (
                "d2_reduction_min_pct", "d2_reduction_median_pct", "d2_reduction_max_pct"))
        return begin + [_pct(s["d2_reduction_pct"]), spread,
                        f"{s['case_wins']} / {s['case_ties']} / {s['case_losses']}",
                        _pct(s["n2_reduction_pct"]),
                        f"{s['seed_wins']} / {s['seed_ties']} / {s['seed_losses']}",
                        _pct(s["median_paired_median_pct"]), f"{s['decay_seconds']:,.1f}",
                        f"{s['weighted_seconds']:,.1f}"]
    return _table(["Group", "Cases", "D2 reduction¹", "Case D2 reductions, min / median / max",
                   "Cases W / T / L", "N2 reduction¹", "Seeds W / T / L", "Median paired median",
                   "Decay s", "Weighted s"], map(cells, summaries))


def _markdown(results, figures, plan):
    cases, summaries = results["cases"], results["summaries"]
    done = [r for r in cases if r["complete"]]
    n_ok = sum(c[f"{arm}_ok"] for c in cases for arm in ARMS)
    n_total = sum(2 * c["expected_per_arm"] for c in cases)
    lines = ["# SABRE wait time on Quantum Advantage Tracker circuits",
             f"Run `{results['run_id']}` · {len(done)}/{len(cases)} verified cases · "
             f"{n_ok:,}/{n_total:,} successful trial identities."]
    overall = summaries[0]
    if overall["complete"]:
        lines.append(f"The weighted search produced a shallower best circuit in **{overall['case_wins']}** "
                     f"cases, tied in **{overall['case_ties']}** and produced a deeper best circuit in "
                     f"**{overall['case_losses']}**. Across cases, the geometric-mean D2 reduction "
                     f"was **{_pct(overall['d2_reduction_pct'])}**.")
    lines += ["Each circuit was compiled with level-3 optimization and paired outer seeds. The "
              "baseline uses SABRE decay. The weighted arm uses wait time at weight 0.0005. "
              "D2 counts native two-qubit gates on the longest dependency path, including "
              "synchronization across zero-cost operations. N2 counts all CZ gates.",
              "## Equal-budget results by group",
              "Positive reductions indicate a lower weighted-arm value. W / T / L counts shallower, "
              "equal-depth and deeper weighted circuits. ¹ Group reductions use the geometric mean "
              "of weighted-to-decay ratios with equal weight per case. Seconds sum recorded compile "
              "time, including any failed attempts.",
              _comparison_table(summaries),
              "## Equal-budget winners by case",
              "Each arm minimizes native two-qubit depth (D2), then CZ count (N2), then seed. "
              "An exact tie on D2 and N2 selects decay. A positive N2 change means the weighted "
              "winner uses more CZ gates. Winning seeds link to their compiled circuits."]
    def winner_cell(c, arm):
        value = f"{c[arm+'_seed']}"
        return f"[{value}]({c['artifacts'][arm]})"

    def winner_row(c):
        start = [c["target"], c["label"]]
        if not c["complete"]:
            return start + ["—"] * 9
        return start + [winner_cell(c, "decay"), c["decay_d2"], c["decay_n2"],
                        winner_cell(c, "weighted"), c["weighted_d2"], c["weighted_n2"],
                        _pct(c["d2_reduction_pct"]), f"{c['n2_change']:+,} ({_pct(c['n2_change_pct'])})",
                        f"[{c['winner_arm']}]({c['artifacts']['overall']})"]
    lines += [_table(["Target", "Circuit", "Decay seed", "D2", "N2", "Weighted seed",
                      "D2", "N2", "D2 reduction", "N2 change", "Winner"], map(winner_row, cases)),
              "## Seed pairing and compile cost",
              "Each pair shares its outer seed. Quartiles use linear interpolation. "
              "IQR bounds and width describe the distribution of paired D2 reductions. "
              "Trial seconds cover pass-manager construction and compilation.",
              _table(["Target", "Circuit", "Stock D2 / N2", "Decay OK / expected", "Weighted OK / expected",
                      "Decay s", "Weighted s", "Pairs", "Median", "Q1 to Q3", "IQR width (pp)", "W / T / L"],
                     ([c["target"], c["label"], f"{c['stock_d2']} / {c['stock_n2']}",
                       f"{c['decay_ok']} / {c['expected_per_arm']}",
                       f"{c['weighted_ok']} / {c['expected_per_arm']}", f"{c['decay_seconds']:,.2f}",
                       f"{c['weighted_seconds']:,.2f}"] +
                      ([c["pairs"], _pct(c["paired_median_pct"]),
                        f"{_pct(c['paired_q1_pct'])} to {_pct(c['paired_q3_pct'])}", f"{c['paired_iqr_pct']:.2f}",
                        f"{c['seed_wins']} / {c['seed_ties']} / {c['seed_losses']}"]
                       if c["complete"] else ["—"] * 5) for c in cases))]
    if len(done) < len(cases):
        lines += ["## Incomplete cases",
                  _table(["Target", "Circuit", "Issue"], ([c["target"], c["label"], ", ".join(c["issues"])]
                                                         for c in cases if not c["complete"]))]
    if any(c[f"{arm}_failed_attempts"] for c in cases for arm in ARMS):
        lines += ["## Failed attempts",
                  "Successful retries keep their original seed and count once toward the paired budget. "
                  "The time curves include all recorded attempts for each seed. Untimed failures add "
                  "no duration to the recorded totals.",
                  _table(["Target", "Circuit", "Arm", "Failed attempts", "Recorded failed seconds", "Untimed failures"],
                         ([c["target"], c["label"], arm, c[f"{arm}_failed_attempts"],
                           f"{c[f'{arm}_failed_seconds']:.2f}", c[f"{arm}_untimed_failures"]]
                          for c in cases for arm in ARMS if c[f"{arm}_failed_attempts"]))]
    if pilot := plan.get("pilot"):
        lines += ["## Execution",
                  f"The {pilot['pilot_trials']}-compilation pilot used {pilot['pilot_compile_seconds']:,.1f} "
                  f"compile seconds. Peak worker RSS was {pilot['peak_worker_rss_mib']:,.0f} MiB, "
                  f"and the slowest compilation took {pilot['max_compile_seconds']:.1f} s. "
                  f"The {plan['executor']} executor used up to {pilot['workers']} workers, with "
                  f"{pilot['trials_per_shard']} trials per shard. "
                  "[Run plan and runtime versions](plan.json) · [Source identities and routing screen](prepared.json)"]
    if figures:
        lines.append("## Figures")
        for name, filename, tablefiles in figures:
            links = " · ".join(f"[{label}](tables/{path})" for label, path in tablefiles)
            lines.extend([f"### {name}", f"![{name}](figures/{filename})", links])
    lines += ["## Data", "[Run plan](plan.json) · [Trial measurements and errors](trials.json) · "
              "[Case and group results](results.json) · [Case table](tables/cases.csv) · "
              "[Group table](tables/summaries.csv) · [Paired seeds](tables/seed_pairs.csv) · "
              "[Search history and scatter points](tables/search.csv)"]
    if "published_inventory" in plan:
        inventory = plan["published_inventory"]
        lines.append(f"{inventory['note']} [Annotated source inventory]({inventory['path']})")
    return "\n\n".join(lines) + "\n"


def write_report(output: Path, plan: dict, rows: list[dict], verified_cases: dict) -> dict:
    """Write report artifacts into a run directory after the collector audits its exports."""
    from .figures import FIGURES, write_figures

    output = Path(output)
    results, pairs, search = analyze(plan, rows, verified_cases)
    output.mkdir(parents=True, exist_ok=True)
    # Render the entire report before publishing any part of it. Keeping the
    # stage on the same filesystem permits atomic replacements, with Markdown last.
    with TemporaryDirectory(prefix=".report-", dir=output) as temporary:
        stage = Path(temporary)
        tables = stage / "tables"
        tables.mkdir()
        for filename, fields, data in (("cases.csv", CASE_FIELDS, results["cases"]),
                                       ("summaries.csv", SUMMARY_FIELDS, results["summaries"]),
                                       ("seed_pairs.csv", PAIR_FIELDS, pairs),
                                       ("search.csv", SEARCH_FIELDS, search)):
            _write_csv(tables / filename, fields, data)
        figures = write_figures(stage / "figures", results, pairs, search)
        write_json(stage / "results.json", results)
        atomic_write(stage / "report.md", lambda stream: stream.write(_markdown(results, figures, plan)))
        for folder in ("tables", "figures"):
            (output / folder).mkdir(exist_ok=True)
            for path in (stage / folder).glob("*"):
                os.replace(path, output / folder / path.name)
        os.replace(stage / "results.json", output / "results.json")
        os.replace(stage / "report.md", output / "report.md")
        published = {name for _, name, _ in figures}
        for _, name, _ in FIGURES:
            if name not in published:
                (output / "figures" / name).unlink(missing_ok=True)
    return results
