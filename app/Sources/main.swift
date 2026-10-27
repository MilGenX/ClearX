// MemGuard menu-bar app. Reads live stats natively (Mach, libproc, IOKit, SMC) every 2 s,
// reads memguard's state.json, and calls the memguard CLI for every action.
import SwiftUI
import AppKit
import IOKit
import Darwin
import UserNotifications

let MG_DIR = NSString(string: "~/Developer/MacMemoryManager").expandingTildeInPath
let MG_CLI = MG_DIR + "/clearx"
let STATE = MG_DIR + "/state/state.json"
let OFF = MG_DIR + "/state/disabled"

// MARK: - system readers

struct ProcInfo: Identifiable {
    let id: Int32
    let name: String
    let mem: UInt64      // phys footprint
    var cpu: Double      // % of one core
}

func sysctlInt(_ name: String) -> Int {
    var v: Int32 = 0; var sz = MemoryLayout<Int32>.size
    return sysctlbyname(name, &v, &sz, nil, 0) == 0 ? Int(v) : -1
}

func swapUsage() -> (used: UInt64, total: UInt64) {
    var x = xsw_usage(); var sz = MemoryLayout<xsw_usage>.size
    sysctlbyname("vm.swapusage", &x, &sz, nil, 0)
    return (x.xsu_used, x.xsu_total)
}

func vmStats() -> vm_statistics64 {
    var s = vm_statistics64()
    var count = mach_msg_type_number_t(MemoryLayout<vm_statistics64>.size / MemoryLayout<integer_t>.size)
    _ = withUnsafeMutablePointer(to: &s) {
        $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
            host_statistics64(mach_host_self(), HOST_VM_INFO64, $0, &count)
        }
    }
    return s
}

final class CoreLoad {
    var prev: [UInt32] = []
    func sample() -> [Double] {
        var cpuInfo: processor_info_array_t?; var n: mach_msg_type_number_t = 0; var cpus: natural_t = 0
        guard host_processor_info(mach_host_self(), PROCESSOR_CPU_LOAD_INFO, &cpus, &cpuInfo, &n) == KERN_SUCCESS,
              let info = cpuInfo else { return [] }
        defer { vm_deallocate(mach_task_self_, vm_address_t(bitPattern: info), vm_size_t(n) * 4) }
        let cur = (0..<Int(n)).map { UInt32(bitPattern: info[$0]) }
        defer { prev = cur }
        guard prev.count == cur.count else { return Array(repeating: 0, count: Int(cpus)) }
        return (0..<Int(cpus)).map { c in
            let b = c * Int(CPU_STATE_MAX)
            let d = (0..<Int(CPU_STATE_MAX)).map { Double(cur[b + $0] &- prev[b + $0]) }
            let total = d.reduce(0, +)
            return total > 0 ? 100 * (total - d[Int(CPU_STATE_IDLE)]) / total : 0
        }
    }
}

final class ProcSampler {
    var lastCPU: [Int32: (UInt64, Double)] = [:]
    var tb = mach_timebase_info_data_t()
    init() { mach_timebase_info(&tb) }
    func sample() -> [ProcInfo] {
        let n = proc_listallpids(nil, 0)
        var pids = [Int32](repeating: 0, count: Int(n) + 64)
        let got = proc_listallpids(&pids, Int32(pids.count * MemoryLayout<Int32>.size))
        let now = Date().timeIntervalSince1970
        let uid = getuid()
        var out: [ProcInfo] = []; var seen: [Int32: (UInt64, Double)] = [:]
        for pid in pids.prefix(Int(got)) where pid > 0 {
            var bsd = proc_bsdshortinfo()
            guard proc_pidinfo(pid, PROC_PIDT_SHORTBSDINFO, 0, &bsd, Int32(MemoryLayout<proc_bsdshortinfo>.size)) > 0,
                  bsd.pbsi_uid == uid else { continue }
            var ru = rusage_info_v2()
            let ok = withUnsafeMutablePointer(to: &ru) {
                $0.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) { proc_pid_rusage(pid, RUSAGE_INFO_V2, $0) }
            }
            guard ok == 0 else { continue }
            let ticks = ru.ri_user_time + ru.ri_system_time
            var cpu = 0.0
            if let (t0, w0) = lastCPU[pid], now > w0 {
                let ns = Double(ticks &- t0) * Double(tb.numer) / Double(tb.denom)
                cpu = 100 * ns / 1e9 / (now - w0)
            }
            seen[pid] = (ticks, now)
            var buf = [CChar](repeating: 0, count: 256)
            proc_name(pid, &buf, 256)
            var name = String(cString: buf)
            if name.isEmpty { name = withUnsafeBytes(of: bsd.pbsi_comm) { String(decoding: $0.prefix { $0 != 0 }, as: UTF8.self) } }
            out.append(ProcInfo(id: pid, name: name, mem: ru.ri_phys_footprint, cpu: cpu))
        }
        lastCPU = seen
        return out
    }
}

func gpuStats() -> (used: UInt64, util: Int)? {
    var it: io_iterator_t = 0
    guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IOAccelerator"), &it) == KERN_SUCCESS else { return nil }
    defer { IOObjectRelease(it) }
    var best: (UInt64, Int)? = nil
    while case let e = IOIteratorNext(it), e != 0 {
        defer { IOObjectRelease(e) }
        guard let p = IORegistryEntryCreateCFProperty(e, "PerformanceStatistics" as CFString, kCFAllocatorDefault, 0)?
                .takeRetainedValue() as? [String: Any],
              let used = (p["inUseVidMemoryBytes"] as? NSNumber)?.uint64Value else { continue }
        let util = (p["Device Utilization %"] as? NSNumber)?.intValue ?? 0
        if best == nil || used > best!.0 { best = (used, util) }
    }
    return best
}

