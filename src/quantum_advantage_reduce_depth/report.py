"""Compare mean two-qubit depth and gate count by circuit family."""

import csv
import os
from collections import defaultdict
from pathlib import Path
from statistics import fmean, geometric_mean
from tempfile import TemporaryDirectory

from .storage import write_json

FAMILY_FIELDS = ("family", "cases_complete", "cases_expected", "d2_mean_ratio_gmean", "n2_mean_ratio_gmean")


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


def _markdown(families, seed_count):
    lines = [
        "# Mean two-qubit depth and gate count by circuit family",
        "",
        f"For each circuit–target pair, take the arithmetic mean over {seed_count} seeds for each heuristic "
        "and divide the wait-time mean by the decay mean. Each family ratio is the geometric mean of its "
        "circuit–target ratios, with equal weight per pair. Percentage improvement is 100 × (1 − ratio). "
        "Positive values mean lower depth or fewer CZ gates with wait time.",
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
    lines.extend(["", "[Full-precision family ratios (CSV)](families.csv) · [Results (JSON)](results.json)"])
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
        markdown = _markdown(results["families"], len(plan["seeds"]))
        for name in ("README.md", "report.md"):
            (stage / name).write_text(markdown, encoding="utf-8")
        for name in ("families.csv", "results.json", "report.md", "README.md"):
            os.replace(stage / name, output / name)
    return results
