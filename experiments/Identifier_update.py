import sys
import os
import json
import subprocess
import argparse
import tempfile
import time

try:
    from qiskit import QuantumCircuit
except ImportError:
    print("[Fatal] Qiskit is not installed. Please run: pip install qiskit")
    sys.exit(1)

OPTIMAL_BS_MAP = {
    "direct":         1,
    "checkpoint":     1,
    "ps":           128,
    "cawst":        512,
    "segment":      512,
    "update_cawst": 128,
    "update_segment": 512,
}


def extract_mst_weights_from_cpp(exec_path, qasm_file, fault_json_path):
    print("[Dispatcher] 正在向 C++ 後端請求 MST 邊權重矩陣 (Edge Weights)...")
    weight_file = "mst_weights.json"
    if os.path.exists(weight_file):
        os.remove(weight_file)
    
    #calling C++ to output mst Weight matrix
    cmd = [exec_path, qasm_file, fault_json_path, "export_mst_weights", "1", "1"]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        print(f"[Fatal] 呼叫 C++ 萃取 Weight 失敗: {e}")
        sys.exit(1)

    if not os.path.exists(weight_file):
        print("[Fatal] C++ 未能產生 mst_weights.json。請確認 C++ main 已支援 export_mst_weights！")
        sys.exit(1)

    with open(weight_file, 'r') as f:
        weight_matrix = json.load(f)
    
    print(f"[Dispatcher] 成功取得 C++ Weight 矩陣 (共 {len(weight_matrix)} 個場景節點)。")
    return weight_matrix




#2：(Iterative Deepest Path Extraction)
def cluster_scenarios_by_weight(scenarios, weight_matrix, debug=True):
    dispatch_plan = {}
    MST_WEIGHT_THRESHOLD = 8  


    # Step 1:(Fast Path Bypass)
    heavy_indices = set()
    for i, scenario in enumerate(scenarios):
        faults = scenario["faults"] if isinstance(scenario, dict) and "faults" in scenario else scenario
        
        if len(faults) < 3:
            dispatch_plan[i] = ("ps", -1)
        else:
            heavy_indices.add(i)

    if not heavy_indices:
        return dispatch_plan


    #strong data structure to fix checking problems ：(Pre-sorted Adjacency List)
    # 我們只紀錄距離 <= 8 的鄰居，並由近到遠排好序。
    # 這樣在最內層迴圈找鄰居時，時間複雜度幾乎是 O(1)
    fast_neighbors = {i: [] for i in heavy_indices}
    
    for i in heavy_indices:
        str_i = str(i)
        if str_i in weight_matrix:
            for j in heavy_indices:
                if i != j:
                    str_j = str(j)
                    w = weight_matrix[str_i].get(str_j, float('inf'))
                    # 過濾掉超過閥值的點，壓縮資料量
                    if w <= MST_WEIGHT_THRESHOLD:
                        fast_neighbors[i].append((w, j))
            
            # 依照weight (距離) 從小到大排序
            fast_neighbors[i].sort(key=lambda x: x[0])

    
    # Step 2: 最深 DFS (Iterative Deepest Path) (cut backedge)
    unvisited = set(heavy_indices)
    current_cluster_id = 0

    if debug: print(f"\n    🛤️ [Deepest Path Search] 開始在 {len(unvisited)} 個場景中「窮舉所有節點」尋找最深連續路徑...")

    while unvisited:
        best_chain = []
        best_chain_weight = float('inf')

        #把剩下的每一個點都輪流當作起點！
        for start_node in unvisited:
            curr = start_node
            current_chain = [curr]
            current_weight_sum = 0
            
            local_unvisited = unvisited.copy()
            local_unvisited.remove(curr)

            # No backedge DFS：貪婪地往最深的合法鄰居走
            while True:
                next_node = -1
                min_dist = float('inf')
                
                # 因為 fast_neighbors 已經排好序，我們只要挑第一個還沒走過的點！
                for w, neighbor in fast_neighbors[curr]:
                    if neighbor in local_unvisited:
                        next_node = neighbor
                        min_dist = w
                        break  # 找到了最近的點，直接中斷迴圈！
                
                # 如果找不到任何合法的未走訪鄰居 (或是所有鄰居都大於閥值)
                if next_node == -1:
                    break

                current_chain.append(next_node)
                current_weight_sum += min_dist
                local_unvisited.remove(next_node)
                curr = next_node

            # 結算這條路徑：保留最長、成本最低的最佳解
            if len(current_chain) > len(best_chain) or \
               (len(current_chain) == len(best_chain) and current_weight_sum < best_chain_weight):
                best_chain = current_chain
                best_chain_weight = current_weight_sum

       
        # Step 3: 從總池子裡拔除我們剛剛找到的「絕對最深路徑」
        if debug and len(best_chain) > 1:
            print(f"    🌟 [Cluster {current_cluster_id:>3}] 拔除最深路徑 (深度 {len(best_chain):>3}, 總權重 {best_chain_weight}): {best_chain[:4]}... 等")

        for idx, node in enumerate(best_chain):
            if idx == 0:
                dispatch_plan[node] = ("cawst", current_cluster_id)
            else:
                dispatch_plan[node] = ("update_cawst", current_cluster_id)
            
            # 正式從大池子中拔除
            unvisited.remove(node)

        current_cluster_id += 1

    return dispatch_plan