// SMC temperature (no root needed). Layout must be exactly 80 bytes.
struct SMCVers { var major: UInt8 = 0, minor: UInt8 = 0, build: UInt8 = 0, reserved: UInt8 = 0; var release: UInt16 = 0 }
struct SMCPLimit { var version: UInt16 = 0, length: UInt16 = 0; var cpu: UInt32 = 0, gpu: UInt32 = 0, mem: UInt32 = 0 }
struct SMCKeyInfo { var dataSize: UInt32 = 0, dataType: UInt32 = 0; var attributes: UInt8 = 0 }
struct SMCParam {
    var key: UInt32 = 0; var vers = SMCVers(); var pLimit = SMCPLimit(); var keyInfo = SMCKeyInfo()
    var padding: UInt16 = 0; var result: UInt8 = 0, status: UInt8 = 0, data8: UInt8 = 0; var data32: UInt32 = 0
    var bytes: (UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8,
                UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8) =
        (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
}

final class SMC {
    var conn: io_connect_t = 0
    init?() {
        guard MemoryLayout<SMCParam>.stride == 80 else { return nil }
        let svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSMC"))
        guard svc != 0 else { return nil }
        defer { IOObjectRelease(svc) }
        guard IOServiceOpen(svc, mach_task_self_, 0, &conn) == KERN_SUCCESS else { return nil }
    }
    private func call(_ inp: inout SMCParam) -> SMCParam? {
        var out = SMCParam(); var sz = MemoryLayout<SMCParam>.stride
        let r = IOConnectCallStructMethod(conn, 2, &inp, MemoryLayout<SMCParam>.stride, &out, &sz)
        return r == KERN_SUCCESS && out.result == 0 ? out : nil
    }
    func temp(_ key: String) -> Double? {
        let k = key.utf8.reduce(UInt32(0)) { $0 << 8 | UInt32($1) }
        var p = SMCParam(); p.key = k; p.data8 = 9
        guard let info = call(&p) else { return nil }
        var q = SMCParam(); q.key = k; q.keyInfo.dataSize = info.keyInfo.dataSize; q.data8 = 5
        guard let r = call(&q) else { return nil }
        let t = Double(Int8(bitPattern: r.bytes.0)) + Double(r.bytes.1) / 256
        return t > 5 && t < 130 ? t : nil
    }
    func cpuTemp() -> Double? { for k in ["TC0P", "TC0D", "TC0F", "TC0E", "TCXC"] { if let t = temp(k) { return t } }; return nil }
}

@discardableResult
func mg(_ args: [String]) -> String {
    let p = Process(); p.executableURL = URL(fileURLWithPath: MG_CLI); p.arguments = args
    let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
    try? p.run(); p.waitUntilExit()
    return String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
}

// MARK: - model

final class Model: ObservableObject {
    // refresh() reassigns everything every 2 s, so one objectWillChange per refresh is enough
    var level = "green", mode = "normal", modeSetting = "auto"
    var pressurePct = 0, availB: UInt64 = 0, freeB: UInt64 = 0, activeB: UInt64 = 0, wiredB: UInt64 = 0, compB: UInt64 = 0
    var swapUsed: UInt64 = 0, swapRate = 0.0, swapHist: [Double] = []
    var vramUsed: UInt64 = 0, gpuUtil = 0
    var cores: [Double] = [], topMem: [ProcInfo] = [], topCPU: [ProcInfo] = []
    var paused: [(pid: String, name: String, why: String)] = []
    var bg: Set<Int32> = [], throttled: Set<Int32> = []
    var speedLimit = 100, hotSustained = false, temp: Double? = nil, enabled = true, tte: Double? = nil
    // the 20 GB pool from state.json (written by the daemon every tick)
    var poolSeg: [String: UInt64] = [:], poolTotal: UInt64 = 20 << 30, poolWhy: [String] = []
    var spillRate = 0.0, spilling = false, diskFree: UInt64 = 0, diskLat = 0.0, tight = false, xcodeJobs = 0
    let vramTotal: UInt64 = 4096 << 20
    let pageSize = UInt64(vm_kernel_page_size)
    private let coreLoad = CoreLoad(), procs = ProcSampler(), smc = SMC()
    private var lastSwapouts: UInt64 = 0, lastT = Date()
    private var timer: Timer?

    init() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { _, _ in }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
    }

    /// The daemon leaves its notifications in state/notify.json so they show ClearX's own name and icon
    /// (osascript ones show Script Editor's); without notification permission, fall back to osascript.
    func postNotice() {
        let f = MG_DIR + "/state/notify.json"
        guard let d = try? Data(contentsOf: URL(fileURLWithPath: f)),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return }
        try? FileManager.default.removeItem(atPath: f)
        let title = j["title"] as? String ?? "", msg = j["msg"] as? String ?? ""
        let c = UNMutableNotificationContent(); c.title = title; c.body = msg
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil)) { err in
            guard err != nil else { return }
            let esc = { (s: String) in s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") }
            let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            p.arguments = ["-e", "display notification \"\(esc(msg))\" with title \"ClearX\" subtitle \"\(esc(title))\""]
            try? p.run()
        }
    }

    func refresh() {
        objectWillChange.send()
        postNotice()
        let s = vmStats(), now = Date()
        freeB = UInt64(s.free_count + s.speculative_count) * pageSize
        availB = freeB + UInt64(s.inactive_count + s.purgeable_count) * pageSize
        activeB = UInt64(s.active_count) * pageSize
        wiredB = UInt64(s.wire_count) * pageSize
        compB = UInt64(s.compressor_page_count) * pageSize
        let dt = now.timeIntervalSince(lastT)
        if lastSwapouts > 0 && dt > 0 { swapRate = Double(s.swapouts &- lastSwapouts) * Double(pageSize) / 1048576 / dt }
        lastSwapouts = s.swapouts; lastT = now
        swapHist.append(swapRate); if swapHist.count > 300 { swapHist.removeFirst(swapHist.count - 300) }
        swapUsed = swapUsage().used
        let lvl = sysctlInt("kern.memorystatus_level")
        pressurePct = lvl >= 0 ? 100 - lvl : 0
        if let g = gpuStats() { vramUsed = g.used; gpuUtil = g.util }
        cores = coreLoad.sample()
        let all = procs.sample()
        topMem = Array(all.sorted { $0.mem > $1.mem }.prefix(8))
        topCPU = Array(all.sorted { $0.cpu > $1.cpu }.prefix(6))
        temp = smc?.cpuTemp()
        enabled = !FileManager.default.fileExists(atPath: OFF)
        if let d = try? Data(contentsOf: URL(fileURLWithPath: MG_DIR + "/state/maint.json")),
           let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let h = j["health"] as? [String: Any] {
            health = h["score"] as? Int ?? 0; healthWhy = h["why"] as? [String] ?? []
            let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"
            ssdToday = num(((j["ssd"] as? [String: Any])?["days"] as? [String: Any])?[f.string(from: Date())])
        }
        if let a = try? FileManager.default.attributesOfFileSystem(forPath: "/System/Volumes/Data") {
            diskTotal = (a[.systemSize] as? NSNumber)?.uint64Value ?? 1
        }
        loadNeeds()
        if let d = try? Data(contentsOf: URL(fileURLWithPath: STATE)),
           let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
            level = j["level"] as? String ?? level
            mode = j["mode"] as? String ?? mode
            modeSetting = (j["mode_override"] as? String) ?? "auto"
            speedLimit = j["cpu_speed_limit"] as? Int ?? 100
            hotSustained = j["thermal_sustained"] as? Bool ?? false
            tte = j["tte"] as? Double
            paused = ((j["paused"] as? [String: [String: Any]]) ?? [:]).map {
                (pid: $0.key, name: $0.value["name"] as? String ?? "?", why: $0.value["why"] as? String ?? "")
            }.sorted { $0.pid < $1.pid }
            bg = Set(((j["bg"] as? [String: Any]) ?? [:]).keys.compactMap { Int32($0) })
            throttled = Set(((j["throttled"] as? [String: Any]) ?? [:]).keys.compactMap { Int32($0) })
            xcodeJobs = j["xcode_jobs"] as? Int ?? 0
            reserve = j["reserve"] as? [String: Any]
            if let p = j["pool"] as? [String: Any] {
                poolSeg = ((p["seg"] as? [String: Any]) ?? [:]).mapValues { num($0) }
                poolTotal = max(num(p["total"]), 1)
                poolWhy = p["why"] as? [String] ?? []
                spillRate = (p["spill_rate"] as? NSNumber)?.doubleValue ?? 0
                spilling = p["spilling"] as? Bool ?? false
                diskFree = num(p["disk_free"]); diskLat = (p["disk_lat"] as? NSNumber)?.doubleValue ?? 0
                tight = p["tight"] as? Bool ?? false
            }
        }
        // what Finder calls "available" = free + purgeable (space macOS clears by itself when needed); after diskFree
        // is read for this refresh
        if let v = try? URL(fileURLWithPath: "/").resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]),
           let imp = v.volumeAvailableCapacityForImportantUsage {
            diskPurgeable = UInt64(max(imp, 0)) > diskFree ? UInt64(imp) - diskFree : 0
        }
    }

    var health = 0, healthWhy: [String] = [], needs: [[String: Any]] = [], diskTotal: UInt64 = 1, diskPurgeable: UInt64 = 0
    @Published var detailsTab = 0
    var reserve: [String: Any]? = nil
    var ssdToday: UInt64 = 0
    private var lastNeeds = Date.distantPast
    func loadNeeds(force: Bool = false) {
        guard force || Date().timeIntervalSince(lastNeeds) > 120 else { return }
        lastNeeds = Date()
        DispatchQueue.global().async {
            let out = mg(["maintain", "needs"])
            let j = out.split(separator: "\n").last.flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [[String: Any]] }
            DispatchQueue.main.async { self.needs = j ?? []; self.objectWillChange.send() }
        }
    }
    @Published var boost: [String: Any]? = nil
    @Published var boosting = false
    @Published var quitPick: Set<String> = []
    func speedUp(quit: [String] = []) {
        boosting = true
        DispatchQueue.global().async {
            let out = mg(["boost", "--json"] + (quit.isEmpty ? [] : ["--quit", quit.joined(separator: ",")]))
            let j = out.split(separator: "\n").last.flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            DispatchQueue.main.async {
                self.boosting = false; self.boost = j
                self.quitPick = Set(((j?["idle"] as? [[String: Any]]) ?? []).filter { $0["safe"] as? Bool ?? false }.compactMap { $0["name"] as? String })
                self.refresh()
            }
        }
    }

    func doNeed(_ kind: String, open: @escaping () -> Void) {
        if kind.hasPrefix("open:"), let u = URL(string: String(kind.dropFirst(5))) { NSWorkspace.shared.open(u) }
        else if kind.hasPrefix("details:") { open(); NSApp.activate(ignoringOtherApps: true) }
        else if kind.hasPrefix("fix:") {
            let what = String(kind.dropFirst(4))
            DispatchQueue.global().async { mg(["maintain", "fix", what]); DispatchQueue.main.async { self.loadNeeds(force: true) } }
        }
    }

    func act(_ args: [String]) {
        DispatchQueue.global().async { mg(args); DispatchQueue.main.async { self.refresh() } }
    }

    var color: NSColor {
        switch level { case "red": return .systemRed; case "orange": return .systemOrange
                       case "yellow": return .systemYellow; default: return .systemGreen }
    }
}

