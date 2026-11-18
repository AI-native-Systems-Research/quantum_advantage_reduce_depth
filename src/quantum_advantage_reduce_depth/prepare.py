"""Prepare immutable tracker inputs and resolve routing eligibility on both devices."""

import hashlib
import io
import itertools
import re
from collections import Counter, deque
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

from .storage import atomic_write, read_json, sha256, write_json

TARGETS = ("FakeBoston", "FakeNighthawk")
MANIFEST = Path(__file__).with_name("manifest.json")
VF2_CALL_LIMIT = 2_000_000


class CircuitImportError(ValueError):
    """The published file could not be faithfully imported."""


def import_qasm(raw, name):
    from qiskit import qasm2, qasm3

    try:
        text = raw.decode("utf-8-sig")
        header = re.match(r"(?:\s|//[^\n]*(?:\n|$)|/\*.*?\*/)*OPENQASM\s+([23])(?:\.\d+)?\s*;", text, re.S)
        if header is None:
            raise ValueError("expected an OPENQASM 2 or 3 header")
        version = int(header[1])
        circuit = (qasm2.loads(text, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
                   if version == 2 else qasm3.loads(text))
        return version, circuit
    except Exception as exc:
        raise CircuitImportError(f"{name}: {type(exc).__name__}: {exc}") from exc


def _source_bytes(output, source, manifest, entry):
    relative = entry["sources"][0]["path"]
    cached = Path("inputs", entry["circuit_id"] + ".qasm")
    if source is not None:
        raw = (Path(source) / relative).read_bytes()
    elif (output / cached).exists():
        raw = (output / cached).read_bytes()
    else:
        url = f"https://raw.githubusercontent.com/{manifest['repository']}/{manifest['revision']}/{quote(relative)}"
        with urlopen(url, timeout=120) as response:
            raw = response.read()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if blob != entry["git_blob"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise ValueError(f"{relative}: published Git blob or SHA-256 does not match manifest")
    if not (output / cached).exists() or (output / cached).read_bytes() != raw:
        atomic_write(output / cached, lambda f: f.write(raw), binary=True)
    return raw, cached.as_posix()


def _indices(circuit, bits):
    return tuple(circuit.find_bit(bit).index for bit in bits)


def _verify_round_trip(original, restored):
    # Circuit equality includes definitions, parameters and global phase. Also compare
    # the ordered instruction stream explicitly (DAG equality permits commuting gates).
    if (original != restored or original.qregs != restored.qregs or
            original.cregs != restored.cregs or original.ancillas != restored.ancillas):
        raise ValueError(f"{original.name}: QPY circuit, registers or ancillas changed")
    for before, after in zip(original.data, restored.data, strict=True):
        if (before.operation != after.operation
                or _indices(original, before.qubits) != _indices(restored, after.qubits)
                or _indices(original, before.clbits) != _indices(restored, after.clbits)):
            raise ValueError(f"{original.name}: QPY changed ordered instruction {before.operation.name}")


def snapshot(circuit, path):
    from qiskit import qpy

    if path.exists():
        data = path.read_bytes()
    else:
        buf = io.BytesIO()
        qpy.dump(circuit, buf)
        data = buf.getvalue()
    restored, = qpy.load(io.BytesIO(data))
    _verify_round_trip(circuit, restored)
    if not path.exists():
        atomic_write(path, lambda f: f.write(data), binary=True)
    return restored, hashlib.sha256(data).hexdigest()


def _source_details(circuit):
    from qiskit.circuit import Gate

    ops = dict(circuit.count_ops())
    gates = [inst for inst in circuit.data if isinstance(inst.operation, Gate)]
    touched = sorted({circuit.find_bit(q).index for inst in gates for q in inst.qubits})
    return {"num_qubits": circuit.num_qubits, "num_clbits": circuit.num_clbits,
            "active_qubits": touched, "num_active_qubits": len(touched), "operations": ops,
            "two_qubit_gates": sum(len(i.qubits) == 2 for i in gates),
            "barriers": ops.get("barrier", 0), "measurements": ops.get("measure", 0)}


def _graph(width, edges):
    adj = [set() for _ in range(width)]
    for u, v in edges:
        adj[u].add(v)
        adj[v].add(u)
    return adj


def _four_cycle(adj):
    # Two vertices with two common neighbors give a four-cycle.
    for u, v in itertools.combinations(range(len(adj)), 2):
        common = sorted(adj[u] & adj[v])
        if len(common) >= 2:
            return [u, common[0], v, common[1]]
    return None


def _bipartitions(adj):
    colors = {}
    parts = []
    for root in range(len(adj)):
        if root in colors:
            continue
        count = [1, 0]
        colors[root] = 0
        queue = deque([root])
        while queue:
            u = queue.popleft()
            for v in adj[u]:
                if v not in colors:
                    colors[v] = 1 - colors[u]
                    count[colors[v]] += 1
                    queue.append(v)
                elif colors[u] == colors[v]:
                    return None
        parts.append(sorted(count))
    return parts


def _obstruction(logical, physical):
    logical_degree = max(map(len, logical), default=0)
    physical_degree = max(map(len, physical), default=0)
    if logical_degree > physical_degree:
        return {"type": "maximum_degree", "logical": logical_degree, "physical": physical_degree}
    cycle = _four_cycle(logical)
    if cycle and _four_cycle(physical) is None:
        return {"type": "four_cycle", "logical_cycle": cycle, "target_has_four_cycle": False}
    source_parts, target_parts = _bipartitions(logical), _bipartitions(physical)
    if source_parts is not None and target_parts is not None:
        # A connected component maps into one component on the device. Its two
        # color classes must fit either directly or with the colors reversed.
        for a, b in source_parts:
            if all(a > x or b > y for x, y in target_parts):
                return {"type": "bipartition_capacity", "logical_component_classes": [a, b],
                        "target_component_classes": target_parts}
    return None


def routing_screen(circuit, backend):
    from qiskit.converters import circuit_to_dag
    from qiskit.transpiler.passes import VF2Layout

    logical_edges = set()
    for item in circuit.data:
        if item.operation.name == "barrier":
            continue
        if len(item.qubits) == 2:
            logical_edges.add(tuple(sorted(_indices(circuit, item.qubits))))
        elif len(item.qubits) > 2:
            raise ValueError(f"{circuit.name}: unsupported routing-screen operation {item.operation.name}")
    physical_edges = {tuple(sorted(edge)) for edge in backend.target["cz"]}
    vf2 = VF2Layout(target=backend.target, seed=0, max_trials=1, call_limit=VF2_CALL_LIMIT)
    vf2.run(circuit_to_dag(circuit))
    reason = vf2.property_set["VF2Layout_stop_reason"].name
    result = {"vf2_stop_reason": reason, "vf2_call_limit": VF2_CALL_LIMIT, "seed": 0,
              "max_trials": 1}
    layout = vf2.property_set["layout"]
    if layout is not None:
        mapped = [layout[q] for q in circuit.qubits]
        if (len(set(mapped)) != len(mapped) or min(mapped) < 0 or max(mapped) >= backend.num_qubits
                or any(tuple(sorted((mapped[a], mapped[b]))) not in physical_edges for a, b in logical_edges)):
            raise ValueError(f"{circuit.name}: invalid VF2 embedding")
        return {**result, "requires_swaps": False, "layout": mapped}

    # Qiskit's bounded VF2 does not distinguish an exhausted call budget from a
    # proof. Admit a negative only if the actual graphs prove it independently.
    proof = _obstruction(_graph(circuit.num_qubits, logical_edges),
                         _graph(backend.num_qubits, physical_edges))
    if proof is None:
        raise ValueError(f"{circuit.name} on {backend.name}: routing screen unresolved ({reason})")
    return {**result, "requires_swaps": True, "structural_obstruction": proof}


def prepare(output, source=None):
    """Import the selected sources, run both target screens and record fresh stock baselines."""
    from .compiler import (compile_stock, configure_threads, measure, resolve_target,
                           runtime_metadata, target_metadata)

    configure_threads()
    runtime = runtime_metadata()
    output = Path(output)
    manifest = read_json(MANIFEST)
    license_data = MANIFEST.with_name("tracker-LICENSE").read_bytes()
    atomic_write(output / "inputs/LICENSE", lambda f: f.write(license_data), binary=True)
    backends = {target: resolve_target(target) for target in TARGETS}
    inputs, cases = [], []
    for entry in manifest["inventory"]:
        if not entry["expected_targets"]:
            continue
        cid = entry["circuit_id"]
        raw, qasm_path = _source_bytes(output, source, manifest, entry)
        version, parsed = import_qasm(raw, entry["sources"][0]["path"])
        parsed.name = cid
        relative = Path("inputs") / (cid + ".qpy")
        circuit, digest = snapshot(parsed, output / relative)
        details = {**_source_details(circuit), "circuit_id": cid, "raw_qasm": qasm_path,
                   "raw_sha256": entry["sha256"], "qasm_version": version,
                   "snapshot": relative.as_posix(), "sha256": digest, "screen": {}}
        for name, backend in backends.items():
            result = routing_screen(circuit, backend)
            if result["requires_swaps"] != (name in entry["expected_targets"]):
                raise ValueError(f"{cid} on {name}: routing selection differs from the pinned design")
            stock_circuit = compile_stock(circuit, backend)
            stock = measure(stock_circuit)
            result["stock"] = stock
            details["screen"][name] = result
            if result["requires_swaps"]:
                cases.append({"case_id": f"{name.lower()}-{cid}", "circuit_id": cid,
                              "target": name, "family": entry["family"], "label": entry["label"],
                              "snapshot": relative.as_posix(), "sha256": digest, "stock": stock})
        inputs.append(details)
        print(f"Prepared {cid} ({len(inputs)}/15 inputs)", flush=True)
    counts = Counter(c["target"] for c in cases)
    if counts != {"FakeBoston": 14, "FakeNighthawk": 4} or len(inputs) != 15:
        raise ValueError(f"Unexpected routing screen size: {counts} from {len(inputs)} inputs")
    result = {**manifest, "manifest_sha256": sha256(MANIFEST), "runtime": runtime,
              "inputs": inputs, "cases": cases,
              "targets": {name: target_metadata(backend) for name, backend in backends.items()}}
    write_json(output / "prepared.json", result)
    return result