def run_simulation_chunk(exec_path, qasm_file, chunk_json_path, sim, shots):
    bs = OPTIMAL_BS_MAP.get(sim, 128)
    cmd = [exec_path, qasm_file, chunk_json_path, sim, str(bs), str(shots)]
    expected_dynamic = f"results_{sim}.json"
    expected_sv = f"sv_results_{sim}.json"

    for old_file in [expected_dynamic, expected_sv]:
        if os.path.exists(old_file): os.remove(old_file)
        
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            return None, 0.0, 0.0
            
        build_time_ms = 0.0
        sim_time_ms = 0.0
        for line in result.stdout.split('\n'):
            if line.startswith("DATA|") and line.endswith("|DONE"):
                parts = line.split('|')
                is_sv = "SV" in parts
                build_time_ms = float(parts[4]) if is_sv else float(parts[3])
                sim_time_ms = float(parts[5]) if is_sv else float(parts[4])
                mem = parts[6] if is_sv else parts[5]
                engine_type = "Statevector" if is_sv else "Dynamic"
                print(f"      - Engine: {engine_type:<12} | Build: {build_time_ms:>6}ms | Sim: {sim_time_ms:>6}ms | Mem: {mem}MB")
                break

        output_data = None
        if os.path.exists(expected_sv):
            with open(expected_sv, 'r') as f: output_data = json.load(f)
            os.remove(expected_sv)
        elif os.path.exists(expected_dynamic):
            with open(expected_dynamic, 'r') as f: output_data = json.load(f)
            os.remove(expected_dynamic)
            
        return output_data, build_time_ms, sim_time_ms
    except subprocess.TimeoutExpired:
        return None, 0.0, 0.0


