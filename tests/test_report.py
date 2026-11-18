"""Regression coverage for the statistics used in the study's acceptance criteria."""

import csv
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quantum_advantage_reduce_depth.report import analyze, write_report


def study():
    descriptors = [("a", "Peaked", "FakeBoston"), ("b", "Peaked", "FakeNighthawk"),
                   ("c", "Graph checks", "FakeNighthawk")]
    plan = dict(run_id="fixture", seeds=[0, 1, 2], cases=[])
    verified, rows = {}, []
    metrics = {
        "a": ([(12, 120), (10, 100), (10, 100)], [(9, 115), (12, 90), (9, 110)]),
        "b": ([(20, 50), (18, 55), (25, 42)], [(18, 55), (22, 40), (24, 38)]),
        "c": ([(5, 18), (5, 18), (5, 18)], [(10, 15), (10, 16), (10, 20)]),
    }
    for case_id, family, target in descriptors:
        label = "Random graph sampling with 27 checks" if case_id == "c" else "Peaked " + case_id
        plan["cases"].append(dict(case_id=case_id, circuit_id=case_id, target=target, family=family,
                                  label=label, stock=dict(d2=metrics[case_id][0][0][0], n2=1)))
        verified[case_id] = dict(complete=True, issues=[],
                                 artifacts={k: f"best/{case_id}-{k}.qpy" for k in ("decay", "weighted", "overall")})
        for arm, sequence in zip(("decay", "weighted"), metrics[case_id]):
            for seed, (d2, n2) in enumerate(sequence):
                rows.append(dict(task_id=f"{case_id}-{arm}-{seed}", case_id=case_id, seed=seed,
                                 arm=arm, status="ok", d2=d2, n2=n2, depth=d2 * 2, seconds=seed + 1))
    return plan, rows, verified


class ReportTests(unittest.TestCase):
    def test_paired_statistics_and_winner_ties(self):
        results, pairs, history = analyze(*study())
        self.assertTrue(results["complete"])
        cases = {c["case_id"]: c for c in results["cases"]}
        self.assertEqual(cases["a"]["decay_seed"], 1)
        self.assertEqual(cases["a"]["weighted_seed"], 2)
        self.assertEqual(cases["a"]["n2_change"], 10)
        self.assertEqual(cases["a"]["winner_arm"], "weighted")
        self.assertEqual((cases["b"]["decay_seed"], cases["b"]["weighted_seed"]), (1, 0))
        self.assertEqual(cases["b"]["winner_arm"], "decay")
        self.assertEqual([p["d2_reduction_pct"] for p in pairs if p["case_id"] == "a"], [25, -20, 10])
        self.assertEqual((cases["a"]["paired_median_pct"], cases["a"]["paired_q1_pct"],
                          cases["a"]["paired_q3_pct"], cases["a"]["paired_iqr_pct"]), (10, -5, 17.5, 22.5))
        decay = [p for p in history if p["case_id"] == "a" and p["arm"] == "decay"]
        self.assertEqual([p["best_d2"] for p in decay], [12, 10, 10])
        self.assertEqual([p["cumulative_seconds"] for p in decay], [1, 3, 6])
        self.assertEqual(sum(p["arm_winner"] for p in history), 6)
        overall = results["summaries"][0]
        self.assertAlmostEqual(overall["d2_ratio_gmean"], math.pow(.9 * 1 * 2, 1 / 3))
        self.assertEqual([overall[k] for k in ("case_wins", "case_ties", "case_losses")], [1, 1, 1])
        self.assertEqual([overall[k] for k in ("seed_wins", "seed_ties", "seed_losses")], [4, 0, 5])
        self.assertEqual(overall["pairs"], 9)
        self.assertEqual(overall["weighted_seconds"], 18)
        graph = next(s for s in results["summaries"] if s["scope"] == "family" and s["family"] == "Graph checks")
        self.assertNotIn("d2_reduction_min_pct", graph)
        self.assertEqual(graph["d2_reduction_median_pct"], -100)

    def test_incomplete_case_suppresses_overlapping_groups(self):
        plan, rows, verified = study()
        verified["b"] = dict(complete=False, issues=["duplicate weighted trial"], artifacts={})
        rows.append(rows[6])
        results, pairs, history = analyze(plan, rows, verified)
        self.assertFalse(results["complete"])
        incomplete = next(c for c in results["cases"] if c["case_id"] == "b")
        self.assertEqual(incomplete["decay_ok"], 3)
        self.assertNotIn("winner_arm", incomplete)
        self.assertFalse(any(p["case_id"] == "b" for p in pairs + history))
        for s in results["summaries"]:
            partial = (s["scope"] == "all" or
                       (s["scope"] == "target" and s["target"] == "FakeNighthawk") or
                       (s["family"] == "Peaked" and s["target"] != "FakeBoston"))
            self.assertEqual(s["complete"], not partial)
            self.assertEqual("d2_reduction_pct" in s, not partial)

    def test_successful_retry_preserves_seed_and_includes_failed_cost(self):
        plan, rows, verified = study()
        rows[0]["previous_attempts"] = [dict(status="error", seconds=4.5), dict(status="error")]
        results, pairs, history = analyze(plan, rows, verified)
        case = next(c for c in results["cases"] if c["case_id"] == "a")
        self.assertEqual(case["decay_ok"], 3)
        self.assertEqual(case["decay_success_seconds"], 6)
        self.assertEqual(case["decay_failed_attempts"], 2)
        self.assertEqual(case["decay_untimed_failures"], 1)
        self.assertEqual(case["decay_seconds"], 10.5)
        self.assertEqual(results["summaries"][0]["decay_seconds"], 22.5)
        self.assertEqual(len(pairs), 9)
        self.assertEqual(history[0]["cumulative_seconds"], 5.5)
        self.assertEqual(history[0]["prior_failed_seconds"], 4.5)

    def test_report_links_tables_and_plots(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            plan, rows, verified = study()
            write_report(path, plan, rows, verified)
            report = (path / "report.md").read_text()
            self.assertIn("18/18", report)
            self.assertIn("best/a-weighted.qpy", report)
            self.assertTrue(json.loads((path / "results.json").read_text())["complete"])
            figures = list((path / "figures").glob("*.png"))
            self.assertEqual(len(figures), 5)
            for f in figures:
                self.assertIn(f.name, report)
                self.assertEqual(f.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            with (path / "tables" / "seed_pairs.csv").open() as f:
                self.assertEqual(len(list(csv.DictReader(f))), 9)
            previous = (path / "tables" / "cases.csv").read_bytes()
            rows[0]["seconds"] += 11
            with patch("quantum_advantage_reduce_depth.figures.write_figures",
                       side_effect=RuntimeError("render interrupted")), self.assertRaises(RuntimeError):
                write_report(path, plan, rows, verified)
            self.assertEqual((path / "tables" / "cases.csv").read_bytes(), previous)
            self.assertEqual((path / "report.md").read_text(), report)
            for item in verified.values():
                item.update(complete=False, artifacts={}, issues=["failed"])
            write_report(path, plan, rows, verified)
            self.assertEqual(list((path / "figures").glob("*.png")), [])
            self.assertNotIn("best/a-weighted.qpy", (path / "report.md").read_text())
