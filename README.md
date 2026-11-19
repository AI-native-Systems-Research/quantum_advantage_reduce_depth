# Routing Quantum Advantage Tracker circuits

Quantum advantage means a quantum computer solves a task with fewer resources than the best known classical method at comparable accuracy. Using circuits from the Quantum Advantage Tracker, we ask whether SABRE’s wait-time heuristic produces shallower circuits than decay when a device’s wiring forces the compiler to move qubits.

## Results by circuit family

We compiled 15 circuits for the FakeBoston and FakeNighthawk device models. Both heuristics used the same 50 seeds for each of 18 circuit–target pairs (1,800 compilations). **D2** counts CZ gates along the longest dependency path. **N2** counts all CZ gates. For each pair, we divide the wait-time mean across seeds by the decay mean. Family ratios are geometric means of the case ratios. Values below 1 favor wait time.

| Circuit family | Target | Cases | D2 ratio | D2 improvement | N2 ratio | N2 improvement |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Peaked circuits (P9, P11, P12) | Boston and Nighthawk | 6 | 0.6085 | +39.15% | 1.0390 | −3.90% |
| Anderson impurity | Boston | 11 | 0.9006 | +9.94% | 1.0221 | −2.21% |
| Random graph state with 27 checks | Nighthawk | 1 | 0.7340 | +26.60% | 0.9839 | +1.61% |

## Why these circuits?

Each family has a wiring constraint that forces the router to insert SWAPs.

- **Peaked circuits.** Some qubits interact with as many as 42 partners. A physical site has at most three neighbors on Boston and four on Nighthawk.
- **Anderson impurity.** The circuits need a four-site loop. Boston’s heavy-hex wiring has none.
- **Checked graph state.** The circuit needs 61 sites of one checkerboard color on Nighthawk’s grid, which has only 60 of each color.

## Why other circuits were excluded

Of the 39 distinct circuit files, 19 have interactions that fit both devices directly, including Floquet Ising and unchecked graph states. Four repeat a selected interaction pattern, and the 4Fe–4S circuit fails to import. Hamiltonian-only entries supply no circuit to route. [Circuit inventory](src/quantum_advantage_reduce_depth/manifest.json)

## Run locally

With uv, Git, Rust and a C/C++ compiler installed, run from the repository root.

```bash
uv sync --locked --python 3.12
source .venv/bin/activate
quantum-advantage-reduce-depth prepare
quantum-advantage-reduce-depth run
```

The run writes a [family report](output/README.md) and [CSV results](output/families.csv). For another fresh run, pass `--output NEW_DIRECTORY` to both `prepare` and `run`.