def main():
    parser = argparse.ArgumentParser(description="DDFSIM Dispatcher (Deepest Path Extraction Edition)")
    parser.add_argument("qasm_file", help="Path to the quantum circuit .qasm file")
    parser.add_argument("mixed_fault_json", help="Path to the input fault scenario JSON file")
    parser.add_argument("--shots", type=int, default=1024, help="Number of simulation shots")
    parser.add_argument("--sim-path", default="./build/ddf_statevector_sim", help="Path to C++ executable")
    parser.add_argument("--no-debug", action="store_true", help="Disable structural radar logging")
    args = parser.parse_args()

    if not os.path.exists(args.mixed_fault_json):
        print(f"[Error] File not found: {args.mixed_fault_json}")
        sys.exit(1)

    total_start_time = time.time()

    with open(args.mixed_fault_json, 'r') as f:
        all_scenarios = json.load(f)

    print(f"\n[Dispatcher] Loaded {len(all_scenarios)} scenarios.")
    if not args.no_debug: print("-" * 90)

    #步驟 1：萃取 C++ 真實 MST 權重矩陣
    weight_matrix = extract_mst_weights_from_cpp(args.sim_path, args.qasm_file, args.mixed_fault_json)

    if not args.no_debug: print("-" * 90)

    #步驟 2：執行最深路徑萃取與切割
    dispatch_plan = cluster_scenarios_by_weight(all_scenarios, weight_matrix, debug=(not args.no_debug))

    sim_batches = {}
    for idx, scenario in enumerate(all_scenarios):
        best_sim, cluster_id = dispatch_plan[idx]

        if "update" in best_sim and cluster_id != -1:
            batch_key = f"{best_sim} (Cluster_{cluster_id})"
        else:
            batch_key = best_sim

        if batch_key not in sim_batches:
            sim_batches[batch_key] = {"engine_cmd": best_sim, "scenarios": []}
            
        sim_batches[batch_key]["scenarios"].append({"original_idx": idx, "data": scenario})

    if not args.no_debug: print("-" * 90)

    chunks = [{"batch_name": k, "engine_cmd": v["engine_cmd"], "scenarios": v["scenarios"]} for k, v in sim_batches.items()]
    print(f"[Dispatcher] Global Batching Complete. Segregated into {len(chunks)} ultra-optimized clusters.\n")

    final_merged_results = []
    total_cpp_build_time_ms = 0.0
    total_cpp_sim_time_ms = 0.0

    #步驟 3：將分流好的最佳化批次丟給 C++ 引擎執行
    for chunk_idx, chunk in enumerate(chunks):
        batch_name = chunk["batch_name"]
        sim_cmd = chunk["engine_cmd"]
        scenarios_to_run = [item["data"] for item in chunk["scenarios"]]
        original_indices = [item["original_idx"] for item in chunk["scenarios"]]
        
        print(f"📦 [Batch {chunk_idx+1}/{len(chunks)}] Dispatching {len(scenarios_to_run):>5} scenarios -> \033[96m{batch_name}\033[0m")
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".json") as temp_fault_file:
            json.dump(scenarios_to_run, temp_fault_file)
            temp_fault_path = temp_fault_file.name

        chunk_results, b_ms, s_ms = run_simulation_chunk(args.sim_path, args.qasm_file, temp_fault_path, sim_cmd, args.shots)
        os.remove(temp_fault_path)

        if chunk_results:
            total_cpp_build_time_ms += b_ms
            total_cpp_sim_time_ms += s_ms
            for i, res in enumerate(chunk_results):
                res["scenario_index"] = original_indices[i]
                final_merged_results.append(res)
        else:
            print(f"[Warning] Batch {chunk_idx+1} execution failed or timed out.")

    final_merged_results.sort(key=lambda x: x["scenario_index"])

    output_filename = "results_mixed_final.json"
    with open(output_filename, 'w') as f:
        json.dump(final_merged_results, f, indent=4)

    wall_clock_time = time.time() - total_start_time

    print(f"\n🎉 [Success] Pipeline completed. Unified array saved to: {output_filename}")
    print("\n" + "="*60)
    print(" 📊 Performance Analysis Report (Iterative Deepest Path Edition)")
    print("="*60)
    print(f"  ⏳ Wall-Clock Overhead       : {wall_clock_time:>8.4f} seconds")
    print(f"  ⚙️ C++ Core Build Time       : {total_cpp_build_time_ms / 1000:>8.4f} seconds")
    print(f"  🚀 C++ Core Simulation       : {total_cpp_sim_time_ms / 1000:>8.4f} seconds")
    print(f"  🔄 Dispatcher Routing Time   : {max(0, wall_clock_time - ((total_cpp_build_time_ms + total_cpp_sim_time_ms) / 1000)):>8.4f} seconds")
    print("="*60)

if __name__ == "__main__":
    main()