func gb(_ b: UInt64) -> String { String(format: "%.1f GB", Double(b) / 1073741824) }
/// Disk sizes the way Finder shows them (1 GB = 1,000,000,000 bytes), so the numbers match Finder's.
func gbd(_ b: UInt64) -> String { String(format: "%.1f GB", Double(b) / 1e9) }
func mb(_ b: UInt64) -> String { b >= 1 << 30 ? gb(b) : "\(b >> 20) MB" }

// MARK: - views

struct Sparkline: View {
    let values: [Double]
    var body: some View {
        GeometryReader { g in
            let mx = max(values.max() ?? 1, 1)
            Path { p in
                for (i, v) in values.enumerated() {
                    let x = g.size.width * CGFloat(i) / CGFloat(max(values.count - 1, 1))
                    let y = g.size.height * (1 - CGFloat(v / mx))
                    i == 0 ? p.move(to: CGPoint(x: x, y: y)) : p.addLine(to: CGPoint(x: x, y: y))
                }
            }.stroke(Theme.warn, lineWidth: 1.5)
        }
        .frame(height: 28)
        .background(RoundedRectangle(cornerRadius: 6).fill(Theme.line.opacity(0.5)))
    }
}

struct Bar: View {
    let frac: Double
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Rectangle().fill(Theme.line)
                Rectangle().fill(frac > 0.9 ? Theme.bad : frac > 0.7 ? Theme.warn : Theme.accent)
                    .frame(width: g.size.width * CGFloat(min(max(frac, 0), 1)))
            }
        }.frame(height: 6).cornerRadius(2)
    }
}

// One bar for RAM + VRAM: app | wired | compressed | GPU spill | VRAM | cached | free, swap drawn past the end
let POOL_SEGS: [(String, String, Color)] = [("app", "Apps", Theme.info), ("wired", "System", Color(red: 0.70, green: 0.55, blue: 1.0)), ("compressed", "Compressed", Theme.warn),
                                            ("spill", "Graphics overflow", Theme.bad), ("vram", "Graphics", Color(red: 0.36, green: 0.83, blue: 0.95)), ("cached", "Reusable cache", Theme.accent.opacity(0.45)),
                                            ("free", "Unused", Theme.accent)]

struct PoolBar: View {
    let seg: [String: UInt64], total: UInt64, swap: UInt64
    var body: some View {
        GeometryReader { g in
            let over = min(Double(swap) / Double(total), 0.25)
            let w = g.size.width / CGFloat(1 + over)
            HStack(spacing: 0) {
                ForEach(POOL_SEGS, id: \.0) { k, _, c in
                    Rectangle().fill(c).frame(width: w * CGFloat(Double(seg[k] ?? 0) / Double(total)))
                }
                Rectangle().fill(Theme.bad.opacity(0.55)).frame(width: w * CGFloat(over))   // swap overflow
                    .overlay(Rectangle().stroke(Theme.bad, style: StrokeStyle(lineWidth: 1, dash: [2])))
                Spacer(minLength: 0)
            }
        }.frame(height: 14).background(Theme.line).clipShape(RoundedRectangle(cornerRadius: 4))
    }
}

// Speed Up result: measured before/after, what was done, idle apps to quit (graceful, opt-in)
struct BoostView: View {
    @ObservedObject var m: Model
    let b: [String: Any]
    var body: some View {
        let bf = b["before"] as? [String: Any] ?? [:], af = b["after"] as? [String: Any] ?? [:]
        let idle = b["idle"] as? [[String: Any]] ?? []
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("Speed Up").font(.caption).bold()
                Spacer()
                Button("Close") { m.boost = nil }.buttonStyle(.borderless).font(.caption)
            }
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 1) {
                GridRow { Text(""); Text("before"); Text("after") }.foregroundColor(.secondary)
                ForEach([("Available", "avail"), ("App memory", "app"), ("Compressed", "compressed"), ("Swap", "swap")], id: \.1) { l, k in
                    GridRow { Text(l); Text(gb(num(bf[k]))).monospacedDigit(); Text(gb(num(af[k]))).monospacedDigit() }
                }
                GridRow { Text("Pressure"); Text("\(num(bf["pressure"]))"); Text("\(num(af["pressure"]))") }
            }.font(.caption2)
            ForEach(Array(((b["actions"] as? [String]) ?? []).prefix(8).enumerated()), id: \.offset) { _, a in
                Text("• " + a).font(.caption2).lineLimit(1)
            }
            if !idle.isEmpty {
                Text("Idle apps (30+ min, > 300 MB): quit politely, they ask to save").font(.caption2).foregroundColor(.secondary)
                ForEach(Array(idle.enumerated()), id: \.offset) { _, a in
                    let n = a["name"] as? String ?? ""
                    HStack {
                        Toggle(isOn: Binding(get: { m.quitPick.contains(n) },
                                             set: { if $0 { m.quitPick.insert(n) } else { m.quitPick.remove(n) } })) {
                            Text("\(n) · \(mb(num(a["mem"]))) · idle \(num(a["idle_min"])) min")
                        }.disabled(a["never"] as? Bool ?? false).controlSize(.mini)
                    }.font(.caption2)
                }
                Button("Quit selected") { m.speedUp(quit: Array(m.quitPick)) }.font(.caption).disabled(m.quitPick.isEmpty)
            }
        }.padding(10).foregroundColor(Theme.text)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.card)).overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.line))
    }
}

