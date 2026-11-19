# Routing Quantum Advantage Tracker circuits

Quantum advantage means a quantum computer solves a task with fewer resources than the best known classical method at comparable accuracy. Using circuits from the Quantum Advantage Tracker, we ask whether SABRE’s wait-time heuristic produces shallower circuits than decay when a device’s wiring forces the compiler to move qubits.

## Results by circuit family

We compiled 15 circuits for the FakeBoston and FakeNighthawk device models. Both heuristics used the same 50 seeds for each of 18 circuit–target pairs (1,800 compilations). **D2** counts CZ gates along the longest dependency path. **N2** counts all CZ gates.

| Circuit family | Target | Cases | D2 ratio | D2 improvement | N2 ratio | N2 improvement |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Peaked circuits (P9, P11, P12) | Boston and Nighthawk | 6 | 0.6085 | +39.15% | 1.0390 | −3.90% |
| Anderson impurity | Boston | 11 | 0.9006 | +9.94% | 1.0221 | −2.21% |
| Random graph state with 27 checks | Nighthawk | 1 | 0.7340 | +26.60% | 0.9839 | +1.61% |

## How the ratios are calculated

1. For each circuit–target pair, average D2 over the 50 seeds separately for wait time and decay. Divide the wait-time mean by the decay mean. Repeat for N2.
2. Multiply the case ratios within each family and take the nth root, where n is the number of circuit–target pairs. This geometric mean gives each pair equal weight.
3. Percentage improvement is `100 × (1 − family ratio)`. Positive means lower depth or fewer CZ gates with wait time. Calculate with full precision and round for display.

For example, the checked graph on Nighthawk is a one-case family. Its mean D2 was 286.52 with wait time and 390.36 with decay. The ratio is `286.52 / 390.36 ≈ 0.7340`, a **26.60% depth improvement**. Its mean CZ counts give `3,716.32 / 3,777.30 ≈ 0.9839`, a **1.61% gate-count improvement**.

## Why these circuits?

Each family has a wiring constraint that forces the router to insert SWAPs.

