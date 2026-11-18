import os
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from quantum_advantage_reduce_depth import compiler, report, sweep
from quantum_advantage_reduce_depth.storage import sha256, write_json


@pytest.fixture
def study(tmp_path):
    compiler.configure_threads()
    from qiskit import QuantumCircuit, qpy

    backend = sweep.backend_for("FakeBoston")
    source = QuantumCircuit(3)
    source.h(0)
    source.cz(0, 2)
    snapshot = tmp_path / "input.qpy"
    with snapshot.open("wb") as stream:
        qpy.dump(source, stream)
    case = dict(case_id="boston-fixture", circuit_id="fixture", target="FakeBoston",
                label="CZ fixture", family="fixture", stock={"d2": 1, "n2": 1},
                sha256=sha256(snapshot), snapshot=snapshot.name)
    plan = dict(run_id="test", seeds=[0], cases=[case], tasks=list(sweep.tasks_for(case, [0])),
                fingerprint={"compiler_sha256": sha256(Path(compiler.__file__)),
                             "runtime": compiler.runtime_metadata()},
                targets={"FakeBoston": compiler.target_metadata(backend)})
    write_json(tmp_path / "plan.json", plan)
    return plan, case


def test_plan_requires_preparation_from_the_same_environment(tmp_path):
    compiler.configure_threads()
    runtime = compiler.runtime_metadata()
    cases = [{"case_id": f"case-{i}", "target": "FakeBoston" if i < 14 else "FakeNighthawk"}
             for i in range(18)]
    other = {**runtime, "packages": {**runtime["packages"], "numpy": "0"}}
    write_json(tmp_path / "prepared.json", {"cases": cases, "runtime": other})
    with pytest.raises(ValueError, match="different packages"):
        sweep.create_plan(tmp_path)
    assert not (tmp_path / "plan.json").exists()
    write_json(tmp_path / "prepared.json", {"cases": cases, "runtime": runtime})
    assert len(sweep.create_plan(tmp_path)["tasks"]) == 1800


@pytest.mark.parametrize("mismatch", ["compiler", "packages", "target"])
def test_worker_provenance_fails_before_compile(tmp_path, study, mismatch):
    plan, case = study
    if mismatch == "compiler":
        plan["fingerprint"]["compiler_sha256"] = "wrong"
    elif mismatch == "packages":
        plan["fingerprint"]["runtime"]["packages"]["numpy"] = "wrong"
    else:
        plan["targets"]["FakeBoston"]["configuration_sha256"] = "wrong"
    write_json(tmp_path / "plan.json", plan)
    sweep.execute_batch(tmp_path, plan["run_id"], case, plan["tasks"][:1])
    rows, issues = sweep.canonical_rows(tmp_path, plan)
    assert not issues and len(rows) == 1
    assert rows[0]["status"] == "error" and "differs from its plan" in rows[0]["error"]
    assert rows[0]["seconds"] == 0 and "artifact" not in rows[0]


def test_retry_preserves_identity_and_captures_compile_cost(tmp_path, study, monkeypatch):
    plan, case = study
    tasks = plan["tasks"]
    def fail_manager(*args):
        raise ValueError("simulated compile failure")
    with monkeypatch.context() as patch:
        patch.setattr(compiler, "make_manager", fail_manager)
        sweep.execute_batch(tmp_path, plan["run_id"], case, tasks[:1])
    rows, issues = sweep.canonical_rows(tmp_path, plan)
    assert not issues and len(rows) == 1
    assert rows[0]["seconds"] > 0 and rows[0]["status"] == "error"

    # Same task is retried successfully once and skipped on the next replay.
    sweep.execute_batch(tmp_path, plan["run_id"], case, tasks)
    sweep.execute_batch(tmp_path, plan["run_id"], case, tasks)
    rows, issues = sweep.canonical_rows(tmp_path, plan)
    assert not issues
    assert [r["status"] for r in rows] == ["ok", "ok"]
    assert [r["attempt"] for r in rows] == [2, 1]
    failed, = rows[0]["previous_attempts"]
    assert failed["task_id"] == rows[0]["task_id"] and failed["seconds"] > 0

    # Keep this worker/collector integration test independent of plotting.
    monkeypatch.setattr(report, "write_report",
                        lambda output, plan, rows, verified: report.analyze(plan, rows, verified)[0])
    assert sweep.collect(tmp_path)
    best = tmp_path / "best" / case["case_id"]
    assert (best / "decay.qpy").read_bytes() == (best / "overall.qpy").read_bytes()
    assert (best / "weighted.qpy").exists()

    duplicate = {**rows[0], "attempt": 3}
    write_json(sweep.task_dir(tmp_path, tasks[0]) / "attempt-0003.json", duplicate)
    _, issues = sweep.canonical_rows(tmp_path, plan)
    assert "Duplicate trial" in issues[case["case_id"]][0]
    assert not sweep.collect(tmp_path)
    assert not list(best.glob("*.qpy"))