// Black theme: one ground, one card tone, one accent; status colours only where something needs attention.
enum Theme {
    static let bg = Color.black
    static let card = Color(red: 0.043, green: 0.043, blue: 0.051)     // #0B0B0D
    static let line = Color(red: 0.122, green: 0.122, blue: 0.133)     // #1F1F22
    static let text = Color(red: 0.949, green: 0.949, blue: 0.949)     // #F2F2F2
    static let sub = Color(red: 0.604, green: 0.604, blue: 0.627)      // #9A9AA0
    static let accent = Color(red: 0.486, green: 0.961, blue: 0.769)   // #7CF5C4
    static let info = Color(red: 0.541, green: 0.706, blue: 1.0)       // #8AB4FF
    static let warn = Color(red: 1.0, green: 0.710, blue: 0.278)       // #FFB547
    static let bad = Color(red: 1.0, green: 0.420, blue: 0.420)        // #FF6B6B
}

struct AccentButton: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 15, weight: .bold)).foregroundColor(.black)
            .frame(maxWidth: .infinity, minHeight: 44)
            .background(RoundedRectangle(cornerRadius: 12).fill(Theme.accent.opacity(configuration.isPressed ? 0.75 : 1)))
    }
}

struct GhostButton: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { GhostLabel(c: configuration) }
}

struct GhostLabel: View {
    let c: ButtonStyleConfiguration
    @Environment(\.isEnabled) var enabled
    var body: some View {
        c.label.font(.system(size: 12, weight: .semibold)).foregroundColor(Theme.text)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(Capsule().fill(Theme.line.opacity(c.isPressed ? 1 : 0.7)))
            .opacity(enabled ? 1 : 0.4)
    }
}

struct ScoreRing: View {
    let score: Int, color: Color
    var body: some View {
        ZStack {
            Circle().stroke(Theme.line, lineWidth: 8)
            Circle().trim(from: 0, to: CGFloat(min(max(score, 0), 100)) / 100)
                .stroke(color, style: StrokeStyle(lineWidth: 8, lineCap: .round)).rotationEffect(.degrees(-90))
            Text("\(score)").font(.system(size: 28, weight: .bold, design: .rounded)).foregroundColor(Theme.text)
        }.frame(width: 84, height: 84).animation(.easeOut(duration: 0.6), value: score)
    }
}

// The menu: one score, one sentence, three bars, Speed Up, and at most 3 things that need you.
struct SimpleBar: View {
    let title: String, frac: Double, word: String, color: Color
    var detail = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack { Text(title).foregroundColor(Theme.text); Spacer(); Text(word).fontWeight(.semibold).foregroundColor(color) }
                .font(.system(size: 13))
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.line)
                    Capsule().fill(color).frame(width: g.size.width * CGFloat(min(max(frac, 0.03), 1)))
                }
            }.frame(height: 6)
            if !detail.isEmpty { Text(detail).font(.system(size: 11)).foregroundColor(Theme.sub) }
        }
    }
}

struct Panel: View {
    @ObservedObject var m: Model
    @Environment(\.openWindow) var openWindow
    func open(_ tab: Int? = nil) {
        if let t = tab { m.detailsTab = t }
        openWindow(id: "details"); NSApp.activate(ignoringOtherApps: true)
    }
    var greeting: String {
        let h = Calendar.current.component(.hour, from: Date())
        let part = h < 5 ? "Working late" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"
        let first = NSFullUserName().split(separator: " ").first.map(String.init) ?? ""
        return first.isEmpty ? part : "\(part), \(first)"
    }
    var memWord: (String, Color) {
        switch m.level { case "red": return ("Strained", Theme.bad); case "orange": return ("Strained", Theme.warn)
                         case "yellow": return ("Busy", Theme.info); default: return ("Good", Theme.accent) }
    }
    var cpuAvg: Double { m.cores.isEmpty ? 0 : m.cores.reduce(0, +) / Double(m.cores.count) }
    var cpuWord: (String, Color) {
        m.hotSustained ? ("Hot", Theme.bad) : m.speedLimit < 100 ? ("Warm", Theme.warn) :
            cpuAvg > 80 ? ("Busy", Theme.info) : ("Good", Theme.accent)
    }
    var diskWord: (String, Color) {
        m.diskFree < 20 << 30 ? ("Almost full", Theme.bad) : m.diskFree < 40 << 30 ? ("Getting full", Theme.warn) : ("Good", Theme.accent)
    }
    var scoreColor: Color { m.health >= 85 ? Theme.accent : m.health >= 65 ? Theme.warn : Theme.bad }
    var status: (String, Color) {
        if !m.enabled { return ("Off", Theme.sub) }
        switch m.level { case "red", "orange": return ("Protecting", Theme.warn); case "yellow": return ("Busy", Theme.info)
                         default: return ("All good", Theme.accent) }
    }
    var headline: String {
        if !m.enabled { return "ClearX is off" }
        switch m.level {
        case "red": return "Very short on memory"
        case "orange": return "Short on memory"
        case "yellow": return "Your Mac is busy"
        default: return m.speedLimit < 100 ? "Running warm" : "Your Mac is in good shape"
        }
    }
    var sentence: String {
        if !m.enabled { return "Your Mac is on its own until you turn it back on." }
        switch m.level {
        case "red": return "Background apps are paused so what you're using keeps going."
        case "orange": return "Background apps are paused for a moment."
        case "yellow": return "ClearX is quietly slowing background apps."
        default: return m.speedLimit < 100 ? "Builds are slowed a little to cool it down." : "Nothing needs doing right now."
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 1) {
                    Text(greeting).font(.system(size: 11)).foregroundColor(Theme.sub)
                    Text("Your iMac").font(.system(size: 15, weight: .semibold)).foregroundColor(Theme.text)
                }
                Spacer()
                Text(status.0).font(.system(size: 11, weight: .semibold)).foregroundColor(status.1)
                    .padding(.horizontal, 10).padding(.vertical, 4)
                    .background(Capsule().fill(Theme.card)).overlay(Capsule().stroke(Theme.line))
            }
            HStack(spacing: 16) {
                ScoreRing(score: m.health, color: scoreColor)
                    .help(m.healthWhy.isEmpty ? "Nothing is taking points off." : m.healthWhy.joined(separator: "\n"))
                VStack(alignment: .leading, spacing: 4) {
                    Text(headline).font(.system(size: 16, weight: .semibold)).foregroundColor(Theme.text)
                    Text(sentence).font(.system(size: 12)).foregroundColor(Theme.sub).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(14)
            .background(RoundedRectangle(cornerRadius: 12).fill(Theme.card))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.line))
            Button(action: { m.speedUp() }) { Text(m.boosting ? "Speeding up…" : "Speed Up") }
                .buttonStyle(AccentButton()).disabled(m.boosting || !m.enabled).opacity(m.boosting || !m.enabled ? 0.5 : 1)
            if let b = m.boost { BoostView(m: m, b: b) }
            FrozenRow()
            ReserveRow(m: m)
            SimpleBar(title: "Memory", frac: Double(m.poolTotal - (m.poolSeg["free"] ?? 0) - (m.poolSeg["cached"] ?? 0)) / Double(m.poolTotal),
                      word: memWord.0, color: memWord.1,
                      detail: "\(gb(m.availB)) of 16 GB ready for apps right now")
            SimpleBar(title: "Processor", frac: cpuAvg / 100, word: cpuWord.0, color: cpuWord.1,
                      detail: String(format: "%.0f%% busy%@", cpuAvg, m.hotSustained ? " · easing off background work to cool down" : m.speedLimit < 100 ? " · warm, cooling" : ""))
            Button(action: { open(0) }) {
                SimpleBar(title: "Storage", frac: 1 - Double(m.diskFree + m.diskPurgeable) / Double(max(m.diskTotal, 1)),
                          word: diskWord.0, color: diskWord.1,
                          detail: "\(gbd(m.diskFree + m.diskPurgeable)) free of \(gbd(m.diskTotal)) · SSD healthy\(m.ssdToday > 100 << 30 ? ", busy today" : "")")
                    .contentShape(Rectangle())
            }.buttonStyle(.plain)
            if !m.needs.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("NEEDS YOU").font(.system(size: 10, weight: .semibold)).kerning(0.8).foregroundColor(Theme.sub)
                    ForEach(Array(m.needs.prefix(3).enumerated()), id: \.offset) { _, n in
                        HStack(spacing: 10) {
                            Circle().fill(Theme.warn).frame(width: 7, height: 7)
                            Text(n["text"] as? String ?? "").font(.system(size: 12)).foregroundColor(Theme.text)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer()
                            Button(n["button"] as? String ?? "Fix") { m.doNeed(n["kind"] as? String ?? "", open: { open() }) }
                                .buttonStyle(GhostButton())
                        }
                    }
                }
                .padding(.top, 10).overlay(Rectangle().fill(Theme.line).frame(height: 1), alignment: .top)
            }
            HStack {
                Button("Details…") { open() }.buttonStyle(.plain).foregroundColor(Theme.sub)
                Spacer()
                Toggle("On", isOn: Binding(get: { m.enabled }, set: { m.act([$0 ? "on" : "off"]) }))
                    .toggleStyle(.switch).controlSize(.mini).tint(Theme.accent).foregroundColor(Theme.sub)
                Button("Quit") { NSApp.terminate(nil) }.buttonStyle(.plain).foregroundColor(Theme.sub)
            }
            .font(.system(size: 12))
            .padding(.top, 10).overlay(Rectangle().fill(Theme.line).frame(height: 1), alignment: .top)
        }
        .padding(18)
        .frame(width: 360)
        .background(Theme.bg)
        .environment(\.colorScheme, .dark)
    }
}

