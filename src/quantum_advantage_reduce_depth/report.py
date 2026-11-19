"""Compare mean two-qubit depth and gate count by circuit family."""

import csv
import os
from collections import defaultdict
from pathlib import Path
from statistics import fmean, geometric_mean
from tempfile import TemporaryDirectory

from .storage import write_json

FAMILY_FIELDS = ("family", "cases_complete", "cases_expected", "d2_mean_ratio_gmean", "n2_mean_ratio_gmean")
TRACKERS = "https://quantum-advantage-tracker.github.io/trackers"
VERIFIABLE = f"{TRACKERS}/classically-verifiable-problems"
TRACKER_ENTRIES = {
    "Peaked circuits (HQAP)": (
        ("P9 tracker entry", f"{VERIFIABLE}?category=superseded&instance=peaked_circuit_P9_Hqap_56x1917"),
        ("P11 tracker entry", f"{VERIFIABLE}?instance=peaked_circuit_P11_Hqap_98x1999"),
        ("P12 tracker entry", f"{VERIFIABLE}?instance=peaked_circuit_P12_Hqap_98x2457"),
    ),
    "Anderson impurity (SKQD)": (
        ("Four-impurity tracker entry", f"{TRACKERS}/variational-problems?category=baseline&instance=anderson_impurity_model_4i_28b_32e"),
    ),
    "Random graph state sampling with checks": (
        ("Random graph sampling tracker entry", f"{VERIFIABLE}?instance=random_graph_sampling_nq70_depth70_checks27_basis_fstate"),
    ),
}


def analyze(plan, rows, verified_cases):
    """Summarize only families with all planned circuit–target cases verified."""
    trials = defaultdict(list)
    for row in rows:
        if row["status"] == "ok":
            trials[row["case_id"], row["arm"]].append(row)

    def case_ratio(case_id, metric):
        weighted = fmean(r[metric] for r in trials[case_id, "weighted"])
        decay = fmean(r[metric] for r in trials[case_id, "decay"])
        return weighted / decay

    by_family = defaultdict(list)
    for case in plan["cases"]:
        by_family[case["family"]].append(case["case_id"])

    families = []
    for family, cases in sorted(by_family.items()):
        complete = sum(verified_cases[case_id]["complete"] for case_id in cases)
        record = dict(family=family, cases_complete=complete, cases_expected=len(cases))
        if complete == len(cases):
            for metric in ("d2", "n2"):
                record[f"{metric}_mean_ratio_gmean"] = geometric_mean(case_ratio(case_id, metric) for case_id in cases)
        families.append(record)

    complete = all(f["cases_complete"] == f["cases_expected"] for f in families)
    return dict(run_id=plan["run_id"], complete=complete, families=families)


def _markdown(families, plan):
    lines = [
        "# Mean two-qubit depth and gate count by circuit family",
        "",
        "| Circuit family | Verified cases | Two-qubit depth (D2) ratio | D2 improvement | CZ count (N2) ratio | N2 improvement |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for record in families:
        family = record["family"].replace("|", r"\|")
        counts = f"{record['cases_complete']}/{record['cases_expected']}"
        cells = [family, counts]
        for key in ("d2_mean_ratio_gmean", "n2_mean_ratio_gmean"):
            ratio = record.get(key)
            cells.extend(("—", "—") if ratio is None else (f"{ratio:.4f}", f"{1 - ratio:+.2%}"))
        lines.append("| " + " | ".join(cells) + " |")
    lines.extend([
        "", "[Full-precision family ratios (CSV)](families.csv) · [Results (JSON)](results.json)",
        "", "## How the ratios are calculated", "",
        f"1. For each circuit–target pair, average D2 over the {len(plan['seeds'])} seeds separately for wait "
        "time and decay. Divide the wait-time mean by the decay mean. Repeat for N2.",
        "2. Multiply the case ratios within each family and take the nth root, where n is the number of "
        "circuit–target pairs. This geometric mean gives each pair equal weight.",
        "3. Percentage improvement is `100 × (1 − family ratio)`. Positive means lower depth or fewer CZ "
        "gates with wait time. Calculate with full precision and round for display.",
        "", "For example, two cases with ratios 0.80 and 0.90 would give a family ratio of "
        "`√(0.80 × 0.90) ≈ 0.8485`, a 15.15% improvement.",
    ])
    sources = []
    for record in families:
        family = record["family"]
        if family in TRACKER_ENTRIES:
            links = " · ".join(f"[{label}]({url})" for label, url in TRACKER_ENTRIES[family])
            sources.append(f"- **{family}.** {links}")
    if sources:
        lines.extend(["", "## Tracker entries", "", *sources])
    # The exclusions describe the circuit files in this particular tracker snapshot.
    if plan.get("revision") == "4d27f882d55028162206c4f12b5b6fc6a63868e7":
        lines.extend(["", Path(__file__).with_name("exclusions.md").read_text(encoding="utf-8").rstrip()])
    return "\n".join(lines) + "\n"


def write_report(output, plan, rows, verified_cases):
    results = analyze(plan, rows, verified_cases)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # Finish rendering all formats before replacing the published files.
    with TemporaryDirectory(prefix=".report-", dir=output) as temporary:
        stage = Path(temporary)
        with (stage / "families.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=FAMILY_FIELDS)
            writer.writeheader()
            writer.writerows(results["families"])
        write_json(stage / "results.json", results)
        markdown = _markdown(results["families"], plan)
        for name in ("README.md", "report.md"):
            (stage / name).write_text(markdown, encoding="utf-8")
        for name in ("families.csv", "results.json", "report.md", "README.md"):
            os.replace(stage / name, output / name)
    return results
