"""Run a fixed paired matrix, checkpoint each attempt, and verify the winners."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from functools import lru_cache
import math
import multiprocessing
import os
from pathlib import Path
import shutil
import time
import uuid

from . import compiler
from .storage import atomic_write, read_json, sha256, write_json

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
    fingerprint = {
        "prepared_sha256": sha256(output / "prepared.json"),
        "compiler_sha256": sha256(Path(compiler.__file__)),
        "runtime": compiler.runtime_metadata(),
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


def rank(row):
    return row["d2"], row["n2"], row["seed"]


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
    from qiskit import qpy

    output = Path(output)
    minima = {}
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
                compiled, metrics = compiler.compile_trial(source, backend, task["seed"], arm)
                row.update(metrics)
                if arm == "decay" and task["seed"] == 0 and row["d2"] != case["stock"]["d2"]:
                    raise ValueError(f"Seed 0 D2 {row['d2']} differs from stock {case['stock']['d2']}")
                if arm not in minima or rank(row) < minima[arm]:
                    artifact = directory / f"candidate-{ordinal:04}.qpy"
                    atomic_write(artifact, lambda stream: qpy.dump(compiled, stream), binary=True)
                    row.update(artifact=artifact.relative_to(output).as_posix(),
                               artifact_sha256=sha256(artifact))
                row["status"] = "ok"
            except Exception as exc:
                row.setdefault("seconds", getattr(exc, "seconds", 0.0))
                row.update(status="error", error=f"{type(exc).__name__}: {exc}")
            write_json(directory / f"attempt-{ordinal:04}.json", row)
            marker.unlink()
            if row["status"] == "ok" and "artifact" in row:
                minima[arm] = rank(row)


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
        "lsf_memory_mib": worker_memory, "lsf_wall_minutes": max(5, math.ceil(2*slowest*size/60)+2),
    }


def dispatch(output, plan, batches, settings, options, phase, next_check):
    """Feed a bounded pool. The heartbeat runs even if no batch has finished."""
    from .lsf import submit

    if options.executor == "local":
        pool = ProcessPoolExecutor(max_workers=settings["workers"],
                                   initializer=compiler.configure_threads,
                                   mp_context=multiprocessing.get_context("spawn"))
    else:
        pool = ThreadPoolExecutor(max_workers=settings["workers"])
    errors = []
    with pool:
        futures = set()
        batch_iter = iter(enumerate(batches))
        def fill():
            for index, (case, tasks) in batch_iter:
                if options.executor == "local":
                    job = pool.submit(execute_batch, output, plan["run_id"], case, tasks)
                else:
                    job = pool.submit(submit, output, plan, case, tasks, phase, index, settings,
                                      options)
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


def run_phase(output, plan, seeds, settings, options, phase, next_check):
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
        errors = dispatch(output, plan, batches, settings, options, phase, next_check)
        if errors:
            print("Batch errors:", *errors, sep="\n", flush=True)
    rows, issues = canonical_rows(output, plan, phase_tasks)
    if issues or sum(r["status"] == "ok" for r in rows) != len(phase_tasks):
        monitor(output, plan, f"{phase} incomplete")
        raise RuntimeError(f"{phase} incomplete after retry. Inspect _work/tasks and rerun.")
    return rows


def run(output, options):
    plan = create_plan(output)
    cap = options.workers or max(1, int((os.cpu_count() or 1)*0.8))
    pilot = {"workers": min(4, cap), "trials_per_shard": 2,
             "lsf_memory_mib": 2048, "lsf_wall_minutes": 10}
    if options.executor == "lsf":
        from .lsf import check_lsf
        check_lsf(options)
    next_check = [time.monotonic() + MONITOR_SECONDS]
    plan.update(executor=options.executor, worker_limit=cap, pilot_resources=pilot)
    if options.executor == "lsf":
        plan["lsf"] = {"bsub": options.bsub, "queue": options.lsf_queue,
                       "project": options.lsf_project, "memory_unit": options.lsf_memory_unit}
    write_json(output / "plan.json", plan)
    monitor(output, plan, "start/resume")
    started = time.monotonic()
    rows = run_phase(output, plan, PILOT_SEEDS, pilot, options, "pilot", next_check)
    settings = size_from_pilot(rows, cap)
    if "pilot" not in plan:
        settings["elapsed_seconds"] = time.monotonic() - started
    else:
        settings["elapsed_seconds"] = plan["pilot"]["elapsed_seconds"]
    plan["pilot"] = settings
    write_json(output / "plan.json", plan)
    monitor(output, plan, "pilot verified")
    print(f"Full matrix resources: {settings}", flush=True)
    run_phase(output, plan, SEEDS[3:], settings, options, "matrix", next_check)
    complete = collect(output, repair=True)
    monitor(output, plan, "complete" if complete else "collection incomplete")
    return complete


def copy_atomic(source, destination):
    def copy(stream):
        with Path(source).open("rb") as reader:
            shutil.copyfileobj(reader, stream)
    atomic_write(destination, copy, binary=True)


def verified_candidate(output, backend, source, row, repair):
    from qiskit import qpy

    directory = task_dir(output, row)
    path = output / row["artifact"]
    if path.resolve().parent != directory.resolve():
        raise ValueError("Candidate QPY outside the matching task directory")
    recovered = directory / "recovered.qpy"
    recovery = directory / "recovery.json"
    for candidate, digest in [(path, row["artifact_sha256"])] + (
        [(recovered, read_json(recovery)["sha256"])] if recovery.exists() else []
    ):
        if candidate.is_file() and sha256(candidate) == digest:
            with candidate.open("rb") as stream:
                compiled, = qpy.load(stream)
            compiler.verify_winner(compiled, source, backend, row)
            return candidate
    if not repair:
        raise ValueError(f"Missing or damaged winning artifact for {row['task_id']}")
    # A replay restores an artifact. Its measurements never enter the trial budget.
    compiled, metrics = compiler.compile_trial(source, backend, row["seed"], row["arm"])
    compiler.verify_winner(compiled, source, backend, row)
    atomic_write(recovered, lambda stream: qpy.dump(compiled, stream), binary=True)
    with recovered.open("rb") as stream:
        restored, = qpy.load(stream)
    compiler.verify_winner(restored, source, backend, row)
    write_json(recovery, {"time": now(), "task_id": row["task_id"], "run_id": row["run_id"],
                          "original_sha256": row["artifact_sha256"], "sha256": sha256(recovered),
                          "replay_seconds": metrics["seconds"]})
    return recovered


def collect(output, repair=False):
    from .report import write_report

    plan = read_json(output / "plan.json")
    rows, issues = canonical_rows(output, plan)
    write_json(output / "trials.json", rows)
    verified = {}
    for case in plan["cases"]:
        case_id = case["case_id"]
        problems = issues.get(case_id, [])
        subset = [r for r in rows if r["case_id"] == case_id]
        files = {}
        expected = {(arm, seed) for arm in ARMS for seed in plan["seeds"]}
        actual = {(r["arm"], r["seed"]) for r in subset if r["status"] == "ok"}
        if actual != expected or len(subset) != len(expected):
            problems.append(f"{len(actual)}/{len(expected)} distinct trials succeeded")
        if not problems:
            try:
                stock_row = next(r for r in subset if r["arm"] == "decay" and r["seed"] == 0)
                if stock_row["d2"] != case["stock"]["d2"]:
                    raise ValueError("Seed 0 decay D2 differs from stock")
                source = load_snapshot(output / case["snapshot"], case["sha256"])
                backend = backend_for(case["target"])
                winners = {}
                candidates = {}
                for arm in ARMS:
                    row = min((r for r in subset if r["arm"] == arm), key=rank)
                    candidates[arm] = verified_candidate(output, backend, source, row, repair)
                    winners[arm] = row
                # ARMS order gives decay the exact D2/N2 tie, independent of seed.
                winner_arm = min(ARMS, key=lambda a: (winners[a]["d2"], winners[a]["n2"]))
                for name, candidate in {**candidates, "overall": candidates[winner_arm]}.items():
                    relative = f"best/{case_id}/{name}.qpy"
                    copy_atomic(candidate, output / relative)
                    files[name] = relative
            except Exception as exc:
                # QPY parsing also raises Qiskit-specific exceptions. Keep other
                # independently verified cases publishable if one file is bad.
                problems.append(f"Winner validation failed: {type(exc).__name__}: {exc}")
        if problems:
            for name in (*ARMS, "overall"):
                (output / "best" / case_id / f"{name}.qpy").unlink(missing_ok=True)
            files.clear()
        verified[case_id] = {"complete": not problems, "issues": problems, "artifacts": files}
    return write_report(output, plan, rows, verified)["complete"]
