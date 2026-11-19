"""Compile and verify circuits on the pinned IBM target snapshots."""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import resource
import sys
import time

QISKIT_REVISION = "9dedcf6f64a66344cb31364e846f71d020adcf9d"
RUNTIME_VERSION = "0.52.1"
IMPORTER_VERSION = "0.6.0"
WEIGHT = 0.0005
SABRE_TRIALS = 20
SABRE_ITERATIONS = 4
TARGET_NAMES = ("FakeBoston", "FakeNighthawk")
THREAD_LIMITS = {
    "RAYON_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1", "QISKIT_NUM_PROCS": "1",
    "QISKIT_PARALLEL": "FALSE",
}


def configure_threads():
    """Call before importing Qiskit or NumPy, including in each spawned worker."""
    os.environ.update(THREAD_LIMITS)


def runtime_metadata():
    packages = {name: metadata.version(name) for name in (
        "qiskit", "qiskit-ibm-runtime", "qiskit-qasm3-import", "numpy", "rustworkx",
        "scipy", "sympy",
    )}
    source = json.loads(metadata.distribution("qiskit").read_text("direct_url.json") or "{}")
    revision = source.get("vcs_info", {}).get("commit_id")
    if revision != QISKIT_REVISION:
        raise RuntimeError(f"Install the pinned Qiskit fork at {QISKIT_REVISION}; found {source}")
    for package, expected in [("qiskit-ibm-runtime", RUNTIME_VERSION),
                              ("qiskit-qasm3-import", IMPORTER_VERSION)]:
        if packages[package] != expected:
            raise RuntimeError(f"{package} {expected} required, found {packages[package]}")
    return {
        "python": platform.python_version(), "platform": platform.platform(),
        "packages": packages, "qiskit_revision": revision,
        "thread_limits": {key: os.environ.get(key) for key in THREAD_LIMITS},
    }


def resolve_target(name):
    from qiskit_ibm_runtime import fake_provider

    name = name.removeprefix("qiskit_ibm_runtime.fake_provider:")
    if name not in TARGET_NAMES:
        raise ValueError(f"Unknown study target {name!r}, expected one of {TARGET_NAMES}")
    return getattr(fake_provider, name)()


def target_metadata(backend):
    target = backend.target
    edges = sorted(target.build_coupling_map().get_edges())
    allowed = {}
    for name in sorted(target.operation_names):
        locations = target.qargs_for_operation_name(name)
        allowed[name] = None if locations is None else sorted(map(list, locations))
    config_path = Path(backend.dirname) / backend.conf_filename
    props_path = Path(backend.dirname) / backend.props_filename
    return {
        "factory": "qiskit_ibm_runtime.fake_provider:" + type(backend).__name__,
        "name": backend.name, "backend_version": backend.backend_version,
        "num_qubits": target.num_qubits, "basis": sorted(target.operation_names),
        "coupling_map": list(map(list, edges)),
        "cz_edges": len({tuple(sorted(pair)) for pair in target["cz"]}),
        "allowed_qargs": allowed,
        "configuration_sha256": sha256(config_path.read_bytes()).hexdigest(),
        "properties_sha256": sha256(props_path.read_bytes()).hexdigest(),
    }


def make_manager(backend, seed, arm):
    """Preserve the reference compiler's search effort and width-scaled scoring."""
    from qiskit._accelerate.sabre import Heuristic, SetScaling
    from qiskit.transpiler import PassManager
    from qiskit.transpiler.passes import SabreLayout
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

    width = backend.target.num_qubits
    heuristic = (Heuristic(attempt_limit=10 * width)
                 .with_basic(1.0, SetScaling.Constant)
                 .with_lookahead([0.5 / width], SetScaling.Constant))
    if arm == "decay":
        heuristic = heuristic.with_decay(0.001, 5)
    elif arm == "weighted":
        heuristic = heuristic.with_wait_time(WEIGHT)
    else:
        raise ValueError(f"Unknown SABRE arm {arm!r}")
    manager = generate_preset_pass_manager(
        backend=backend, optimization_level=3, seed_transpiler=seed, routing_method="none",
    )
    manager.layout = PassManager([SabreLayout(
        backend.target, heuristic=heuristic, seed=seed, max_iterations=SABRE_ITERATIONS,
        layout_trials=SABRE_TRIALS, swap_trials=SABRE_TRIALS,
    )])
    return manager


