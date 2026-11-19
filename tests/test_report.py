import csv
import json
import math
from pathlib import Path

import pytest

from quantum_advantage_reduce_depth.report import analyze, write_report


def study():
    plan = dict(run_id="fixture", seeds=[0, 1, 2], cases=[])
    verified, rows = {}, []
    cases = [
        ("p-boston", "Peaked", [(12, 120), (10, 100), (10, 100)], [(9, 115), (12, 90), (9, 110)]),
        ("p-grid", "Peaked", [(20, 50), (18, 55), (25, 42)], [(18, 55), (22, 40), (24, 38)]),
        ("g-grid", "Graph checks", [(5, 18), (5, 18), (5, 18)], [(10, 15), (10, 16), (10, 20)]),
    ]
    for case_id, family, decay, weighted in cases:
        plan["cases"].append(dict(case_id=case_id, family=family))
        verified[case_id] = dict(complete=True)
        for arm, values in (("decay", decay), ("weighted", weighted)):
            for seed, (d2, n2) in enumerate(values):
                rows.append(dict(case_id=case_id, arm=arm, seed=seed, d2=d2, n2=n2, status="ok"))
    return plan, rows, verified


def test_family_ratios_of_seed_means():
    results = analyze(*study())
    assert results["complete"]
    graph, peaked = results["families"]
    assert [graph["family"], peaked["family"]] == ["Graph checks", "Peaked"]
    assert (peaked["cases_complete"], peaked["cases_expected"]) == (2, 2)
    assert peaked["d2_mean_ratio_gmean"] == pytest.approx(math.sqrt(30 / 32 * 64 / 63))
    assert peaked["n2_mean_ratio_gmean"] == pytest.approx(math.sqrt(315 / 320 * 133 / 147))
    assert graph["d2_mean_ratio_gmean"] == pytest.approx(30 / 15)
    assert graph["n2_mean_ratio_gmean"] == pytest.approx(51 / 54)


def test_family_publication_and_incomplete_rerun(tmp_path):
    plan, rows, verified = study()
    write_report(tmp_path, plan, rows, verified)
    verified["p-grid"]["complete"] = False
    del rows[6]
    results = write_report(tmp_path, plan, rows, verified)
    assert not results["complete"]
    graph, peaked = results["families"]
    assert peaked == dict(family="Peaked", cases_complete=1, cases_expected=2)
    assert graph["d2_mean_ratio_gmean"] == pytest.approx(2)

    assert set(p.name for p in tmp_path.iterdir()) == {"results.json", "families.csv", "report.md", "README.md"}
    assert json.loads((tmp_path / "results.json").read_text()) == results
    with (tmp_path / "families.csv").open() as stream:
        graph_row, peaked_row = csv.DictReader(stream)
    assert float(graph_row["n2_mean_ratio_gmean"]) == graph["n2_mean_ratio_gmean"]
    assert peaked_row["d2_mean_ratio_gmean"] == peaked_row["n2_mean_ratio_gmean"] == ""
    report = (tmp_path / "README.md").read_text()
    assert report == (tmp_path / "report.md").read_text()
    assert "| Graph checks | 1/1 | 2.0000 | -100.00% | 0.9444 | +5.56% |" in report
    assert "| Peaked | 1/2 | — | — | — | — |" in report


def test_published_snapshot_exclusions_match_readme(tmp_path):
    plan, rows, verified = study()
    plan["revision"] = "4d27f882d55028162206c4f12b5b6fc6a63868e7"
    write_report(tmp_path, plan, rows, verified)
    heading = "## Why other circuits were excluded"
    report = (tmp_path / "README.md").read_text()
    exclusions = report[report.index(heading):].strip()
    project_readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "| Operator Loschmidt echo | 9 |" in exclusions
    assert exclusions in project_readme
