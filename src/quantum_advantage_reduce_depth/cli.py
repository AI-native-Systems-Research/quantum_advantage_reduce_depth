"""Command line for preparation, paired compilation, monitoring and collection."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from .compiler import configure_threads
from .storage import read_json


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="quantum-advantage-reduce-depth")
    sub = parser.add_subparsers(dest="command", required=True)
    descriptions = {
        "prepare": "Verify tracker inputs, create snapshots and screen both devices",
        "run": "Run or resume the 108-trial pilot and the full paired matrix",
        "collect": "Verify completed cases and rebuild the reports and figures",
        "status": "Show checkpointed progress without interrupting a run",
        "worker": "Execute a batch assigned by the LSF coordinator",
    }
    for name, help_text in descriptions.items():
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--output", type=Path, default=Path("output"))
        if name == "prepare":
            cmd.add_argument("--source", type=Path, help="Use a local tracker checkout")
        if name == "run":
            cmd.add_argument("--workers", type=int, help="Maximum concurrent workers or LSF jobs")
            cmd.add_argument("--executor", choices=["local", "lsf"], default="local")
            cmd.add_argument("--bsub", default="bsub")
            cmd.add_argument("--lsf-queue")
            cmd.add_argument("--lsf-project")
            cmd.add_argument("--lsf-memory-unit", choices=["KB", "MB", "GB"], default="MB")
        if name == "worker":
            cmd.add_argument("--batch", required=True, type=Path)
    options = parser.parse_args(argv)
    if getattr(options, "workers", None) is not None and options.workers < 1:
        parser.error("--workers must be positive")
    return options


def main(argv=None):
    configure_threads()
    from . import sweep

    options = parse_args(argv)
    output = options.output.resolve()
    try:
        if options.command == "worker":
            from .lsf import worker
            worker(output, options.batch)
            return 0
        if options.command == "status":
            plan = read_json(output / "plan.json")
            rows, issues = sweep.canonical_rows(output, plan)
            print(json.dumps({"run_id": plan["run_id"], "expected": len(plan["tasks"]),
                              "counts": dict(Counter(r["status"] for r in rows)),
                              "retried": sum("previous_attempts" in r for r in rows),
                              "issues": issues}, indent=2))
            return 0
        with sweep.lock(output / "_work" / "coordinator.lock"):
            if options.command == "prepare":
                from .prepare import prepare
                if (output / "plan.json").exists():
                    raise ValueError("This directory already contains a run. Use run to resume "
                                     "or prepare into a new directory.")
                prepared = prepare(output, options.source)
                print(f"Prepared {len(prepared['cases'])} cases in {output}")
                return 0
            if options.command == "run":
                if not (output / "prepared.json").exists():
                    raise ValueError("Run prepare first to verify source identities and screen targets")
                try:
                    return 0 if sweep.run(output, options) else 1
                except RuntimeError:
                    if (output / "plan.json").exists():
                        sweep.collect(output)
                    raise
            return 0 if sweep.collect(output) else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