def compile_stock(circuit, backend):
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

    manager = generate_preset_pass_manager(backend=backend, optimization_level=3, seed_transpiler=0)
    compiled = manager.run(circuit)
    validate_target(compiled, backend)
    return compiled


def validate_target(circuit, backend):
    """Validate directed placements for each distinct native operation and qarg tuple."""
    if circuit.num_qubits > backend.target.num_qubits:
        raise ValueError(f"Circuit width {circuit.num_qubits} exceeds {backend.name} width")
    qubits = {q: i for i, q in enumerate(circuit.qubits)}
    seen = set()
    for item in circuit.data:
        name = item.name
        if name == "barrier":
            continue
        indices = tuple(qubits[q] for q in item.qubits)
        key = name, indices
        if key in seen:
            continue
        if not backend.target.instruction_supported(operation_name=name, qargs=indices):
            raise ValueError(f"Illegal operation {name}{indices} on {backend.name}")
        seen.add(key)


def measure(circuit):
    """Compute native gate dependency-path depth, with zero-cost synchronization."""
    from qiskit.circuit import ControlFlowOp, Gate

    depths = dict.fromkeys([*circuit.qubits, *circuit.clbits], 0)
    n2 = 0
    for item in circuit.data:
        op = item.operation
        if isinstance(op, ControlFlowOp) or getattr(op, "condition", None) is not None:
            raise ValueError("D2 measurement supports static circuits only")
        wires = (*item.qubits, *item.clbits)
        if len(wires) < 2:
            continue
        if len(item.qubits) > 2 and op.name != "barrier":
            raise ValueError(f"Unexpected multi-qubit output instruction {op.name}")
        cost = int(len(item.qubits) == 2 and isinstance(op, Gate))
        depth = max(depths[bit] for bit in wires) + cost
        for bit in wires:
            depths[bit] = depth
        n2 += cost
    return {"d2": max(depths.values(), default=0), "n2": n2}


def _measurements(circuit):
    """Physical or logical qubit and classical destination for each measurement."""
    return [[circuit.find_bit(item.qubits[0]).index, circuit.find_bit(item.clbits[0]).index]
            for item in circuit.data if item.name == "measure"]


def _mapping(compiled, source):
    if compiled.layout is None:
        raise ValueError("Compiled circuit has no transpiler layout")
    initial = compiled.layout.initial_index_layout(filter_ancillas=True)
    final = compiled.layout.final_index_layout(filter_ancillas=True)
    for indices in (initial, final):
        if (len(indices) != source.num_qubits or len(set(indices)) != len(indices)
                or any(q is None or not 0 <= q < compiled.num_qubits for q in indices)):
            raise ValueError("Invalid logical-to-physical layout")
    if source.clbits != compiled.clbits or source.cregs != compiled.cregs:
        raise ValueError("Classical bits or their register order changed")
    measured = _measurements(compiled) if compiled.num_clbits else []
    if source.num_clbits:
        # The published circuits have no measurements. The measured fixtures use
        # final readout, for which the final layout determines each physical wire.
        expected = Counter((final[q], c) for q, c in _measurements(source))
        if Counter(map(tuple, measured)) != expected:
            raise ValueError("Output measurement wiring disagrees with the source and final layout")
    return {"initial_layout": initial, "final_layout": final, "measurement_map": measured}


def _metrics(compiled, source, backend):
    validate_target(compiled, backend)
    metrics = measure(compiled)
    if metrics["n2"] != compiled.count_ops().get("cz", 0):
        raise ValueError("Native two-qubit count disagrees with the CZ count")
    return {**metrics, "depth": compiled.depth(), **_mapping(compiled, source)}


class TrialFailure(RuntimeError):
    """A failed attempt with the time it actually spent in the transpiler."""

    def __init__(self, cause, seconds):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.seconds = seconds


def compile_trial(circuit, backend, seed, arm):
    start = time.perf_counter()
    seconds = None
    try:
        manager = make_manager(backend, seed, arm)
        compiled = manager.run(circuit)
        seconds = time.perf_counter() - start
        metrics = _metrics(compiled, circuit, backend)
    except Exception as exc:
        if seconds is None:
            seconds = time.perf_counter() - start
        raise TrialFailure(exc, seconds) from exc
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return compiled, {**metrics, "seconds": seconds,
                      "peak_rss_mib": rss / (1024**2 if sys.platform == "darwin" else 1024)}
