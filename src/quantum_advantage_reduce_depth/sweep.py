"""Run a fixed paired matrix locally and collect its checkpointed measurements."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from functools import lru_cache
import math
import multiprocessing
import os
from pathlib import Path
import time
import uuid

from . import compiler
from .storage import read_json, sha256, write_json

ARMS = ("decay", "weighted")
SEEDS = list(range(50))
PILOT_SEEDS = list(range(3))
MONITOR_SECONDS = 20 * 60


def compiler_settings():
    return {
        "optimization_level": 3, "sabre_layout_iterations": compiler.SABRE_ITERATIONS,
        "internal_layout_trials": compiler.SABRE_TRIALS, "internal_swap_trials": compiler.SABRE_TRIALS,
        "routing_method": "none", "threads_per_worker": 1,
        "arms": {"decay": {"decay": [0.001, 5]}, "weighted": {"wait_time": compiler.WEIGHT}},
        "attempt_limit": "10 * target_width", "basic": 1.0, "lookahead": "0.5 / target_width",
    }


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another process holds {path}") from exc
        yield


def tasks_for(case, seeds):
    for seed in seeds:
        for arm in ARMS:
            yield {"task_id": f"{case['case_id']}.{arm}.{seed:02}", "case_id": case["case_id"],
                   "arm": arm, "seed": seed}


def create_plan(output):
    compiler.configure_threads()
    prepared = read_json(output / "prepared.json")
    plan_file = output / "plan.json"
    runtime = compiler.runtime_metadata()
    # The snapshots and stock references come from preparation. They describe the
    # trials only if both steps ran in the same installation.
    for field in ("python", "packages", "qiskit_revision"):
        if prepared["runtime"][field] != runtime[field]:
            raise ValueError(f"Preparation used different {field} than this environment. "
                             "Prepare a new output directory here.")
    fingerprint = {
        "prepared_sha256": sha256(output / "prepared.json"),
        "compiler_sha256": sha256(Path(compiler.__file__)),
        "runtime": runtime,
    }
    cases = prepared["cases"]
    if len(cases) != 18 or len({case["case_id"] for case in cases}) != 18 or (
        Counter(case["target"] for case in cases) != {"FakeBoston": 14, "FakeNighthawk": 4}
    ):
        raise ValueError("Preparation must yield the specified 14 Boston and 4 Nighthawk cases")
    specification = {
        **prepared, "schema_version": 1, "fingerprint": fingerprint,
        "seeds": SEEDS, "pilot_seeds": PILOT_SEEDS, "compiler_settings": compiler_settings(),
        "tasks": [task for case in cases for task in tasks_for(case, SEEDS)],
    }
    if plan_file.exists():
        plan = read_json(plan_file)
        if any(plan.get(k) != v for k, v in specification.items()):
            raise ValueError("Inputs, compiler, runtime or matrix changed. Prepare a new output directory.")
        return plan
    plan = {**specification, "run_id": uuid.uuid4().hex, "created_at": now()}
    write_json(plan_file, plan)
    return plan


def task_dir(output, task):
    return output / "_work" / "tasks" / task["case_id"] / task["arm"] / f"{task['seed']:02}"


def read_attempts(directory, run_id, task):
    rows = []
    for ordinal, path in enumerate(sorted(directory.glob("attempt-*.json")), 1):
        row = read_json(path)
        if (row["run_id"] != run_id or row["attempt"] != ordinal
                or path.name != f"attempt-{ordinal:04}.json"
                or any(row[k] != v for k, v in task.items())
                or row["status"] not in {"ok", "error"}):
            raise ValueError(f"Invalid attempt identity or status in {path}")
        rows.append(row)
    return rows


@lru_cache(maxsize=2)
def load_snapshot(path, digest):
    from qiskit import qpy

    if sha256(path) != digest:
        raise ValueError(f"Input QPY checksum changed: {path}")
    with path.open("rb") as stream:
        circuit, = qpy.load(stream)
    return circuit


@lru_cache(maxsize=2)
def backend_for(name):
    return compiler.resolve_target(name)


@lru_cache(maxsize=2)
def verified_worker_backend(output, run_id, name):
    """Check the local worker installation once for each run and target it serves."""
    plan = read_json(output / "plan.json")
    fingerprint = plan["fingerprint"]
    if plan["run_id"] != run_id:
        raise ValueError("Worker run ID differs from its plan")
    if sha256(Path(compiler.__file__)) != fingerprint["compiler_sha256"]:
        raise ValueError("Worker compiler checksum differs from its plan")
    runtime = compiler.runtime_metadata()
    for field in ("python", "packages", "qiskit_revision", "thread_limits"):
        if runtime[field] != fingerprint["runtime"][field]:
            raise ValueError(f"Worker {field} differs from its plan")
    backend = backend_for(name)
    if compiler.target_metadata(backend) != plan["targets"][name]:
        raise ValueError(f"Worker {name} target differs from its plan")
    return backend


def recover_interrupted(directory, run_id, task, attempts):
    marker = directory / "in-progress.json"
    if not marker.exists():
        return
    active = read_json(marker)
    if active["run_id"] != run_id or any(active[k] != v for k, v in task.items()):
        raise ValueError(f"Invalid in-progress task identity in {marker}")
    ordinal = active["attempt"]
    if ordinal == len(attempts) + 1:
        # The lock is now ours. The preceding worker exited without finishing.
        row = {**active, "status": "error", "seconds": None,
               "error": "Interrupted worker: no completion checkpoint"}
        write_json(directory / f"attempt-{ordinal:04}.json", row)
        attempts.append(row)
    elif ordinal != len(attempts):
        raise ValueError(f"Invalid in-progress attempt number in {marker}")
    # A crash after publishing a complete row may also leave this marker.
    marker.unlink()


def execute_batch(output, run_id, case, tasks):
    """A batch may be replayed. Only uncompleted task identities get new attempts."""
    output = Path(output)
    for task in tasks:
        directory = task_dir(output, task)
        with lock(directory / ".lock"):
            attempts = read_attempts(directory, run_id, task)
            recover_interrupted(directory, run_id, task, attempts)
            if any(a["status"] == "ok" for a in attempts):
                continue
            ordinal = len(attempts) + 1
            row = {**task, "run_id": run_id, "attempt": ordinal, "started_at": now()}
            marker = directory / "in-progress.json"
            write_json(marker, row)
            arm = task["arm"]
            try:
                backend = verified_worker_backend(output, run_id, case["target"])
                source = load_snapshot(output / case["snapshot"], case["sha256"])
                _, metrics = compiler.compile_trial(source, backend, task["seed"], arm)
                row.update(metrics)
                if arm == "decay" and task["seed"] == 0 and row["d2"] != case["stock"]["d2"]:
                    raise ValueError(f"Seed 0 D2 {row['d2']} differs from stock {case['stock']['d2']}")
                row["status"] = "ok"
            except Exception as exc:
                row.setdefault("seconds", getattr(exc, "seconds", 0.0))
                row.update(status="error", error=f"{type(exc).__name__}: {exc}")
            write_json(directory / f"attempt-{ordinal:04}.json", row)
            marker.unlink()


def canonical_rows(output, plan, tasks=None):
    rows, issues = [], {}
    for task in plan["tasks"] if tasks is None else tasks:
        try:
            attempts = read_attempts(task_dir(output, task), plan["run_id"], task)
            if not attempts:
                continue
            latest = attempts[-1].copy()
            if len(attempts) > 1:
                latest["previous_attempts"] = attempts[:-1]
            if any(a["status"] == "ok" for a in attempts[:-1]):
                raise ValueError(f"Duplicate trial after success: {task['task_id']}")
            rows.append(latest)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            issues.setdefault(task["case_id"], []).append(str(exc))
    return rows, issues


def monitor(output, plan, event):
    import json

    rows, issues = canonical_rows(output, plan)
    status = {"time": now(), "event": event, "ok": sum(r["status"] == "ok" for r in rows),
              "failed": sum(r["status"] == "error" for r in rows), "expected": len(plan["tasks"]),
              "issues": issues}
    with (output / "monitor.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(status) + "\n")
    print(f"{status['time']} {event}: {status['ok']}/{status['expected']} successful, "
          f"{status['failed']} failed", flush=True)
    return status


def host_memory_mib():
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024**2
    except (OSError, ValueError):
        return None


def size_from_pilot(rows, worker_limit):
    peak = max(row["peak_rss_mib"] for row in rows)
    slowest = max(row["seconds"] for row in rows)
    memory = host_memory_mib()
    # Each worker has a full Python runtime and backend. Reserve half of host RAM.
    worker_memory = max(512, math.ceil(peak * 1.5))
    workers = max(1, min(worker_limit, int(memory / 2 / worker_memory))) if memory else worker_limit
    # Keep dispatch overhead low and spread the most expensive cases across workers.
    size = max(2, min(24, 2 * int(90 / max(slowest, 0.01) / 2)))
    return {
        "pilot_trials": len(rows), "peak_worker_rss_mib": peak, "max_compile_seconds": slowest,
        "pilot_compile_seconds": sum(row["seconds"] for row in rows),
        "host_memory_mib": memory, "workers": workers, "trials_per_shard": size,
    }


def dispatch(output, plan, batches, settings, phase, next_check):
    """Feed a bounded pool. The heartbeat runs even if no batch has finished."""
    errors = []
    with ProcessPoolExecutor(max_workers=settings["workers"], initializer=compiler.configure_threads,
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = set()
        batch_iter = iter(batches)
        def fill():
            for case, tasks in batch_iter:
                job = pool.submit(execute_batch, output, plan["run_id"], case, tasks)
                futures.add(job)
                if len(futures) >= settings["workers"] * 2:
                    break
        try:
            fill()
            while futures:
                done, pending = wait(futures, timeout=max(0, next_check[0]-time.monotonic()),
                                     return_when=FIRST_COMPLETED)
                futures = pending
                for job in done:
                    try:
                        job.result()
                    except BrokenProcessPool:
                        raise
                    except Exception as exc:
                        errors.append(f"{type(exc).__name__}: {exc}")
                if time.monotonic() >= next_check[0]:
                    monitor(output, plan, phase)
                    next_check[0] = time.monotonic() + MONITOR_SECONDS
                fill()
        except BrokenProcessPool as exc:
            # A terminated process invalidates both queued futures and future
            # submissions. The phase retry starts a fresh pool from checkpoints.
            errors.append(f"BrokenProcessPool: {exc}")
    return errors


def run_phase(output, plan, seeds, settings, phase, next_check):
    phase_tasks = [t for t in plan["tasks"] if t["seed"] in seeds]
    for submission in range(2):
        rows, issues = canonical_rows(output, plan, phase_tasks)
        if issues:
            raise ValueError(f"Invalid task records: {issues}")
        ok = {r["task_id"] for r in rows if r["status"] == "ok"}
        if len(ok) == len(phase_tasks):
            return rows
        batches = []
        for case in plan["cases"]:
            missing = [t for t in tasks_for(case, seeds) if t["task_id"] not in ok]
            size = settings["trials_per_shard"]
            batches.extend((case, missing[i:i+size]) for i in range(0, len(missing), size))
        errors = dispatch(output, plan, batches, settings, phase, next_check)
        if errors:
            print("Batch errors:", *errors, sep="\n", flush=True)
    rows, issues = canonical_rows(output, plan, phase_tasks)
    if issues or sum(r["status"] == "ok" for r in rows) != len(phase_tasks):
        monitor(output, plan, f"{phase} incomplete")
        raise RuntimeError(f"{phase} incomplete after retry. Inspect _work/tasks and rerun.")
    return rows


def run(output, workers=None):
    plan = create_plan(output)
    cap = workers or max(1, int((os.cpu_count() or 1)*0.8))
    pilot = {"workers": min(4, cap), "trials_per_shard": 2}
    next_check = [time.monotonic() + MONITOR_SECONDS]
    plan.update(executor="local", worker_limit=cap, pilot_resources=pilot)
    write_json(output / "plan.json", plan)
    monitor(output, plan, "start/resume")
    started = time.monotonic()
    rows = run_phase(output, plan, PILOT_SEEDS, pilot, "pilot", next_check)
    settings = size_from_pilot(rows, cap)
    if "pilot" not in plan:
        settings["elapsed_seconds"] = time.monotonic() - started
    else:
        settings["elapsed_seconds"] = plan["pilot"]["elapsed_seconds"]
    plan["pilot"] = settings
    write_json(output / "plan.json", plan)
    monitor(output, plan, "pilot verified")
    print(f"Full matrix resources: {settings}", flush=True)
    run_phase(output, plan, SEEDS[3:], settings, "matrix", next_check)
    complete = collect(output)
    monitor(output, plan, "complete" if complete else "collection incomplete")
    return complete


def collect(output):
    from .report import write_report

    plan = read_json(output / "plan.json")
    rows, issues = canonical_rows(output, plan)
    write_json(output / "trials.json", rows)
    verified = {}
    for case in plan["cases"]:
        case_id = case["case_id"]
        problems = issues.get(case_id, [])
        subset = [r for r in rows if r["case_id"] == case_id]
        expected = {(arm, seed) for arm in ARMS for seed in plan["seeds"]}
        actual = {(r["arm"], r["seed"]) for r in subset if r["status"] == "ok"}
        if actual != expected or len(subset) != len(expected):
            problems.append(f"{len(actual)}/{len(expected)} distinct trials succeeded")
        if not problems:
            stock = next(r for r in subset if r["arm"] == "decay" and r["seed"] == 0)
            if stock["d2"] != case["stock"]["d2"]:
                problems.append("Seed 0 decay D2 differs from stock")
        verified[case_id] = {"complete": not problems, "issues": problems}
    return write_report(output, plan, rows, verified)["complete"]
