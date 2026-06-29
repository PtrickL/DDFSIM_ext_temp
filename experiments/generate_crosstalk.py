import sys
import json
import argparse
import numpy as np
from qiskit_ibm_runtime.fake_provider import FakeGuadalupeV2
from qiskit.circuit import QuantumCircuit
from qiskit.quantum_info import Operator


DEFAULT_ANGLES = [np.pi/5, 2*np.pi/5, 3*np.pi/5, 4*np.pi/5, np.pi]

class MergedCrosstalkExporter:
    def __init__(self, backend, max_distance=1, fault_sizes=None):
        self.backend = backend
        self.coupling = backend.coupling_map
        self.num_qubits = backend.num_qubits
        self.max_distance = max_distance
        self.fault_sizes = fault_sizes if fault_sizes is not None else list(DEFAULT_ANGLES)
        self.gate_durations = {
            'id': 160, 'x': 160, 'sx': 160, 'h': 160, 'y': 160,
            'rz': 0, 'z': 0, 's': 0, 'sdg': 0, 't': 0, 'tdg': 0,
            'cx': 1440, 'cz': 1440, 'cp': 1440, 'swap': 4320
        }

    def _get_duration(self, name, qubits):
        try:
            q_tuple = tuple(qubits)
            duration_sec = self.backend.target[name][q_tuple].duration
            if duration_sec is not None:
                return duration_sec * 1e9
        except Exception:
            pass
        return self.gate_durations.get(name, 160)

    def _distance(self, q1: int, q2: int) -> int:
        try:
            return self.coupling.distance(q1, q2)
        except Exception:
            return 10**9

    def _within_distance(self, qs_a: list, qs_b: list) -> bool:
        for qa in qs_a:
            for qb in qs_b:
                if self._distance(qa, qb) <= self.max_distance:
                    return True
        return False

    def _edges(self) -> set:
        edges = set()
        for a, b in self.coupling.get_edges():
            edges.add(frozenset((a, b)))
        return edges

    def _neighbors(self, q: int) -> set:
        nbrs = set()
        for a, b in self.coupling.get_edges():
            if a == q:
                nbrs.add(b)
            elif b == q:
                nbrs.add(a)
        return nbrs

    @staticmethod
    def _Rz(theta: float) -> np.ndarray:
        return np.array([[np.exp(-1j*theta/2), 0], [0, np.exp(1j*theta/2)]], dtype=complex)

    @staticmethod
    def _matrix_to_json(mat: np.ndarray) -> list:
        return [[float(np.real(v)), float(np.imag(v))] for v in mat.flatten()]

    def _build_timeline(self, qc: QuantumCircuit) -> list:
        ops = []
        gate_index_counter = 0
        qubit_free_time = {q: 0 for q in range(self.num_qubits)}

        for instruction in qc.data:
            op = instruction.operation
            qs = [qc.find_bit(q).index for q in instruction.qubits]

            if op.name in ['barrier', 'measure', 'reset', 'delay']:
                gate_index_counter += 1
                continue

            start_time = max([qubit_free_time.get(q, 0) for q in qs] + [0])
            dur = self._get_duration(op.name, qs)
            end_time = start_time + dur
            for q in qs:
                qubit_free_time[q] = end_time

            ops.append({
                "idx": gate_index_counter,
                "start": start_time, 
                "end": end_time,
                "qs": qs, 
                "name": op.name,
                "op": op
            })
            gate_index_counter += 1

        return ops

    def _overlaps(self, op_a: dict, op_b: dict) -> bool:
        return min(op_a["end"], op_b["end"]) - max(op_a["start"], op_b["start"]) > 0

    def _faulty_matrix(self, victim_op: dict, theta: float) -> np.ndarray:
        n = len(victim_op["qs"])
        temp_qc = QuantumCircuit(n)
        temp_qc.append(victim_op["op"], list(range(n)))
        for local_idx in range(n):
            temp_qc.rz(theta, local_idx)
        return Operator(temp_qc).data

    def generate(self, qasm_path: str, output_json: str):
        qc = QuantumCircuit.from_qasm_file(qasm_path)
        ops = self._build_timeline(qc)
        
        trigger_pairs = [] 
        for i in range(len(ops)):
            for j in range(len(ops)):
                if i == j:
                    continue
                victim_op, aggressor_op = ops[i], ops[j]
                if not self._overlaps(victim_op, aggressor_op):
                    continue
                if self._within_distance(aggressor_op["qs"], victim_op["qs"]):
                    trigger_pairs.append((victim_op, aggressor_op))

        combo_counts = {(1, 1): 0, (1, 2): 0, (2, 1): 0, (2, 2): 0, "other": 0}
        hw_defect_map = {}
        
        for (victim_op, aggressor_op) in trigger_pairs:
            v_arity = len(victim_op["qs"])
            a_arity = len(aggressor_op["qs"])
            key = (min(v_arity, 2), min(a_arity, 2))
            if key in combo_counts:
                combo_counts[key] += 1
            else:
                combo_counts["other"] += 1

            hw_key = tuple(sorted(victim_op["qs"]))
            if hw_key not in hw_defect_map:
                hw_defect_map[hw_key] = []
            hw_defect_map[hw_key].append(victim_op)

        final_json = []
        for hw_key, triggered_ops in hw_defect_map.items():
            unique_victim_ops = {op["idx"]: op for op in triggered_ops}.values()
            
            for theta in self.fault_sizes:
                scenario = []
                for v_op in unique_victim_ops:
                    mat = self._faulty_matrix(v_op, theta)
                    scenario.append({
                        "faulty_matrix": self._matrix_to_json(mat),
                        "gate_index":    int(v_op["idx"]),
                        "target_qubits": [self.num_qubits - 1 - int(q) for q in victim_op["qs"]],
                        "type":          "Z",
                    })
                final_json.append(scenario)

        with open(output_json, "w") as f:
            json.dump(final_json, f, separators=(",", ":"))

        # Report
        print(f"Distance threshold         : {self.max_distance}")
        print(f"Fault sizes (rad)          : {[round(a, 4) for a in self.fault_sizes]}")
        print(f"Trigger count              : {len(trigger_pairs)}")
        print(f"  victim 1q ← aggressor 1q : {combo_counts[(1,1)]}")
        print(f"  victim 1q ← aggressor 2q : {combo_counts[(1,2)]}")
        print(f"  victim 2q ← aggressor 1q : {combo_counts[(2,1)]}")
        print(f"  victim 2q ← aggressor 2q : {combo_counts[(2,2)]}")
        print(f"\nTotal faults: {len(final_json)}")
        print(f"Output: {output_json}")

def parse_angles(s: str) -> list:
    angles = []
    for tok in s.split(","):
        tok = tok.strip().replace("pi", str(np.pi)).replace("PI", str(np.pi))
        angles.append(float(eval(tok, {"__builtins__": {}}, {})))
    return angles


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Crosstalk fault generator"
    )
    parser.add_argument("qasm_file", help="qasm path")
    parser.add_argument("--distance", type=int, default=1,
                        help="distance threshold for crosstalk, default: 1")
    parser.add_argument("--angles", type=str, default=None,
                        help='multiple Rz angles, comma-separated, supporting pi.'
                             'default: pi/5, 2*pi/5, 3*pi/5, 4*pi/5, pi')
    args = parser.parse_args()

    fault_sizes = parse_angles(args.angles) if args.angles else None

    base_name = args.qasm_file.rsplit(".", 1)[0]
    out_path = f"{base_name}_ctf.json"

    backend = FakeGuadalupeV2()
    exporter = MergedCrosstalkExporter(
        backend=backend,
        max_distance=args.distance,
        fault_sizes=fault_sizes,
    )
    exporter.generate(args.qasm_file, out_path)