def _die_once(output, run_id, case, tasks):
    """Simulate one OS-level worker death after an atomic task checkpoint."""
    for task in tasks:
        row = {**task, "run_id": run_id, "attempt": 1, "status": "ok"}
        path = sweep.task_dir(output, task) / "attempt-0001.json"
        if path.exists():
            raise AssertionError("A checkpointed task was submitted twice")
        write_json(path, row)
        marker = output / "first-worker-died"
        if not marker.exists():
            marker.touch()
            os._exit(1)


def test_interrupted_attempt_and_marker_left_after_success(tmp_path, study):
    plan, case = study
    task = plan["tasks"][0]
    directory = sweep.task_dir(tmp_path, task)
    marker = directory / "in-progress.json"
    active = {**task, "run_id": plan["run_id"], "attempt": 1, "started_at": sweep.now()}
    write_json(marker, active)
    sweep.execute_batch(tmp_path, plan["run_id"], case, [task])
    interrupted, completed = sweep.read_attempts(directory, plan["run_id"], task)
    assert interrupted["status"] == "error" and interrupted["seconds"] is None
    assert completed["status"] == "ok" and completed["attempt"] == 2

    # A crash between committing the row and clearing its marker is harmless.
    write_json(marker, {**active, "attempt": 2})
    sweep.execute_batch(tmp_path, plan["run_id"], case, [task])
    assert not marker.exists()
    assert sweep.read_attempts(directory, plan["run_id"], task) == [interrupted, completed]
    rows, issues = sweep.canonical_rows(tmp_path, plan)
    assert not issues and rows[0]["previous_attempts"] == [interrupted]


def test_hard_worker_death_restarts_pool_from_checkpoints(tmp_path, monkeypatch):
    case = {"case_id": "fixture"}
    seeds = [0, 1, 2]
    tasks = list(sweep.tasks_for(case, seeds))
    plan = dict(run_id="pool-recovery", cases=[case], tasks=tasks)
    monkeypatch.setattr(sweep, "execute_batch", _die_once)
    rows = sweep.run_phase(tmp_path, plan, seeds, {"workers": 1, "trials_per_shard": 1},
                           SimpleNamespace(executor="local"), "test", [time.monotonic() + 60])
    assert (tmp_path / "first-worker-died").exists()
    assert [row["task_id"] for row in rows] == [task["task_id"] for task in tasks]
    assert all(row["attempt"] == 1 and row["status"] == "ok" for row in rows)


def test_recover_damaged_winner_without_spending_a_trial(tmp_path, study):
    plan, case = study
    task = plan["tasks"][0]
    sweep.execute_batch(tmp_path, plan["run_id"], case, [task])
    rows, _ = sweep.canonical_rows(tmp_path, plan)
    row, = rows
    candidate = tmp_path / row["artifact"]
    candidate.write_bytes(b"damaged candidate")
    source = sweep.load_snapshot(tmp_path / case["snapshot"], case["sha256"])
    backend = sweep.backend_for(case["target"])
    with pytest.raises(ValueError, match="damaged"):
        sweep.verified_candidate(tmp_path, backend, source, row, repair=False)
    recovered = sweep.verified_candidate(tmp_path, backend, source, row, repair=True)
    assert recovered.name == "recovered.qpy"
    assert sweep.verified_candidate(tmp_path, backend, source, row, repair=False) == recovered
    assert candidate.read_bytes() == b"damaged candidate"
    assert sweep.canonical_rows(tmp_path, plan) == (rows, {})