- **Peaked circuits.** Some qubits interact with as many as 42 partners. A physical site has at most three neighbors on Boston and four on Nighthawk. Tracker entries for [P9](https://quantum-advantage-tracker.github.io/trackers/classically-verifiable-problems?category=superseded&instance=peaked_circuit_P9_Hqap_56x1917), [P11](https://quantum-advantage-tracker.github.io/trackers/classically-verifiable-problems?instance=peaked_circuit_P11_Hqap_98x1999) and [P12](https://quantum-advantage-tracker.github.io/trackers/classically-verifiable-problems?instance=peaked_circuit_P12_Hqap_98x2457).
- **Anderson impurity.** The circuits need a four-site loop. Boston’s heavy-hex wiring has none. [Four-impurity tracker entry](https://quantum-advantage-tracker.github.io/trackers/variational-problems?category=baseline&instance=anderson_impurity_model_4i_28b_32e)
- **Checked graph state.** The circuit needs 61 sites of one checkerboard color on Nighthawk’s grid, which has only 60 of each color. [Random graph sampling tracker entry](https://quantum-advantage-tracker.github.io/trackers/classically-verifiable-problems?instance=random_graph_sampling_nq70_depth70_checks27_basis_fstate)


## Run locally

With uv, Git, Rust and a C/C++ compiler installed, run from the repository root.

```bash
uv sync --locked --python 3.12
source .venv/bin/activate
quantum-advantage-reduce-depth prepare
quantum-advantage-reduce-depth run
```

The run writes a [family report](output/README.md) and [CSV results](output/families.csv). For another fresh run, pass `--output NEW_DIRECTORY` to both `prepare` and `run`.



## Why other circuits were excluded

The pinned tracker repository contained 39 distinct QASM files. We used 15. Of the other 24, 19 had interactions that fit both devices without added SWAPs, four repeated a selected routing pattern, and one failed to import. Where an instance has no submissions, the link opens its record in the tracker’s circuit catalog.

| Family | Files excluded | Reason | Tracker entries |
| --- | ---: | --- | --- |
| Floquet mixed-field Ising | 3 | The heavy-hex interactions fit both devices. The three observable labels for 51 qubits and 16 cycles share one circuit file by content. | [51×16 ZZ distance 3](https://quantum-advantage-tracker.github.io/trackers/observable-estimations?instance=floquet_mixed_field_ising_zzd3_51qx16c) · [51×16 magnetization](https://github.com/quantum-advantage-tracker/quantum-advantage-tracker.github.io/blob/4d27f882d55028162206c4f12b5b6fc6a63868e7/data/observable-estimations/circuit-models.json#L55) · [51×16 ZZ distance 2](https://github.com/quantum-advantage-tracker/quantum-advantage-tracker.github.io/blob/4d27f882d55028162206c4f12b5b6fc6a63868e7/data/observable-estimations/circuit-models.json#L64) · [51×30](https://github.com/quantum-advantage-tracker/quantum-advantage-tracker.github.io/blob/4d27f882d55028162206c4f12b5b6fc6a63868e7/data/observable-estimations/circuit-models.json#L82) · [74×27](https://github.com/quantum-advantage-tracker/quantum-advantage-tracker.github.io/blob/4d27f882d55028162206c4f12b5b6fc6a63868e7/data/observable-estimations/circuit-models.json#L91) |
| Operator Loschmidt echo | 9 | The 49–70 active qubits fit both devices without SWAPs. The files declare 156 wires, more than Nighthawk’s 120. The 56-qubit case has six angle variants. | [49×648](https://quantum-advantage-tracker.github.io/trackers/observable-estimations?category=baseline&instance=operator_loschmidt_echo_49x648) · [49×1296](https://quantum-advantage-tracker.github.io/trackers/observable-estimations?instance=operator_loschmidt_echo_49x1296) · [70×1872](https://quantum-advantage-tracker.github.io/trackers/observable-estimations?instance=operator_loschmidt_echo_70x1872) · [56×1488, α = 0.15](https://github.com/quantum-advantage-tracker/quantum-advantage-tracker.github.io/blob/4d27f882d55028162206c4f12b5b6fc6a63868e7/data/observable-estimations/circuit-models.json#L26) · [All six 56-qubit files](https://github.com/quantum-advantage-tracker/quantum-advantage-tracker.github.io/tree/4d27f882d55028162206c4f12b5b6fc6a63868e7/data/observable-estimations/circuit-models/operator_loschmidt_echo) |
| SU(2) hadron dynamics | 2 | Both circuits use a 120-qubit chain that fits both devices. The SWAPs already in the input need no additional routing. | [Meson](https://quantum-advantage-tracker.github.io/trackers/observable-estimations?instance=su2_hadron_dynamics_lsh_x_100_meson) · [SCV](https://github.com/quantum-advantage-tracker/quantum-advantage-tracker.github.io/blob/4d27f882d55028162206c4f12b5b6fc6a63868e7/data/observable-estimations/circuit-models.json#L37) |
| Heavy-hex peaked circuits | 2 | The 49-qubit patch fits both devices without SWAPs. | [49×4020](https://quantum-advantage-tracker.github.io/trackers/classically-verifiable-problems?category=superseded&instance=peaked_circuit_heavy_hex_49x4020) · [49×5072](https://quantum-advantage-tracker.github.io/trackers/classically-verifiable-problems?category=superseded&instance=peaked_circuit_heavy_hex_49x5072) |
| Random graph sampling | 1 | The unchecked 70-qubit chain fits both devices. The checked version fits Boston and was benchmarked on Nighthawk. | [70-qubit graph and checked companion](https://quantum-advantage-tracker.github.io/trackers/classically-verifiable-problems?instance=random_graph_sampling_nq70_depth70_checks27_basis_fstate) |
| Doped random graph sampling | 2 | The unchecked chain fits both devices. The checked version repeats the selected graph circuit’s two-qubit interaction pattern. | [Doped graph and checked companion](https://quantum-advantage-tracker.github.io/trackers/classically-verifiable-problems?instance=doped_random_graph_sampling_nq70_depth70_checks27) |
| Anderson impurity | 4 | The zero-step circuit fits both devices. Three double-angle circuits repeat the interactions of the corresponding `NOs_1` circuits. The 11 selected circuits also fit Nighthawk directly, so they were benchmarked on Boston only. | [Four-impurity instance](https://quantum-advantage-tracker.github.io/trackers/variational-problems?category=baseline&instance=anderson_impurity_model_4i_28b_32e) |
| 4Fe–4S | 1 | The pinned OpenQASM 3 importer rejects the circuit’s empty, zero-qubit `global_phase` gate definition. | [4Fe–4S instance](https://quantum-advantage-tracker.github.io/trackers/variational-problems?category=baseline&instance=4fe_4s_54e_36o) |

**Circuits published as submission attachments.** The inventory counted QASM files committed to the tracker repository. It omitted one attached circuit for [2Fe–2S](https://quantum-advantage-tracker.github.io/trackers/variational-problems?category=baseline&instance=2fe_2s_30e_20o), one for [N₂ at 3.00 Å](https://quantum-advantage-tracker.github.io/trackers/variational-problems?category=baseline&instance=n2_3-00_14e_28o), and 60 for the [49-qubit guided sparse ground-state problem](https://quantum-advantage-tracker.github.io/trackers/variational-problems?category=baseline&instance=guided_sparse_ground_state_problem_49Q_v1). Their routing requirements were not tested.



