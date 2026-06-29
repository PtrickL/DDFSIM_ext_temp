# DDFSIM Add-ons

This is a temporary repository which contains the Python-based extensions for DDFSIM, consisting of a Crosstalk Fault Generator and a Structure-Aware Identifier.
Because these tools are add-ons to the original DDFSIM, they MUST be merged into a working DDFSIM environment before execution.

## 1. Environment Setup

### Prerequisites: 

* **Python:** 3.10+
* **Packages:** `qiskit`, `qiskit-ibm-runtime`, `numpy`
* **DDFSIM Core**

### Step-by-Step File Merging

To run the experiments, move the files from this repository into the corresponding folders of your main DDFSIM directory. 
Also, overwrite `src/main_statevector.cpp` with the one provided in this repository.

The final DDFSIM file tree should look like this after merging:

```text
DDFSIM-MAIN/
├── benchmarks/
│   ├── 3cxx.qasm                   <-- (Copied from add-on)
│   ├── 4cx.qasm                    <-- (Copied from add-on)
│   ├── 3cxx_CTFgolden.json         <-- (Copied from add-on)
│   └── ... 
├── experiments/
│   ├── generate_crosstalk.py       <-- (Copied from add-on)
│   ├── Identifier_update.py        <-- (Copied from add-on)
│   └── ...
├── src/
│   └── main_statevector.cpp        <-- (Overwrites original DDFSIM file)├── CMakeLists.txt
└── README.md
```

Note that `benchmarks/` Contains the `.qasm` test circuits and pre-generated golden crosstalk fault lists (`*_CTFgolden.json`); and `experiments/` Contains the execution scripts (`generate_crosstalk.py`, `Identifier_update.py`, etc.).

### Recompile the Backend

Because `main_statevector.cpp` was modified, the C++ simulator must be rebuilt. From the root of the DDFSIM-MAIN directory, run:
```bash
mkdir -p build
cd build
cmake ..
make -j4
cd ..
```


## 2. Crosstalk Fault Generator

Victims are statically enumerated from the FakeGuadalupeV2 backend, triggered via a temporal-overlap and spatial-distance check, and bundled by physical target qubits. 

### Execution & Parameters

Run the generator from the root directory using the following command:
```bash
python ./experiments/generate_crosstalk.py <qasm_file> [--distance D] [--angles A]
```
* `qasm_file`: Required, the path to the target quantum circuit (e.g., `./benchmarks/3cxx.qasm`). 
* `--distance`: Optional, the distance threshold $D$ on the backend coupling map. Default is 1. 
* `--angles`: Optional, a comma separated list of $R_z$ rotation fault magnitudes. Default is `pi/5,2*pi/5,3*pi/5,4*pi/5,pi`.

This should output a breakdown report in terminal and an output file `./benchmarks/*_ctf.json`. 

## 3. Structure-Aware Identifier

Structure-Aware Quantum Fault Scheduler, solves by pre-analyzing the structural distance between fault scenarios before sending them to the C++ engine: 

1. Distance <= 8: It groups topologically similar faults together for ultra-fast, cache-friendly local updates (`update\_cawst`).
2. Distance > 8: When the structural gap is too large, it stops and dynamically spawns a new tree (`cawst`) to prevent cache miss disasters.

By intelligently balancing tree reconstructions and local updates, Identifier guarantees optimal memory and cache performance under extreme circuit depths.

### Basic Execution

Ensure the C++ backend is compiled at `./build/ddf\_statevector\_sim`. Run the identifier using the generated circuit and fault list:
```bash
python ./experiments/Identifier_update.py <path_to_circuit.qasm> <path_to_faults.json>
```
* `path_to_circuit.qasm`: Required, the target circuit. 
* `path_to_faults.json`: Required, the fault list generated in the previous step. 
This should output the routing plan and display the final execution time overhead.

### Automated Performance Comparison

The two automated benchmarking scripts are included to compare the dynamic Identifier against fixed baseline simulators. 

#### Test against Standard Faults: 

```bash
python ./experiments/test_existing_faults.py
```
This should iterate through the `benchmarks/` folder, run all fixed simulators alongside the Identifier, and print a performance comparison table. The Identifier is expected to outperform the fixed baselines in deeper circuits.

#### Test against Hardware Crosstalk Faults: 

```bash
python ./experiments/testCT.py
```
This should automatically call the Crosstalk Generator, then run the simulation engines to compare execution times. 
