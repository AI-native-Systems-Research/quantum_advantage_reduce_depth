import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quantum_advantage_reduce_depth.lsf import submit


def test_submission_records_replayable_batch_and_pilot_resources(tmp_path):
    plan = {"run_id": "abcd"}
    options = SimpleNamespace(bsub="bsub", lsf_memory_unit="KB", lsf_project="p", lsf_queue="q")
    settings = {"lsf_memory_mib": 512, "lsf_wall_minutes": 5}
    task = {"task_id": "test.decay.00"}
    with patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
        submit(tmp_path, plan, {"case_id": "test"}, [task], "pilot", 0, settings, options)
    command, = run.call_args.args
    assert command[:2] == ["bsub", "-K"]
    assert command[command.index("-M") + 1] == str(512 * 1024)
    assert command[command.index("-q") + 1] == "q"
    folder, = (tmp_path / "_work" / "lsf").iterdir()
    spec = json.loads((folder / "batch.json").read_text())
    assert spec == {"run_id": "abcd", "case_id": "test", "task_ids": ["test.decay.00"]}
    assert str(Path(folder / "batch.json")) in (folder / "job.sh").read_text()
    assert json.loads((folder / "submission.json").read_text())["status"] == "done"
