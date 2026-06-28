import os
import time
import subprocess


def run_crosstalk_benchmark(qasm_file, fault_json, sim_path="./build/ddf_statevector_sim"):
    print(f"\n{'-'*90}")
    print(f"🚀 測試組合: {os.path.basename(qasm_file)}")
    print(f"   錯誤檔:   {os.path.basename(fault_json)}")
    print(f"{'-'*90}")
    
    baselines = ["ps", "cawst", "update_cawst"]
    results = {}

    
    for base in baselines:
        print(f"  - 執行引擎: {base.upper():<12} ...", end="", flush=True)
        cmd = [sim_path, qasm_file, fault_json, base, "256"]
        t0 = time.time()
        
        
        subprocess.run(cmd, capture_output=True, text=True)
        
        elapsed = time.time() - t0
        results[base] = f"{elapsed:.3f} s"
        print(f" {elapsed:.3f} s")


    print(f"  - 執行引擎: {'Identifier':<12} ...", end="", flush=True)
    cmd_ours = ["python", "Identifier_update.py", qasm_file, fault_json, "--no-debug"]
    t0 = time.time()
    

    subprocess.run(cmd_ours, capture_output=True, text=True)
    
    elapsed = time.time() - t0
    results["Identifier"] = f"{elapsed:.3f} s"
    print(f" \033[92m{elapsed:.3f} s\033[0m")

    return results


if __name__ == "__main__":
    print("啟動自動化 Benchmark 環境 (全電路真實 Crosstalk 生成 / 無限死鬥版)...")
    
    CPP_SIM_PATH = "./build/ddf_statevector_sim"
    if not os.path.exists(CPP_SIM_PATH):
        print(f"[警告] 找不到 C++ 引擎 {CPP_SIM_PATH}，請確認路徑！")
        exit(1)


    CIRCUITS_TO_TEST = {
        "qaoa": [10, 11],
        "qpeexact": [10, 11],
        "qft": [10, 11],
        "qftentangled": [10, 11],
        "vqe": [10]
    }
    

    BASE_DIR = "benchmarks" 

    all_results = []

    for ckt_name, qubits_list in CIRCUITS_TO_TEST.items():
        for q in qubits_list:
       
            base_filename = f"{ckt_name}_nativegates_ibm_qiskit_opt0_{q}"
            qasm_file = os.path.join(BASE_DIR, f"{base_filename}.qasm")
            
            if not os.path.exists(qasm_file):
                print(f"[跳過] 找不到 QASM 檔: {qasm_file}")
                continue
                
            # 呼叫使用者的 generate_crosstalk.py 
            print(f"\n➤ 正在為 {base_filename} 生成硬體 Crosstalk 錯誤...")
            subprocess.run(["python", "generate_crosstalk.py", qasm_file], capture_output=True)
            
            # generate_crosstalk.py 
            fault_json = os.path.join(BASE_DIR, f"{base_filename}_ctf.json")
            
            if not os.path.exists(fault_json):
                print(f"[錯誤] 無法生成 JSON 檔: {fault_json}")
                continue
                
         
            res = run_crosstalk_benchmark(qasm_file, fault_json, CPP_SIM_PATH)
            
         
            all_results.append({
                "Circuit": ckt_name.upper(),
                "Qubits": q,
                "Data": res
            })


    if all_results:
        print("\n\n🏆 全電路真實硬體 Crosstalk 效能比較表 (無 Timeout 限制)")
        print("-" * 100)
        print(f" {'Circuit':<14} | {'Qubits':<6} | {'TA 傳統固定引擎 (Baseline)':<35} | {'動態分流'}")
        print(f" {'':<14} | {'':<6} | {'PS':<10} | {'CAWST':<10} | {'UPDATE_CAWST':<12} | {'Identifier (Ours)'}")
        print("-" * 100)
        
        for item in all_results:
            c = item["Circuit"]
            q = item["Qubits"]
            res = item["Data"]
            
            ps_t = res.get("ps", "N/A")
            cw_t = res.get("cawst", "N/A")
            up_t = res.get("update_cawst", "N/A")
            our_t = res.get("Identifier", "N/A")
            
            print(f" {c:<14} | {q:<6} | {ps_t:<10} | {cw_t:<10} | {up_t:<12} | \033[92m{our_t}\033[0m")
        
        print("-" * 100)
        print("💡 真實拓樸 Crosstalk 測試完畢！這些數據將完美展示排程器對空間干擾的處理能力！")
    else:
        print("\n[提示] 未找到任何符合的測資檔案，請檢查 BASE_DIR 路徑設定！")