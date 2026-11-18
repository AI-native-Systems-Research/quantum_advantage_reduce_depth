"""Submit bounded one-core jobs to LSF using the reference blocking bsub protocol."""

from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import uuid

from .storage import read_json, write_json


def check_lsf(options):
    if shutil.which(options.bsub) is None:
        raise ValueError(f"LSF submission executable not found: {options.bsub}")


def submit(output, plan, case, tasks, phase, index, settings, options):
    job_dir = output / "_work" / "lsf" / f"{phase}-{index:04}-{uuid.uuid4().hex[:8]}"
    job_dir.mkdir(parents=True)
    spec = job_dir / "batch.json"
    write_json(spec, {"run_id": plan["run_id"], "case_id": case["case_id"],
                      "task_ids": [t["task_id"] for t in tasks]})
    unit = options.lsf_memory_unit
    memory = settings["lsf_memory_mib"]
    limit = {"KB": memory * 1024, "MB": memory, "GB": (memory + 1023) // 1024}[unit]
    argv = [
        options.bsub, "-K", "-J", f"qat-{plan['run_id'][:10]}-{phase}-{index}",
        "-n", "1", "-W", str(settings["lsf_wall_minutes"]), "-M", str(limit),
        "-R", f"rusage[mem={limit}] span[hosts=1]", "-cwd", str(output),
        "-oo", str(job_dir / "stdout.log"), "-eo", str(job_dir / "stderr.log"),
    ]
    if options.lsf_queue:
        argv.extend(["-q", options.lsf_queue])
    if options.lsf_project:
        argv.extend(["-P", options.lsf_project])
    call = [sys.executable, "-m", "quantum_advantage_reduce_depth", "worker",
            "--output", str(output), "--batch", str(spec)]
    script = job_dir / "job.sh"
    script.write_text("#!/bin/sh\nset -eu\nexec " + shlex.join(call) + "\n", encoding="utf-8")
    audit = {"command": argv, "status": "submitting"}
    write_json(job_dir / "submission.json", audit)
    with script.open("rb") as source, (job_dir / "bsub.log").open("wb") as log:
        result = subprocess.run(argv, stdin=source, stdout=log, stderr=subprocess.STDOUT)
    write_json(job_dir / "submission.json", {**audit, "returncode": result.returncode,
                                            "status": "failed" if result.returncode else "done"})
    if result.returncode:
        raise RuntimeError(f"LSF job failed. See {job_dir / 'bsub.log'}")


def worker(output, batch):
    from .sweep import execute_batch

    plan = read_json(output / "plan.json")
    spec = read_json(Path(batch))
    if spec["run_id"] != plan["run_id"]:
        raise ValueError("Batch run ID differs from plan")
    case = next(c for c in plan["cases"] if c["case_id"] == spec["case_id"])
    ids = set(spec["task_ids"])
    tasks = [t for t in plan["tasks"] if t["case_id"] == case["case_id"] and t["task_id"] in ids]
    if len(tasks) != len(spec["task_ids"]):
        raise ValueError("Unknown or duplicated task in batch")
    execute_batch(output, plan["run_id"], case, tasks)