// Everything else lives here: live numbers, processes, cleanup, storage, optimize, security.
// Details window: sidebar + one page at a time, every section in a card.
let DETAIL_TABS: [(Int, String, String)] = [(1, "Right now", "waveform.path.ecg"), (0, "Storage", "internaldrive"),
                                            (2, "Clean up", "sparkles"), (3, "Checks", "checkmark.shield")]

struct Card<Trailing: View, Content: View>: View {
    let title: String
    let trailing: Trailing
    let content: Content
    init(_ title: String, @ViewBuilder trailing: () -> Trailing, @ViewBuilder content: () -> Content) {
        self.title = title; self.trailing = trailing(); self.content = content()
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !title.isEmpty {
                HStack(spacing: 8) {
                    Text(title.uppercased()).font(.system(size: 10, weight: .semibold)).kerning(0.8).foregroundColor(Theme.sub)
                    Spacer()
                    trailing
                }
            }
            content
        }
        .padding(16).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.card))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.line))
    }
}

extension Card where Trailing == EmptyView {
    init(_ title: String, @ViewBuilder content: () -> Content) { self.init(title, trailing: { EmptyView() }, content: content) }
}

struct PageHeader: View {
    let title: String, sub: String
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.system(size: 22, weight: .bold)).foregroundColor(Theme.text)
            Text(sub).font(.system(size: 13)).foregroundColor(Theme.sub).fixedSize(horizontal: false, vertical: true)
        }
    }
}

// One line in a list: name, a number, then its buttons.
struct ProcRow<Actions: View>: View {
    let name: String, value: String
    let actions: Actions
    init(name: String, value: String, @ViewBuilder actions: () -> Actions) {
        self.name = name; self.value = value; self.actions = actions()
    }
    var body: some View {
        HStack(spacing: 8) {
            Text(name).lineLimit(1).truncationMode(.middle).foregroundColor(Theme.text)
            Spacer()
            Text(value).monospacedDigit().foregroundColor(Theme.sub)
            actions
        }.font(.system(size: 12)).padding(.vertical, 2)
    }
}

struct CheckLine: View {
    let ok: Bool, text: String
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill").foregroundColor(ok ? Theme.accent : Theme.warn)
            Text(text).foregroundColor(Theme.text).fixedSize(horizontal: false, vertical: true)
        }.font(.system(size: 12))
    }
}

func legendDot(_ c: Color, _ t: String) -> some View {
    HStack(spacing: 5) { Circle().fill(c).frame(width: 7, height: 7); Text(t) }
}

struct BusyBadge: View {
    @ObservedObject var mm: MaintModel
    var body: some View {
        if let b = mm.busy {
            HStack(spacing: 6) { ProgressView().controlSize(.small); Text(b).font(.system(size: 11)).foregroundColor(Theme.sub) }
        }
    }
}

