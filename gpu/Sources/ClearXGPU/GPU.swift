import Foundation
import IOKit
import CoreGraphics

struct GPUStats {
    var util: Double?, coreMHz: Double?, memMHz: Double?, watts: Double?, temp: Double?, vramUsedMB: Double?
    var raw: [String: Any] = [:]
}

enum GPU {
    static func accelerator() -> io_object_t? {
        var found: io_object_t?
        for o in ioServices("IOAccelerator") {
            if found == nil, isAMD(o) { found = o } else { IOObjectRelease(o) }
        }
        return found
    }

    static func num(_ d: [String: Any], _ keys: [String]) -> Double? {
        for k in keys { if let n = d[k] as? NSNumber { return n.doubleValue } }
        return nil
    }

    static func stats() -> GPUStats {
        guard let a = accelerator() else { return GPUStats() }
        defer { IOObjectRelease(a) }
        let d = prop(a, "PerformanceStatistics") as? [String: Any] ?? [:]
        var s = GPUStats(raw: d)
        s.util = num(d, ["Device Utilization %", "GPU Activity(%)", "GPU Core Utilization"])
        s.coreMHz = num(d, ["Core Clock(MHz)"])
        s.memMHz = num(d, ["Memory Clock(MHz)"])
        s.watts = num(d, ["Total Power(W)"])
        s.temp = num(d, ["Temperature(C)"])
        if let b = num(d, ["vramUsedBytes", "In use system memory"]) { s.vramUsedMB = b / 1_048_576 }
        return s
    }

    /// Processes holding a GPU user client (pid, name).
    static func clients() -> [(pid: Int32, name: String)] {
        guard let a = accelerator() else { return [] }
        defer { IOObjectRelease(a) }
        var seen = Set<Int32>(), out: [(Int32, String)] = []
        for c in ioChildren(a) {
            defer { IOObjectRelease(c) }
            guard let s = prop(c, "IOUserClientCreator") as? String,
                  s.hasPrefix("pid "), let comma = s.firstIndex(of: ","),
                  let pid = Int32(s.dropFirst(4).prefix(upTo: comma)) else { continue }
            if seen.insert(pid).inserted {
                out.append((pid, s[s.index(after: comma)...].trimmingCharacters(in: .whitespaces)))
            }
        }
        return out.sorted { $0.1 < $1.1 }
    }
}

enum Display {
    /// Switch main display to "Default" HiDPI (5120x2880 pixels shown as 2560x1440) — no supersampling.
    static func setNative() -> String {
        let id = CGMainDisplayID()
        let opts = [kCGDisplayShowDuplicateLowResolutionModes: true] as CFDictionary
        guard let modes = CGDisplayCopyAllDisplayModes(id, opts) as? [CGDisplayMode],
              let cur = CGDisplayCopyDisplayMode(id) else { return "Couldn't read display modes." }
        // Panel's true width = widest 1:1 mode (5120 on the 5K iMac). Supersampled modes can exceed it.
        let panel = modes.filter { $0.width == $0.pixelWidth }.map(\.pixelWidth).max() ?? 0
        guard let target = modes.filter({ $0.pixelWidth == panel && $0.width * 2 == panel })
                .max(by: { $0.refreshRate < $1.refreshRate }) else { return "No native HiDPI mode found." }
        if cur.pixelWidth == target.pixelWidth && cur.width == target.width { return "Already native (\(target.width)x\(target.height) HiDPI)." }
        var cfg: CGDisplayConfigRef?
        CGBeginDisplayConfiguration(&cfg)
        CGConfigureDisplayWithDisplayMode(cfg, id, target, nil)
        let r = CGCompleteDisplayConfiguration(cfg, .permanently)
        return r == .success ? "Switched from \(cur.width)x\(cur.height) to native \(target.width)x\(target.height) HiDPI."
                             : "Display change failed (\(r.rawValue))."
    }
}
