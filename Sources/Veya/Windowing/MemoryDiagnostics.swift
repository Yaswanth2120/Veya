import Darwin

/// Approximate resident memory usage via `task_info` — the standard,
/// public (if low-level) way to read a process's own memory footprint on
/// Darwin. DEBUG-only diagnostics use, never shown to end users.
enum MemoryDiagnostics {
    static func residentMemoryBytes() -> UInt64? {
        var info = mach_task_basic_info()
        var count = mach_msg_type_number_t(MemoryLayout<mach_task_basic_info>.size / MemoryLayout<natural_t>.size)

        let result = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(MACH_TASK_BASIC_INFO), $0, &count)
            }
        }

        guard result == KERN_SUCCESS else { return nil }
        return info.resident_size
    }
}
