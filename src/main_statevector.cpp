#include "PauliFaultGenerator.hpp"
#include "FaultLoader.hpp"
#include "SimUtils.hpp" 
#include "CostAwareTreeSimulator.hpp"
#include "SegmentTreeFaultSimulator.hpp"
#include "PrefixSuffixSimulator.hpp"
#include "CheckpointSimulator.hpp"
#include "UpdateBasedCostAwareTreeSimulator.hpp"
#include "UpdateBasedSegmentTreeSimulator.hpp"
#include "DirectFaultSimulator.hpp"
#include "nlohmann/json.hpp"

#include <iostream>
#include <fstream>
#include <string>
#include <vector>
#include <iomanip>
#include <sys/resource.h> 
#include <chrono>
#include <algorithm>
#include <map>
#include <type_traits> // �����ޤJ�o�ӨӤ䴩 std::is_same_v

long getPeakRSS() {
    struct rusage usage;
    getrusage(RUSAGE_SELF, &usage);
    return usage.ru_maxrss / 1024;
}

// =========================================================
// OOM-Safe �� DD �}�����A�V�q�Ѩ��� (Sparse State Vector Extractor)
// =========================================================
void traverse_dd(dd::Package* pkg, dd::vEdge e, double act_r, double act_i, std::string path, std::map<std::string, std::vector<double>>& res, int current_level) {
    if (e.isZeroTerminal()) return;

    // �p����e���|�ֿn���Ƽ��v��
    double e_r = dd::RealNumber::val(e.w.r);
    double e_i = dd::RealNumber::val(e.w.i);
    double next_r = act_r * e_r - act_i * e_i;
    double next_i = act_r * e_i + act_i * e_r;

    if (std::abs(next_r) < 1e-14 && std::abs(next_i) < 1e-14) return;

    // �p�G�쩳�F�A�ɻ��ѤU�� 0 ���x�s���G
    if (dd::vNode::isTerminal(e.p)) {
        while (current_level >= 0) { path += "0"; current_level--; }
        res[path] = {next_r, next_i};
        return;
    }

    int node_level = e.p->v;
    
    // MQT Core �� DD �Y��W�h�G�p�G�Y�� Qubit �`�I�Q���L�A�N�����@�w�O |0> �A
    while (current_level > node_level) {
        path += "0";
        current_level--;
    }

    // �~�򩹤U���X |0> �M |1> ��Ӥ���
    traverse_dd(pkg, e.p->e[0], next_r, next_i, path + "0", res, node_level - 1);
    traverse_dd(pkg, e.p->e[1], next_r, next_i, path + "1", res, node_level - 1);
}

std::map<std::string, std::vector<double>> getSparseStateVector(dd::Package* pkg, dd::vEdge root, size_t n_qubits) {
    std::map<std::string, std::vector<double>> res;
    // MQT Core �� root level �O n_qubits - 1
    traverse_dd(pkg, root, 1.0, 0.0, "", res, n_qubits - 1);
    return res;
}

// =========================================================
// ���A�V�q�����֤ߤ���
// =========================================================
template <typename SimType>
void run_sv_simulation(const std::string& qasm_file, const std::string& sim_name, const std::vector<RuntimeFaultScenario>& scenarios, size_t config_param) {
    try {
        auto full_qc = qasm3::Importer::importf(qasm_file);
        size_t n_qubits = full_qc.getNqubits();
        
        // �����ϥΩ��h������
        SimType sim(std::move(full_qc));
        
        // �w�藍�P�������I�s������ config ���
        if constexpr (std::is_same_v<SimType, PrefixSuffixSimulator>) {
            sim.setMaxDDSize(n_qubits + config_param); 
        } else if constexpr (std::is_same_v<SimType, CheckpointSimulator>) {
            sim.setCheckpointInterval(config_param); 
        } else {
            sim.setNodeThreshold(n_qubits + config_param); 
        }

        auto t_build_start = std::chrono::high_resolution_clock::now();
        sim.buildIndex();
        auto t_build_end = std::chrono::high_resolution_clock::now();
        double build_ms = std::chrono::duration<double, std::milli>(t_build_end - t_build_start).count();

        auto t_sim_start = std::chrono::high_resolution_clock::now();
        nlohmann::json all_results_json = nlohmann::json::array();

        if constexpr (std::is_base_of_v<MSTFaultSimulator, SimType>) {
            std::cout << "[Info] Using MST to plan optimal execution order..." << std::endl;
            auto order = MSTFaultSimulator::planExecutionOrder(scenarios);
            std::vector<nlohmann::json> temp_results(scenarios.size());
            
            size_t counter = 0;
            for (size_t idx : order) {
                dd::vEdge state = sim.simulate(scenarios[idx]);
                auto sparse_vec = getSparseStateVector(sim.getPackage(), state, n_qubits);
                
                nlohmann::json scen_node;
                scen_node["scenario_index"] = idx;
                scen_node["state_vector"] = sparse_vec;
                temp_results[idx] = scen_node;
                
                counter++;
                if (counter % 10 == 0) sim.getPackage()->garbageCollect();
            }
            for (auto& j : temp_results) all_results_json.push_back(j);
        } else {
            size_t counter = 0;
            for (const auto& sc : scenarios) {
                dd::vEdge state = sim.simulate(sc);
                auto sparse_vec = getSparseStateVector(sim.getPackage(), state, n_qubits);
                
                nlohmann::json scen_node;
                scen_node["scenario_index"] = counter;
                scen_node["state_vector"] = sparse_vec;
                all_results_json.push_back(scen_node);
                
                counter++;
                if (counter % 10 == 0) sim.getPackage()->garbageCollect();
            }
        }

        sim.getPackage()->garbageCollect();

        auto t_sim_end = std::chrono::high_resolution_clock::now();
        double sim_ms = std::chrono::duration<double, std::milli>(t_sim_end - t_sim_start).count();
        long mem_mb = getPeakRSS();

        std::string out_filename = "sv_results_" + sim_name + ".json";
        std::ofstream ofs(out_filename);
        if (ofs.is_open()) {
            ofs << all_results_json.dump(4);
            ofs.close();
        }

        std::cout << "DATA|" << sim_name << "|SV|" << config_param << "|" 
                  << std::fixed << std::setprecision(1) << build_ms << "|"
                  << sim_ms << "|" << mem_mb << "|" << scenarios.size() << "|DONE\n";

    } catch (const std::exception& e) {
        std::cout << "DATA|" << sim_name << "|SV|" << config_param << "|0|-1|-1|0|ERR\n";
        std::cerr << "[Error] " << e.what() << "\n";
    }
}

