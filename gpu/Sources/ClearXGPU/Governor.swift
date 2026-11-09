import Foundation

/// Thermal governor: raises the fan *floor* (F0Mn) ahead of GPU heat so the 575X holds max boost.
/// Only the minimum is written, so the SMC can still spin faster on its own — never slower than stock.
enum Profile: String, CaseIterable {
    case max, balanced, quiet
    var curve: (low: Double, high: Double) {   // °C where ramp starts / reaches fan max
        switch self { case .max: return (45, 68); case .balanced: return (55, 78); case .quiet: return (65, 88) }
    }
}

final class Governor {
    let smc: SMC, profile: Profile
    var stockMin = [Int: Double](), fanMax = [Int: Double](), fans = 0
    private var running = true

    init(_ p: Profile) throws {
        smc = try SMC(); profile = p
        fans = Int(smc.double("FNum") ?? 1)
        for i in 0..<fans {
            stockMin[i] = smc.double("F\(i)Mn") ?? 1200
            fanMax[i] = smc.double("F\(i)Mx") ?? 2700
        }
        saveStock()
    }

    // Stock mins persisted so `restore` works even after a crash.
    static let stockFile = "/Library/Application Support/ClearX/fan-stock.plist"
    func saveStock() {
        if FileManager.default.fileExists(atPath: Self.stockFile) {
            if let d = NSDictionary(contentsOfFile: Self.stockFile) as? [String: Double] {
                for (k, v) in d { if let i = Int(k) { stockMin[i] = v } }
            }
            return
        }
        try? FileManager.default.createDirectory(atPath: (Self.stockFile as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        (Dictionary(uniqueKeysWithValues: stockMin.map { ("\($0.key)", $0.value) }) as NSDictionary).write(toFile: Self.stockFile, atomically: true)
    }

    func restore() {
        for i in 0..<fans { if let m = stockMin[i] { try? smc.setRPM("F\(i)Mn", m) } }
        print("Fans restored to stock minimums.")
    }

    func target(_ t: Double, fan i: Int) -> Double {
        let (lo, hi) = profile.curve, mn = stockMin[i] ?? 1200, mx = fanMax[i] ?? 2700
        let x = min(1, max(0, (t - lo) / (hi - lo)))
        return (mn + (mx - mn) * x).rounded()
    }

    func run() {
        for s in [SIGINT, SIGTERM] {
            signal(s, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: s)
            src.setEventHandler { [weak self] in self?.running = false }
            src.resume(); sources.append(src)
        }
        print("Governor: \(profile.rawValue) profile, \(fans) fan(s). Ctrl-C restores stock.")
        var smoothed: Double?
        while running {
            let t = smc.gpuTemp() ?? GPU.stats().temp
            if let t {
                smoothed = smoothed.map { $0 * 0.6 + t * 0.4 } ?? t   // ramp fast, settle slowly
                let use = max(t, smoothed!)
                for i in 0..<fans {
                    let rpm = target(use, fan: i)
                    do { try smc.setRPM("F\(i)Mn", rpm) } catch { print("Fan write failed (run as root): \(error)"); running = false }
                }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(2))
        }
        restore()
    }
    private var sources: [DispatchSourceSignal] = []
}