struct OutputCard: View {
    @ObservedObject var mm: MaintModel
    var body: some View {
        if !mm.output.isEmpty {
            Card("Last result") {
                ScrollView {
                    Text(mm.output).font(.system(size: 10, design: .monospaced)).foregroundColor(Theme.sub).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.frame(height: 110)
            }
        }
    }
}

struct DetailsView: View {
    @ObservedObject var m: Model
    @StateObject var mm = MaintModel()
    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Circle().fill(Color(m.color)).frame(width: 8, height: 8)
                    Text("ClearX").font(.system(size: 15, weight: .bold)).foregroundColor(Theme.text)
                }.padding(.horizontal, 10).padding(.bottom, 14)
                ForEach(DETAIL_TABS, id: \.0) { t, label, icon in
                    let on = m.detailsTab == t
                    Button(action: { m.detailsTab = t }) {
                        HStack(spacing: 8) { Image(systemName: icon).frame(width: 16); Text(label); Spacer() }
                            .font(.system(size: 13, weight: on ? .semibold : .regular))
                            .foregroundColor(on ? Theme.text : Theme.sub)
                            .padding(.horizontal, 10).padding(.vertical, 8)
                            .background(RoundedRectangle(cornerRadius: 8).fill(on ? Theme.line : Color.clear))
                            .contentShape(Rectangle())
                    }.buttonStyle(.plain)
                }
                Spacer()
                Button("Open settings file") { NSWorkspace.shared.open(URL(fileURLWithPath: MG_DIR + "/memguard.conf")) }
                    .buttonStyle(.plain).font(.system(size: 11)).foregroundColor(Theme.sub).padding(.horizontal, 10)
            }
            .padding(.vertical, 20).padding(.horizontal, 12)
            .frame(width: 190).frame(maxHeight: .infinity, alignment: .top)
            .background(Theme.card)
            .overlay(Rectangle().fill(Theme.line).frame(width: 1), alignment: .trailing)
            ScrollView {
                page.padding(24).frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(minWidth: 760, minHeight: 640)
        .background(Theme.bg).foregroundColor(Theme.text).font(.system(size: 12))
        .buttonStyle(GhostButton()).tint(Theme.accent).preferredColorScheme(.dark)
    }

    @ViewBuilder var page: some View {
        switch m.detailsTab {
        case 0: StoragePage(m: m, mm: mm)
        case 2: CleanupView(mm: mm)
        case 3: ChecksView(mm: mm)
        default: monitor
        }
    }

    var levelSentence: String {
        if !m.enabled { return "ClearX is off. Your Mac is on its own." }
        switch m.level {
        case "red": return "Very short on memory. Background apps are paused so what you're using keeps going."
        case "orange": return "Short on memory. Background apps are paused for a moment."
        case "yellow": return "Busy. ClearX is quietly slowing background apps."
        default: return "Everything is running smoothly."
        }
    }

    var monitor: some View {
        let ready = (m.poolSeg["free"] ?? 0) + (m.poolSeg["cached"] ?? 0)
        return VStack(alignment: .leading, spacing: 16) {
            PageHeader(title: "Right now", sub: levelSentence)
            Card("Memory") {
                Text("working \(m.pressurePct)% hard to find memory").font(.system(size: 11)).foregroundColor(Theme.sub)
            } content: {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(gb(ready)).font(.system(size: 26, weight: .bold, design: .rounded)).foregroundColor(Theme.text)
                    Text("ready to use").foregroundColor(Theme.sub)
                }
                Text("\(gb(m.poolSeg["free"] ?? 0)) unused + \(gb(m.poolSeg["cached"] ?? 0)) cache macOS reuses instantly, out of 16 GB memory + 4 GB graphics memory")
                    .font(.system(size: 11)).foregroundColor(Theme.sub).fixedSize(horizontal: false, vertical: true)
                PoolBar(seg: m.poolSeg, total: m.poolTotal, swap: m.swapUsed)
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 8), count: 4), alignment: .leading, spacing: 6) {
                    ForEach(POOL_SEGS, id: \.0) { k, label, c in legendDot(c, "\(label) \(gb(m.poolSeg[k] ?? 0))") }
                    legendDot(Theme.bad.opacity(0.6), "Swap \(gb(m.swapUsed))")
                }.font(.system(size: 10)).foregroundColor(Theme.sub)
                if !m.poolWhy.isEmpty { Text(m.poolWhy.joined(separator: " · ")).font(.system(size: 11)).foregroundColor(Theme.warn) }
                HStack(spacing: 4) {
                    Text("Overflow to disk (swap) \(mb(m.swapUsed)) · writing \(String(format: "%.1f", m.swapRate)) MB/s").foregroundColor(Theme.sub)
                    if let t = m.tte { Text("· full in about \(Int(t))s").foregroundColor(Theme.bad) }
                }.font(.system(size: 11))
                Sparkline(values: m.swapHist)
            }
            Card("Graphics · Radeon Pro 575X") {
                Text("GPU \(m.gpuUtil)% busy").font(.system(size: 11)).foregroundColor(Theme.sub)
            } content: {
                SimpleBar(title: "Graphics memory", frac: Double(m.vramUsed) / Double(m.vramTotal),
                          word: "\(mb(m.vramUsed)) of 4 GB", color: m.spilling ? Theme.bad : Theme.info)
                Text(String(format: "Spilled into memory %@ · %+.1f MB/s%@", mb(m.poolSeg["spill"] ?? 0), m.spillRate, m.spilling ? " · spilling now" : ""))
                    .font(.system(size: 11)).foregroundColor(m.spilling ? Theme.bad : Theme.sub)
                Text("Disk \(gbd(m.diskFree)) free · saves in \(String(format: "%.1f", m.diskLat)) ms\(m.tight ? " · tight, limits start early" : "") · next Xcode build uses \(m.xcodeJobs) jobs")
                    .font(.system(size: 11)).foregroundColor(m.tight ? Theme.warn : Theme.sub)
            }
            Card("Processor") {
                Text(m.temp.map { String(format: "%.0f °C", $0) } ?? "temp n/a").font(.system(size: 11)).foregroundColor(Theme.sub)
                Text("speed \(m.speedLimit)%").font(.system(size: 11)).foregroundColor(m.hotSustained ? Theme.bad : m.speedLimit < 100 ? Theme.warn : Theme.sub)
            } content: {
                HStack(spacing: 3) {
                    ForEach(Array(m.cores.enumerated()), id: \.offset) { _, v in
                        VStack(spacing: 0) {
                            Spacer(minLength: 0)
                            RoundedRectangle(cornerRadius: 2).fill(v > 85 ? Theme.bad : Theme.accent).frame(height: max(CGFloat(v / 100 * 32), 1))
                        }
                        .frame(maxWidth: .infinity).frame(height: 32)
                        .background(RoundedRectangle(cornerRadius: 2).fill(Theme.line))
                    }
                }
                ForEach(m.topCPU) { p in
                    ProcRow(name: p.name, value: String(format: "%.0f%%", p.cpu)) {
                        if m.throttled.contains(p.id) {
                            Button("Full speed") { m.act(["unthrottle-pid", "\(p.id)"]) }
                        } else {
                            Button("Slow down") { m.act(["throttle-pid", "\(p.id)"]) }
                        }
                    }
                }
            }
            Card("Apps using the most memory") {
                ForEach(m.topMem) { p in
                    ProcRow(name: p.name, value: mb(p.mem)) {
                        if m.paused.contains(where: { $0.pid == "\(p.id)" }) {
                            Button("Resume") { m.act(["resume-pid", "\(p.id)"]) }
                        } else {
                            Button("Pause") { m.act(["pause-pid", "\(p.id)"]) }
                        }
                        Button(m.bg.contains(p.id) ? "Foreground" : "Background") {
                            m.act([m.bg.contains(p.id) ? "fg-pid" : "bg-pid", "\(p.id)"])
                        }
                        Button("Quit") { m.act(["quit-pid", "\(p.id)"]) }
                    }
                }
            }
            if !m.paused.isEmpty {
                Card("Paused (\(m.paused.count))") {
                    Button("Resume all") { m.act(["resume", "all"]) }
                } content: {
                    ForEach(m.paused, id: \.pid) { p in
                        Text("\(p.name) · \(p.why)").font(.system(size: 11)).foregroundColor(Theme.sub).lineLimit(1)
                    }
                }
            }
        }
    }
}


// MARK: - maintenance (CleanMyMac replacement); data comes from state/maint.json written by `memguard maintain`

let MAINT = MG_DIR + "/state/maint.json"

final class MaintModel: ObservableObject {
    @Published var j: [String: Any] = [:]
    @Published var busy: String? = nil
    @Published var selected: Set<String> = []
    @Published var output = ""
    @Published var appName = ""

    init() { load() }
    func load() {
        if let d = try? Data(contentsOf: URL(fileURLWithPath: MAINT)),
           let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] { j = o }
    }
    func run(_ label: String, _ args: [String]) {
        busy = label
        DispatchQueue.global().async {
            let out = mg(["maintain"] + args)
            DispatchQueue.main.async { self.busy = nil; self.output = out; self.load() }
        }
    }
    /// Fixes that delete things: a confirm first, then what was freed.
    func confirmFix(_ what: String, title: String, detail: String) {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert(); a.messageText = title; a.informativeText = detail
        a.addButton(withTitle: "Remove"); a.addButton(withTitle: "Cancel")
        guard a.runModal() == .alertFirstButtonReturn else { return }
        busy = "Removing…"
        DispatchQueue.global().async {
            let out = mg(["maintain", "fix", what])
            DispatchQueue.main.async {
                self.busy = nil; self.load()
                let r = NSAlert(); r.messageText = "Done"
                r.informativeText = out.split(separator: "\n").last.map(String.init) ?? out
                r.runModal()
            }
        }
    }
    func confirmIOSDev() {
        confirmFix("iosdev", title: "Remove iOS simulators and iPhone debug symbols?",
                   detail: "Shuts down and deletes every iOS simulator, deletes the simulator runtimes, and moves the iPhone debug symbols to the Trash. Xcode downloads them again if MyApp's iOS work needs them (slowly).")
    }
    var junk: [[String: Any]] { j["junk"] as? [[String: Any]] ?? [] }
    var startup: [[String: Any]] { j["startup"] as? [[String: Any]] ?? [] }
    var security: [String: Any] { j["security"] as? [String: Any] ?? [:] }
    var updates: [String: Any] { j["updates"] as? [String: Any] ?? [:] }
    var health: [String: Any] { j["health"] as? [String: Any] ?? [:] }
    var healthHist: [Double] { (j["health_hist"] as? [[Double]] ?? []).map { $0.count > 1 ? $0[1] : 0 } }
    var large: [[String: Any]] { j["large"] as? [[String: Any]] ?? [] }
    var storage: [String: Any] { j["storage"] as? [String: Any] ?? [:] }
    var optimize: [[String: Any]] { (j["optimize"] as? [String: Any])?["items"] as? [[String: Any]] ?? [] }
}

