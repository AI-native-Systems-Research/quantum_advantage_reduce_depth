import json

import pytest

from quantum_advantage_reduce_depth.prepare import (
    MANIFEST, CircuitImportError, _graph, _obstruction, _source_bytes,
    import_qasm, snapshot,
)


def test_manifest_includes_aliases_and_prespecified_cases():
    manifest = json.loads(MANIFEST.read_text())
    rows = manifest["inventory"]
    assert len(rows) == len({r["git_blob"] for r in rows}) == len({r["sha256"] for r in rows}) == 39
    sources = [source for row in rows for source in row["sources"]]
    assert len(sources) == len({s["path"] for s in sources}) == 43
    assert all(s["tracker_id"] for s in sources)
    assert sum("FakeBoston" in r["expected_targets"] for r in rows) == 14
    assert sum("FakeNighthawk" in r["expected_targets"] for r in rows) == 4
    assert all(r["expected_targets"] or r["exclusion"] for r in rows)


def test_corrupt_raw_source_fails_before_import(tmp_path):
    manifest = json.loads(MANIFEST.read_text())
    row = next(r for r in manifest["inventory"] if r["expected_targets"])
    src = tmp_path / "source" / row["sources"][0]["path"]
    src.parent.mkdir(parents=True)
    src.write_bytes(b"OPENQASM 2.0;")
    with pytest.raises(ValueError, match="does not match manifest"):
        _source_bytes(tmp_path / "output", tmp_path / "source", manifest, row)
    assert not (tmp_path / "output").exists()


def test_qasm3_custom_gate_and_classical_wire_order(tmp_path):
    from qiskit.quantum_info import Operator

    raw = b"""// exported circuit
OPENQASM 3.0;
include "stdgates.inc";
gate turn(a) x, y { rx(a) x; cx x, y; }
qubit[3] q;
bit[2] c;
turn(pi/7) q[2], q[0];
barrier q;
c[0] = measure q[2];
c[1] = measure q[0];
"""
    version, circuit = import_qasm(raw, "custom.qasm")
    restored, digest = snapshot(circuit, tmp_path / "custom.qpy")
    assert version == 3 and len(digest) == 64
    assert Operator(circuit.remove_final_measurements(inplace=False)).equiv(
        Operator(restored.remove_final_measurements(inplace=False)))
    assert [(restored.find_bit(item.qubits[0]).index, restored.find_bit(item.clbits[0]).index)
            for item in restored.data if item.name == "measure"] == [(2, 0), (0, 1)]
    assert snapshot(circuit, tmp_path / "custom.qpy")[1] == digest


def test_legacy_qasm2_and_named_failure():
    raw = b'OPENQASM 2.0; include "qelib1.inc"; qreg q[2]; sx q[1]; rzz(pi/3) q[0],q[1];'
    version, circuit = import_qasm(raw, "legacy.qasm")
    assert version == 2 and circuit.count_ops() == {"sx": 1, "rzz": 1}
    with pytest.raises(CircuitImportError, match=r"legacy.qasm:.*unknown_gate"):
        import_qasm(raw.replace(b"sx ", b"unknown_gate "), "legacy.qasm")


def test_structural_obstructions():
    chain = _graph(4, [(0, 1), (1, 2), (2, 3)])
    square = _graph(4, [(0, 1), (1, 2), (2, 3), (3, 0)])
    star = _graph(4, [(0, 1), (0, 2), (0, 3)])
    assert _obstruction(square, chain)["type"] == "four_cycle"
    assert _obstruction(star, chain)["type"] == "maximum_degree"
    # Both graphs have degree three and the same number of vertices, but
    # each color class of the target holds only three sites.
    source = _graph(6, [(0, 1), (0, 2), (0, 3), (3, 4), (4, 5)])
    balanced = _graph(6, [(0, 1), (0, 2), (0, 3), (1, 4), (2, 5)])
    assert _obstruction(source, balanced)["type"] == "bipartition_capacity"
    assert _obstruction(chain, square) is None


def test_graph_companion_provenance():
    inventory = json.loads(MANIFEST.read_text())["inventory"]
    entries = {e["sources"][0]["path"]: e for e in inventory}
    checked = next(e for e in inventory if e["circuit_id"] == "graph-checked")
    companion, = checked["sources"]
    bare = entries[companion["catalog_path"]]
    original, = bare["sources"]
    assert bare["family"] == "Random graph state sampling"
    assert bare["label"] == "Random graph state without checks"
    assert checked["family"] == "Random graph state sampling with checks"
    assert bare["expected_targets"] == [] and checked["expected_targets"] == ["FakeNighthawk"]
    assert companion["tracker_id"] == original["tracker_id"]
    assert "companion" in companion["relationship"]
