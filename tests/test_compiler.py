import unittest

from quantum_advantage_reduce_depth.compiler import (
    compile_trial, configure_threads, make_manager, measure, validate_target,
)

configure_threads()

from qiskit import QuantumCircuit, qasm2  # noqa: E402
from qiskit.providers.fake_provider import GenericBackendV2  # noqa: E402
from qiskit.quantum_info import Operator  # noqa: E402
from qiskit.transpiler import CouplingMap  # noqa: E402


class CompilerChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = GenericBackendV2(
            4, coupling_map=CouplingMap.from_line(4), basis_gates=["rz", "sx", "x", "cz"], seed=17,
        )

    def test_custom_gate_and_permuted_measurements(self):
        source = qasm2.loads("""
            OPENQASM 2.0;
            include "qelib1.inc";
            gate hop(t) a,b { cx a,b; rz(t) b; cx a,b; }
            qreg q[4]; creg a[2]; creg b[2];
            h q[0]; hop(0.4) q[0],q[3];
            cx q[3],q[1]; cx q[1],q[0]; cx q[2],q[0];
            barrier q;
            measure q[0] -> b[1]; measure q[1] -> a[1];
            measure q[2] -> b[0]; measure q[3] -> a[0];
        """, custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)
        for arm in ("decay", "weighted"):
            with self.subTest(arm=arm):
                compiled, row = compile_trial(source, self.backend, 4, arm)
                self.assertCountEqual(row["measurement_map"], [
                    [row["final_layout"][q], c] for q, c in enumerate([3, 1, 2, 0])
                ])
                unitary = source.remove_final_measurements(inplace=False)
                output = compiled.remove_final_measurements(inplace=False)
                self.assertTrue(Operator.from_circuit(output).equiv(Operator(unitary)))
                self.assertEqual(row["n2"], compiled.count_ops()["cz"])

    def test_heuristic_configuration_and_fixed_search_effort(self):
        for arm in ["decay", "weighted"]:
            manager = make_manager(self.backend, 12, arm)
            layout, = manager.layout.to_flow_controller().tasks
            self.assertEqual(layout.max_iterations, 4)
            self.assertEqual((layout.layout_trials, layout.swap_trials), (20, 20))
            self.assertFalse(layout.skip_routing)
            settings = repr(layout.heuristic)
            if arm == "weighted":
                self.assertIn("wait_time=WaitTimeHeuristic(weight=0.0005,", settings)
                self.assertIn("decay=None", settings)
            else:
                self.assertIn("wait_time=None", settings)
                self.assertIn("decay=DecayHeuristic", settings)

    def test_depth_counts_synchronizing_barriers(self):
        circuit = QuantumCircuit(4)
        circuit.cz(0, 1)
        circuit.cz(2, 3)
        circuit.cz(0, 1)
        circuit.barrier(1, 2)
        circuit.cz(2, 3)
        self.assertEqual(measure(circuit), {"n2": 4, "d2": 3})

    def test_illegal_edge_and_basis(self):
        circuit = QuantumCircuit(4)
        circuit.cz(0, 3)
        with self.assertRaisesRegex(ValueError, "Illegal operation cz"):
            validate_target(circuit, self.backend)
        circuit = QuantumCircuit(4)
        circuit.cx(0, 1)
        with self.assertRaisesRegex(ValueError, "Illegal operation cx"):
            validate_target(circuit, self.backend)