func num(_ a: Any?) -> UInt64 { (a as? NSNumber)?.uint64Value ?? 0 }

struct CleanupView: View {
    @ObservedObject var mm: MaintModel
    var body: some View {
        let score = (mm.health["score"] as? Int) ?? 0
        let why = (mm.health["why"] as? [String]) ?? []
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                PageHeader(title: "Clean up", sub: "Junk, startup items, big files and apps you don't need.")
                Spacer()
                BusyBadge(mm: mm)
            }
            Card("Health") {
                Button("Full check") { mm.run("Checking…", ["all"]) }.disabled(mm.busy != nil)
            } content: {
                HStack(spacing: 16) {
                    ScoreRing(score: score, color: score >= 85 ? Theme.accent : score >= 65 ? Theme.warn : Theme.bad)
                    VStack(alignment: .leading, spacing: 4) {
                        if why.isEmpty { Text("Nothing is taking points off.").foregroundColor(Theme.sub) }
                        ForEach(why, id: \.self) { Text("• " + $0).foregroundColor(Theme.sub).fixedSize(horizontal: false, vertical: true) }
                    }
                    Spacer(minLength: 0)
                }
                if mm.healthHist.count > 1 { Sparkline(values: mm.healthHist) }
            }
            Card("Junk") {
                Button("Scan") { mm.run("Scanning…", ["scan"]) }
                Button("Move selected to Trash") {
                    mm.run("Cleaning…", ["clean", mm.selected.joined(separator: ",")]); mm.selected = []
                }.disabled(mm.selected.isEmpty || mm.busy != nil)
            } content: {
                if mm.junk.isEmpty { Text("Click Scan to look for junk.").foregroundColor(Theme.sub) }
                ForEach(Array(mm.junk.enumerated()), id: \.offset) { _, c in
                    let id = c["id"] as? String ?? "", action = c["action"] as? String ?? ""
                    HStack(spacing: 8) {
                        if action == "trash" {
                            Toggle("", isOn: Binding(get: { mm.selected.contains(id) },
                                                     set: { if $0 { mm.selected.insert(id) } else { mm.selected.remove(id) } }))
                                .labelsHidden().toggleStyle(.checkbox)
                        } else { Color.clear.frame(width: 14, height: 14) }
                        Text(c["title"] as? String ?? "").lineLimit(1).foregroundColor(Theme.text)
                        Spacer()
                        Text(mb(num(c["size"]))).monospacedDigit().foregroundColor(Theme.sub)
                        if action == "fix:iosdev" { Button("Remove…") { mm.confirmIOSDev() }.disabled(mm.busy != nil) }
                    }.font(.system(size: 12))
                }
                Text("Moves to the Trash, so you can get it back. Empty the Trash yourself to free the space.")
                    .font(.system(size: 11)).foregroundColor(Theme.sub)
            }
            Card("Startup items") {
                Button("Refresh") { mm.run("Listing…", ["startup"]) }
            } content: {
                ForEach(Array(mm.startup.enumerated()), id: \.offset) { _, s in
                    let label = s["label"] as? String ?? "", scope = s["scope"] as? String ?? ""
                    let on = (s["loaded"] as? Bool) ?? false
                    HStack(spacing: 8) {
                        Circle().fill(on ? Theme.accent : Theme.line).frame(width: 7, height: 7)
                        Text(label).lineLimit(1).truncationMode(.middle).foregroundColor(Theme.text)
                        Spacer()
                        Text(mb(num(s["rss"]))).monospacedDigit().foregroundColor(Theme.sub)
                        if scope == "user" && !label.hasPrefix("com.user.memguard") && !label.hasPrefix("com.user.clearx") {
                            Button(on ? "Turn off" : "Turn on") { mm.run("…", [on ? "disable" : "enable", label]) }
                        } else { Text(scope == "login item" ? "Login Items" : "system").foregroundColor(Theme.sub) }
                    }.font(.system(size: 12))
                }
            }
            Card("Large & old files") {
                Button("Find") { mm.run("Searching…", ["large"]) }
            } content: {
                if mm.large.isEmpty { Text("Click Find to list them.").foregroundColor(Theme.sub) }
                ForEach(Array(mm.large.prefix(6).enumerated()), id: \.offset) { _, f in
                    let p = f["path"] as? String ?? ""
                    ProcRow(name: (p as NSString).lastPathComponent, value: mb(num(f["size"]))) {
                        Button("Show") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)]) }
                    }
                }
            }
            Card("Uninstall an app") {
                HStack(spacing: 8) {
                    TextField("App name, e.g. Zoom", text: $mm.appName).textFieldStyle(.roundedBorder)
                    Button("Preview") { mm.run("Finding files…", ["uninstall", mm.appName]) }.disabled(mm.appName.isEmpty)
                    Button("Uninstall") { mm.run("Uninstalling…", ["uninstall", mm.appName, "--apply"]) }.disabled(mm.appName.isEmpty)
                }
                Text("Preview first: it lists every file that would go.").font(.system(size: 11)).foregroundColor(Theme.sub)
            }
            OutputCard(mm: mm)
        }
    }
}

struct ChecksView: View {
    @ObservedObject var mm: MaintModel
    var body: some View {
        let sec = mm.security
        let mac = (mm.updates["macos"] as? [[String: Any]]) ?? [], brew = (mm.updates["brew"] as? [[String: Any]]) ?? []
        let flagged = ((sec["persistence"] as? [[String: Any]]) ?? []).filter { ($0["flag"] as? Bool) ?? false }
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                PageHeader(title: "Checks", sub: "Updates, security and speed settings.")
                Spacer()
                BusyBadge(mm: mm)
            }
            Card("Updates") {
                Button("Check") { mm.run("Checking updates…", ["updates"]) }
                Button("Software Update") {
                    NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Software-Update-Settings.extension")!)
                }
            } content: {
                if mac.isEmpty && brew.isEmpty { CheckLine(ok: true, text: "Nothing waiting (as of the last check).") }
                ForEach(Array(mac.enumerated()), id: \.offset) { _, u in
                    CheckLine(ok: false, text: "macOS: \(u["title"] as? String ?? "") \(u["version"] as? String ?? "")\((u["restart"] as? Bool ?? false) ? " (needs a restart)" : "")")
                }
                ForEach(Array(brew.enumerated()), id: \.offset) { _, b in
                    CheckLine(ok: false, text: "\(b["name"] as? String ?? "") → \(b["current"] as? String ?? "")")
                }
            }
            Card("Security") {
                Button("Audit") { mm.run("Auditing…", ["security"]) }
            } content: {
                Text("XProtect \(sec["xprotect"] as? String ?? "?") · Remediator \(sec["xprotect_remediator"] as? String ?? "?") · SIP \(sec["sip"] as? String ?? "?")")
                    .foregroundColor(Theme.text)
                Text("\(sec["gatekeeper"] as? String ?? "") · \(sec["filevault"] as? String ?? "") · \(sec["firewall"] as? String ?? "")")
                    .foregroundColor(Theme.sub)
                if flagged.isEmpty && !sec.isEmpty { CheckLine(ok: true, text: "Nothing suspicious starts with your Mac.") }
                ForEach(Array(flagged.enumerated()), id: \.offset) { _, a in
                    CheckLine(ok: false, text: "\(a["label"] as? String ?? ""): \(a["detail"] as? String ?? "")")
                }
            }
            OptimizeView(mm: mm)
            OutputCard(mm: mm)
        }
    }
}

