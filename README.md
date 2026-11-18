# Quantum Advantage Tracker routing experiment

This experiment compares two SABRE routing heuristics at equal search effort on published quantum circuits. Each arm compiles the same input at outer seeds 0–49 on the same device. The baseline uses decay (0.001, 5). The other arm uses wait time at weight 0.0005. Both use optimization level 3, four SABRE layout iterations and 20 internal layout and swap trials. The primary outcome is the lowest native two-qubit depth (D2) found in each set of 50 trials.

The matrix contains 15 distinct circuits and 18 circuit–target pairs. All compilation is offline after downloading the inputs and installing dependencies.

## Why these circuits and targets?

IBM FakeBoston has heavy-hex connectivity and FakeNighthawk has a square grid. Both use CZ gates, so we can measure two-qubit depth the same way on two different layouts. All 15 circuits can run on either target. We benchmark a pair only when every initial placement leaves some interacting qubits too far apart for a direct gate. The router then has to choose where to insert SWAPs to bring them together.

**How we calculate which pairs need routing.** The `prepare` command reads each circuit’s QASM and lists every pair of qubits that share a two-qubit gate. Repeated gates on the same pair count once because they need the same connection. Qiskit’s `VF2Layout` searches for a placement that maps each qubit to a different site and each listed pair to a native CZ connection. If the search finds a placement, we verify every pair and skip that case. If the search finds none, we require one of the checks below to prove that a SWAP is necessary. A search timeout alone leaves the case unresolved. [Routing screen implementation](src/quantum_advantage_reduce_depth/prepare.py)

| Selected circuits | Target benchmarked | Calculation from the circuit and target |
| --- | --- | --- |
| 3 peaked circuits (P9, P11, P12) | Both | Count each qubit’s distinct partners and take the largest count. P9 gives 42, P11 gives 33 and P12 gives 41. Even the smallest of these exceeds the most neighbors any site has on Boston (3) or Nighthawk (4). SWAPs bring successive partners into reach. |
| 11 Anderson impurity circuits | Boston | Look for two qubits with two common neighbors. Qubits 12 and 45 both interact with 13 and 44, forming the loop 12–13–45–44–12. Boston has no four-site loop on which to place them. On Nighthawk those four qubits fit around a square, and the placement search verifies the rest of the circuit fits too. |
| 1 random graph-state circuit with 27 checks | Nighthawk | Color Nighthawk’s grid like a checkerboard. Along the data chain (qubits 0–69), the 35 even and 35 odd qubits must occupy opposite colors. The QASM pairs 26 check qubits with even data qubits and 1 with an odd data qubit. Each check needs the color opposite its partner, so the totals are 35 + 26 = 61 and 35 + 1 = 36. The 10 × 12 grid has only 60 sites of each color. Boston has a verified placement for all 97 qubits. |

Counting target pairs gives 3 + 11 = 14 cases on Boston and 3 + 1 = 4 on Nighthawk. For each pair, `prepare` records the verified placement or the evidence that it needs SWAPs under `inputs[].screen` in `output/prepared.json`.

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

Each command accepts `--output DIRECTORY` (default `output`). Run `status` during compilation to read the latest persisted counts. Reissue `run` in the same directory after an interruption. Each completion is checkpointed and failed attempts remain under the original task identity. The coordinator retries outstanding work once automatically. An unsuccessful pilot holds the remainder of the matrix. Use a fresh output directory for new compiler settings or input bytes. Run `prepare` and `run` in the same environment. `run` rejects a preparation made with a different Python or package version.

For an LSF cluster, prepare on a shared filesystem and use `run --executor lsf --workers 12` there. The repository environment, run directory and Python executable must be available to the compute nodes at the same paths. LSF jobs request one core each. Queue, project, memory unit and submission executable can be set with `--lsf-queue`, `--lsf-project`, `--lsf-memory-unit` and `--bsub`. The default memory unit for this command is MB. Job scripts, submission records and logs are retained under `_work/lsf`.

## Read the results

After the run, the report links to the five figures, their CSV data and each winning circuit. [Generated report](output/report.md)

### How the two routing heuristics are compared

For each circuit on each target, compile it 50 times with decay (the baseline) and 50 times with wait time, using seeds 0–49 for both. Score each compilation by its native two-qubit depth (D2), the number of CZ gates along the longest dependency path. Independent CZ gates can run in parallel. Barriers synchronize their wires without adding depth. N2 is the total number of CZ gates.

**Best of 50.** Select the lowest D2 separately for each heuristic. Break ties within each set by lower N2, then lower seed. The two best circuits can therefore come from different seeds. Calculate the depth reduction as `100 × (best decay D2 − best wait-time D2) / best decay D2`. A positive value means wait time produced a shallower circuit. A negative value means decay did. If both D2 and N2 tie across heuristics, decay wins the tie.

For example, on **P9 on FakeBoston**, the best decay depth was 3,216 (seed 11) and the best wait-time depth was 2,349 (seed 0). The reduction was `100 × (3,216 − 2,349) / 3,216 = 26.96%`. Those same circuits used 21,626 and 22,091 CZ gates, respectively, so the shallower circuit used 465 more CZ gates. [Per-case results](output/tables/cases.csv)

**Seed by seed.** Apply the same percentage formula to the two depths at seed 0, then at seed 1, and so on. Report the median and interquartile range of those 50 percentages and count how often wait time produced a lower, equal or higher D2. For P9 on FakeBoston, wait time was shallower at all 50 seeds and the median reduction was 28.71%.

**Ratio of seed means.** For each circuit–target pair, take the arithmetic mean over all 50 seeds in each arm. Divide the wait-time mean by the decay mean, separately for D2 and N2. For P9 on FakeBoston, the D2 ratio is `2,438.06 / 3,424.56 = 0.7119` (28.81% shallower on average). The N2 ratio is `22,298.8 / 22,048.7 = 1.0113` (1.13% more CZ gates on average).

**Across cases.** For each target, family, family on a given target, or the full experiment, take each case’s best wait-time D2 divided by its best decay D2. Give each circuit–target pair equal weight and report `100 × (1 − geometric mean of those ratios)`. [Comparison implementation](src/quantum_advantage_reduce_depth/report.py)

Group summaries also report the geometric mean of the case-level ratios of seed means, giving each case equal weight. Across all 18 cases, the D2 ratio is 0.7814 (21.86% lower depth) and the N2 ratio is 1.0255 (2.55% more CZ gates). [Group results](output/tables/summaries.csv)

### Output files

| File | Contents |
| --- | --- |
| `prepared.json`, `inputs/` | Source inventory, raw QASM, checksummed snapshots, routing evidence and stock references |
| `plan.json` | Run identity, both target snapshots, compiler settings, environment, pilot resources and 1,800 planned task IDs |
| `trials.json` | Canonical trial rows, timings, errors and retry history, including logical-to-physical mappings |
| `results.json`, `tables/`, `figures/` | Case and group metrics plus the data underlying each figure |
| `best/<case>/` | Verified decay, weighted and overall winner QPYs |
| `monitor.jsonl`, `_work/tasks/` | Timestamped progress checks, durable attempts and candidate artifacts |

Collection checks each seed-0 baseline against its stock depth and reloads both arm winners to verify their metrics, physical gates, layout and measurement wiring. A missing, duplicated or failed trial withholds the case and every aggregate containing it. Small fixture tests check unitary equivalence up to layout and measurement-bit wiring.