int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "Usage: " << argv[0] << " <qasm_file> <fault_json> <simulator_type> [config_param]\n";
        std::cerr << "Available Simulators:\n";
        std::cerr << "  - cawst\n  - segment\n  - ps\n  - checkpoint\n";
        std::cerr << "  - update_cawst (MST Optimized)\n  - update_segment (MST Optimized)\n";
        return 1;
    }

    std::string qasm_file = argv[1];
    std::string json_file = argv[2];
    std::string sim_type = argv[3];
    std::transform(sim_type.begin(), sim_type.end(), sim_type.begin(), ::tolower);
    
    size_t config_param = (argc > 4) ? std::stoul(argv[4]) : 256;

// =================================================================
    // 1. 先讀取所有的錯誤場景 (因為算 Weight 矩陣需要用到它們)
    // =================================================================
    if (!std::ifstream(json_file).good()) {
        std::cerr << "[Error] Fault file not found: " << json_file << "\n";
        return 1;
    }
    auto scenarios = FaultLoader::load(json_file);

    // =================================================================
    // 🌟 2. 新增區塊：專門處理 Python 傳來的 MST 權重萃取請求
    // =================================================================
    if (sim_type == "export_mst_weights") {
        std::cout << "[C++] Received export_mst_weights command. Calculating distance matrix..." << std::endl;
        std::ofstream out("mst_weights.json");
        out << "{";
        for (size_t i = 0; i < scenarios.size(); ++i) {
            out << "\"" << i << "\": {";
            for (size_t j = 0; j < scenarios.size(); ++j) {
                // 呼叫學長的算距離函式
                int dist = MSTFaultSimulator::calculateGlobalDistance(scenarios[i], scenarios[j]);
                out << "\"" << j << "\": " << dist;
                if (j < scenarios.size() - 1) out << ", ";
            }
            out << "}";
            if (i < scenarios.size() - 1) out << ", ";
        }
        out << "}";
        out.close();
        
        std::cout << "DATA|MST_WEIGHTS_EXPORTED|DONE" << std::endl;
        return 0; // 🛑 算完 Weight 就直接結束程式，交還控制權給 Python
    }
    // =================================================================

    if (sim_type == "cawst") {
        run_sv_simulation<CostAwareTreeSimulator>(qasm_file, "CAWST", scenarios, config_param);
    } else if (sim_type == "segment") {
        run_sv_simulation<SegmentTreeFaultSimulator>(qasm_file, "SegmentTree", scenarios, config_param);
    } else if (sim_type == "ps") {
        run_sv_simulation<PrefixSuffixSimulator>(qasm_file, "PrefixSuffix", scenarios, config_param);
    } else if (sim_type == "checkpoint") {
        run_sv_simulation<CheckpointSimulator>(qasm_file, "Checkpoint", scenarios, config_param);
    } else if (sim_type == "update_cawst") {
        run_sv_simulation<UpdateBasedCostAwareTreeSimulator>(qasm_file, "Update_CAWST", scenarios, config_param);
    } else if (sim_type == "update_segment") {
        run_sv_simulation<UpdateBasedSegmentTreeSimulator>(qasm_file, "Update_SegmentTree", scenarios, config_param);
    } else if (sim_type == "direct") {
        // Direct �������w�]���ݭn config_param (Block Size)�A�����F�����@�P�ڭ̷Ӽ˶ǤJ
        run_sv_simulation<DirectFaultSimulator>(qasm_file, "Direct", scenarios, config_param);
    } else {
        std::cerr << "[Error] Unknown simulator type: " << sim_type << "\n";
        return 1;
    }

    return 0;
}