// Storage, in plain words: how full, how healthy, how fast, what's using it, what to do.
struct StoragePage: View {
    @ObservedObject var m: Model
    @ObservedObject var mm: MaintModel
    var body: some View {
        let s = mm.storage, d = s["drive"] as? [String: Any] ?? [:], sp = s["speed"] as? [String: Any] ?? [:]
        let total = Double(max(m.diskTotal, 1)), used = Double(m.diskTotal) - Double(m.diskFree) - Double(m.diskPurgeable)  // (Double: no UInt64 underflow trap)
        let ssd = mm.j["ssd"] as? [String: Any] ?? [:], days = ssd["days"] as? [String: Any] ?? [:]
        let today = { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; return f.string(from: Date()) }()
        let dups = (s["duplicates"] as? [[String: Any]]) ?? []
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                PageHeader(title: "Storage", sub: "\(gbd(m.diskFree + m.diskPurgeable)) available of \(gbd(m.diskTotal))")
                Spacer()
                BusyBadge(mm: mm)
            }
            Card("Your drive") {
                GeometryReader { g in
                    HStack(spacing: 0) {
                        Rectangle().fill(Theme.info).frame(width: g.size.width * CGFloat(used / total))
                        Rectangle().fill(Theme.accent.opacity(0.5)).frame(width: g.size.width * CGFloat(Double(m.diskPurgeable) / total))
                        Rectangle().fill(Theme.line)
                    }
                }.frame(height: 14).clipShape(Capsule())
                HStack(spacing: 16) {
                    legendDot(Theme.info, "Used \(gbd(UInt64(max(used, 0))))")
                    legendDot(Theme.accent.opacity(0.5), "macOS can clear \(gbd(m.diskPurgeable))")
                    legendDot(Theme.line, "Free \(gbd(m.diskFree))")
                }.font(.system(size: 11)).foregroundColor(Theme.sub)
                Text("“macOS can clear” is space macOS frees by itself when you need it (iCloud/Drive copies, caches). Finder counts it as available, so Finder shows \(gbd(m.diskFree + m.diskPurgeable)) available.")
                    .font(.system(size: 11)).foregroundColor(Theme.sub).fixedSize(horizontal: false, vertical: true)
            }
            Card("Health and speed") {
                CheckLine(ok: true, text: "Type: \(d["kind"] as? String ?? "?") (\(d["model"] as? String ?? "?"), \(d["protocol"] as? String ?? "?")), a fast internal drive, not the old Fusion/hard-drive kind.")
                CheckLine(ok: (d["smart"] as? String) == "Verified", text: "Health check (SMART): \(d["smart"] as? String ?? "?")\((d["smart"] as? String) == "Verified" ? ", no signs of failure." : "")")
                if let wear = d["wear_pct"] as? Int {
                    CheckLine(ok: wear < 50, text: "Wear: \(wear)% of the drive's life used (\(gb(num(d["lifetime_written"]))) written so far, about \(gb(num(d["endurance"]))) in total).")
                }
                CheckLine(ok: (d["trim"] as? String) == "Yes", text: "TRIM: \(d["trim"] as? String ?? "?")\((d["trim"] as? String) == "Yes" ? ", keeps the drive fast as it fills." : "")")
                if !sp.isEmpty { CheckLine(ok: true, text: "Measured speed: writes \(num(sp["write_mb_s"])) MB/s. Fast enough; an external drive wouldn't help.") }
                CheckLine(ok: num(days[today]) <= 100 << 30,
                          text: "Drive writes today: \(gb(num(days[today]))) (swap \(gb(num((ssd["swap_days"] as? [String: Any])?[today])))). Writes are what wear an SSD; the wear line above is the drive's own measurement. ClearX keeps them down: 2 compile jobs while you work, apps frozen only while compression has room, Brave's disk cache capped at 300 MB.")
                Text("Reads today don't wear the drive: \(gb(num(ssd["boot_r"]))) since the last restart.")
                    .font(.system(size: 11)).foregroundColor(Theme.sub)
            }
            Card("") { StoragePlanView(mm: mm) }
            Card("What's using space") {
                Button(mm.busy ?? "Measure again") { mm.run("Measuring…", ["storage"]) }.disabled(mm.busy != nil)
            } content: {
                ForEach(Array(((s["map"] as? [[String: Any]]) ?? []).prefix(12).enumerated()), id: \.offset) { _, f in
                    let p = f["path"] as? String ?? ""
                    ProcRow(name: p.replacingOccurrences(of: NSHomeDirectory() + "/Library/CloudStorage", with: "Google Drive (cloud copies)")
                                   .replacingOccurrences(of: NSHomeDirectory(), with: "~"), value: mb(num(f["size"]))) {
                        Button("Show") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)]) }
                    }
                }
            }
            Card("Duplicate files") {
                Text("Your documents and media only. The oldest copy is always kept.").font(.system(size: 11)).foregroundColor(Theme.sub)
                if s["duplicates"] == nil { Text("Click “Measure again” to look for them.").foregroundColor(Theme.sub) }
                else if dups.isEmpty { CheckLine(ok: true, text: "None over 50 MB.") }
                ForEach(Array(dups.prefix(10).enumerated()), id: \.offset) { _, g in
                    let paths = g["paths"] as? [String] ?? []
                    ForEach(Array(paths.dropFirst().enumerated()), id: \.offset) { _, p in  // the oldest copy is kept
                        ProcRow(name: (p as NSString).lastPathComponent, value: mb(num(g["size"]))) {
                            Button("Show") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)]) }
                            Button("Move to Trash") { mm.run("Moving to Trash…", ["trash", p]) }
                        }
                    }
                }
            }
        }
    }
}

struct OptimizeView: View {
    @ObservedObject var mm: MaintModel
    var body: some View {
        Card("Speed settings") {
            Button("Measure") { mm.run("Measuring…", ["optimize"]) }
            Button("Apply safe ones") { mm.run("Applying…", ["optimize", "--apply"]) }
        } content: {
            if mm.optimize.isEmpty { Text("Click Measure to see what can be tuned.").foregroundColor(Theme.sub) }
            ForEach(Array(mm.optimize.enumerated()), id: \.offset) { _, i in
                let st = i["status"] as? String ?? ""
                let good = ["ok", "applied", "n/a"].contains(st)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .top, spacing: 8) {
                        Text(st).font(.system(size: 10, weight: .semibold))
                            .foregroundColor(good ? Theme.accent : st == "hardware" ? Theme.info : Theme.warn)
                            .padding(.horizontal, 6).padding(.vertical, 2).background(Capsule().fill(Theme.line))
                            .frame(width: 72, alignment: .leading)
                        Text("\(i["title"] as? String ?? ""): \(i["measured"] as? String ?? "")").foregroundColor(Theme.text)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !good, let a = i["action"] as? String, !a.isEmpty {
                        Text("→ " + a).foregroundColor(Theme.sub).textSelection(.enabled).padding(.leading, 80)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }.font(.system(size: 12))
            }
        }
    }
}

func dotImage(_ c: NSColor) -> NSImage {
    let img = NSImage(size: NSSize(width: 10, height: 10), flipped: false) { r in
        c.setFill(); NSBezierPath(ovalIn: r.insetBy(dx: 1, dy: 1)).fill(); return true
    }
    img.isTemplate = false
    return img
}

@main
struct MemGuardApp: App {
    @StateObject var m = Model()
    init() { _ = Freezer.shared }  // start waking frozen apps on activation from launch
    var body: some Scene {
        MenuBarExtra {
            Panel(m: m)
        } label: {
            HStack(spacing: 3) {
                Image(nsImage: turbineImage())
                if m.level != "green" { Image(nsImage: dotImage(m.color)) }
                Text(String(format: "%.1fG", Double(m.availB) / 1073741824))
            }
        }
        .menuBarExtraStyle(.window)
        Window("ClearX Details", id: "details") { DetailsView(m: m) }
    }
}
