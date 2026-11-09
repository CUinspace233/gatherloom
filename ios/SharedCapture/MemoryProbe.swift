import Darwin
import Foundation
import os

/// 进程内存测量。分享扩展的内存上限远低于主 App，S0 需要记录 OCR 与正文处理时的峰值。
enum MemoryProbe {
    /// 当前物理占用（phys_footprint，与系统判定内存超限所用口径一致），单位 MB。
    static func footprintMB() -> Double {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
        let result = withUnsafeMutablePointer(to: &info) { pointer in
            pointer.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { raw in
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), raw, &count)
            }
        }
        guard result == KERN_SUCCESS else { return -1 }
        return Double(info.phys_footprint) / 1_048_576
    }

    /// 距离系统内存上限还剩多少，单位 MB。
    static func availableMB() -> Double {
        Double(os_proc_available_memory()) / 1_048_576
    }
}

/// 以 20ms 间隔采样内存，记录区间内峰值。
final class PeakMemorySampler {
    private let queue = DispatchQueue(label: "app.gatherloom.memory-sampler")
    private var timer: DispatchSourceTimer?
    private var peak: Double = 0

    func start() {
        queue.sync { peak = MemoryProbe.footprintMB() }
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(20))
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            self.peak = max(self.peak, MemoryProbe.footprintMB())
        }
        timer.resume()
        self.timer = timer
    }

    /// 停止采样并返回峰值（MB）。
    func stop() -> Double {
        queue.sync {
            timer?.cancel()
            timer = nil
            peak = max(peak, MemoryProbe.footprintMB())
            return peak
        }
    }
}

struct MemorySnapshot: Codable {
    var beforeMB: Double
    var afterMB: Double
    var peakMB: Double
    var availableAtStartMB: Double
}
