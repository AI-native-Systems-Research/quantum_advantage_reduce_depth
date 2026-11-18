# Quantum Advantage Tracker routing experiment

This experiment compares two SABRE routing heuristics at equal search effort on published quantum circuits. Each arm compiles the same input at outer seeds 0–49 on the same device. The baseline uses decay (0.001, 5). The other arm uses wait time at weight 0.0005. Both use optimization level 3, four SABRE layout iterations and 20 internal layout and swap trials. The primary outcome is the lowest native two-qubit depth (D2) found in each set of 50 trials.

The matrix contains 15 distinct circuits and 18 circuit–target pairs. All compilation is offline after downloading the inputs and installing dependencies.

## Why these circuits and targets?

IBM FakeBoston has heavy-hex connectivity and FakeNighthawk has a square grid. Both use CZ gates, so we can measure two-qubit depth the same way on two different layouts. All 15 circuits can run on either target. We benchmark a pair only when every initial placement leaves some interacting qubits too far apart for a direct gate. The router then has to choose where to insert SWAPs to bring them together.

| Selected circuits | Target benchmarked | Why the compiler must add SWAPs |
| --- | --- | --- |
| 3 peaked circuits (P9, P11, P12) | Both | One qubit needs gates with at least 33 different partners. Wherever it starts, at most 3 partners on Boston or 4 on Nighthawk can sit next to it at once. Both targets need SWAPs to bring the remaining partners within reach. |
| 11 Anderson impurity circuits | Boston | Four qubits need gates on the pairs A–B, B–C, C–D and D–A. On Nighthawk they can sit at the four corners of one square, and the rest of the circuit fits too. Boston has no four-site loop, so at least one of those gates needs a SWAP to bring its pair together. |
| 1 random graph-state circuit with 27 checks | Nighthawk | Picture Nighthawk’s grid as a checkerboard. The chain of 70 data qubits alternates colors, using 35 sites of each color. To sit beside their partners, 26 of the check qubits need the same color. That takes 35 + 26 = 61 sites of one color, but the grid has only 60. Boston has a layout where all interacting pairs are already neighbors. |

The tracker contains 39 distinct QASM files after counting byte-identical copies once. The other 24 were excluded for three reasons. [Full circuit inventory](src/quantum_advantage_reduce_depth/manifest.json)

- **19 need no added SWAPs on either target.** The active qubits fit directly for Floquet Ising, operator Loschmidt echo, SU(2) hadron dynamics, the two heavy-hex peaked circuits, random and doped graph sampling without checks, and zero-step Anderson. The echo files also declare 156 wires, exceeding Nighthawk’s 120, although only 49–70 are active.
- **4 repeat a selected routing problem.** The checked doped graph circuit has the same two-qubit interactions as the selected checked graph circuit. The three Anderson `double_angles` files repeat the interactions of their `NOs_1` counterparts at the same step counts.
- **1 fails to import.** The 4Fe–4S file defines an empty, zero-qubit `global_phase` gate that the pinned QASM importer cannot read.

Tracker entries that supply only a Hamiltonian have no circuit to route.

## Install and run

Use Python 3.12, uv, Git, Rust and a C/C++ compiler. The lockfile pins the wait-time-enabled Qiskit fork and the IBM device snapshots.

```bash
CARGO_BUILD_JOBS=4 uv sync --locked --extra dev --python 3.12
source .venv/bin/activate
quantum-advantage-reduce-depth prepare
quantum-advantage-reduce-depth run
```

Preparation downloads the raw files at tracker commit `4d27f882d55028162206c4f12b5b6fc6a63868e7`. To use a local checkout, pass `prepare --source /path/to/quantum-advantage-tracker.github.io`. The manifest records Git blob IDs, SHA-256 hashes, paths and aliases for all 39 distinct QASM files. Preparation verifies the selected 15, keeps their raw bytes, checks each parsed circuit after a QPY round trip, screens all 30 circuit–target pairs and compiles the stock seed-0 references. Every admitted case has a structural obstruction to a SWAP-free placement. [Source manifest](src/quantum_advantage_reduce_depth/manifest.json) · [Tracker license](src/quantum_advantage_reduce_depth/tracker-LICENSE)

The coordinator runs seeds 0–2 first as a 108-trial pilot. It uses measured peak worker RSS and slowest compile time to choose full-run concurrency, batch size and LSF resource requests. Peak RSS is a process high-water mark in MiB. The pilot counts toward the 1,800 total trials. Use `run --workers N` to cap concurrency. Every worker uses one thread. The coordinator records progress every 20 minutes and at phase boundaries in `output/monitor.jsonl`.

```bash
quantum-advantage-reduce-depth status
quantum-advantage-reduce-depth collect
pytest -q
```

Each command accepts `--output DIRECTORY` (default `output`). Run `status` during compilation to read the latest persisted counts. Reissue `run` in the same directory after an interruption. Each completion is checkpointed and failed attempts remain under the original task identity. The coordinator retries outstanding work once automatically. An unsuccessful pilot holds the remainder of the matrix. Use a fresh output directory for new compiler settings or input bytes.

For an LSF cluster, prepare on a shared filesystem and use `run --executor lsf --workers 12` there. The repository environment, run directory and Python executable must be available to the compute nodes at the same paths. LSF jobs request one core each. Queue, project, memory unit and submission executable can be set with `--lsf-queue`, `--lsf-project`, `--lsf-memory-unit` and `--bsub`. The default memory unit for this command is MB. Job scripts, submission records and logs are retained under `_work/lsf`.

## Read the results

Open [the generated report](output/report.md) after the run. It links to the five figures, their CSV data and each winning circuit. A positive D2 reduction means the weighted winner is shallower. A positive N2 change means it uses more native CZ gates. The report gives best-of-50 and paired-seed results for individual cases, then summarizes cases by target, family, family within target and overall.

| File | Contents |
| --- | --- |
| `prepared.json`, `inputs/` | Source inventory, raw QASM, checksummed snapshots, routing evidence and stock references |
| `plan.json` | Run identity, both target snapshots, compiler settings, environment, pilot resources and 1,800 planned task IDs |
| `trials.json` | Canonical trial rows, timings, errors and retry history, including logical-to-physical mappings |
| `results.json`, `tables/`, `figures/` | Case and group metrics plus the data underlying each figure |
| `best/<case>/` | Verified decay, weighted and overall winner QPYs |
| `monitor.jsonl`, `_work/tasks/` | Timestamped progress checks, durable attempts and candidate artifacts |

D2 counts native two-qubit gates along the longest dependency path. Barriers synchronize their wires at zero cost. N2 counts native CZs. Winners minimize D2, then N2, then seed within an arm. An exact D2/N2 tie between arms selects decay. Collection checks each seed-0 baseline against its stock depth and reloads both arm winners to verify their metrics, physical gates, layout and measurement wiring. A missing, duplicated or failed trial withholds the case and every aggregate containing it. Small fixture tests check unitary equivalence up to layout and measurement-bit wiring.
