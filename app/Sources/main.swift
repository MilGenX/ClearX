// MemGuard menu-bar app. Reads live stats natively (Mach, libproc, IOKit, SMC) every 2 s,
// reads memguard's state.json, and calls the memguard CLI for every action.
import SwiftUI
import AppKit
import IOKit
import Darwin

let MG_DIR = NSString(string: "~/Developer/MacMemoryManager").expandingTildeInPath
let MG_CLI = MG_DIR + "/memguard"
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
    var speedLimit = 100, temp: Double? = nil, enabled = true, tte: Double? = nil
    // the 20 GB pool from state.json (written by the daemon every tick)
    var poolSeg: [String: UInt64] = [:], poolTotal: UInt64 = 20 << 30, poolWhy: [String] = []
    var spillRate = 0.0, spilling = false, diskFree: UInt64 = 0, diskLat = 0.0, tight = false, xcodeJobs = 0
    let vramTotal: UInt64 = 4096 << 20
    let pageSize = UInt64(vm_kernel_page_size)
    private let coreLoad = CoreLoad(), procs = ProcSampler(), smc = SMC()
    private var lastSwapouts: UInt64 = 0, lastT = Date()
    private var timer: Timer?

    init() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func refresh() {
        objectWillChange.send()
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
        // what Finder calls "available" = free + purgeable (space macOS clears by itself when needed)
        if let v = try? URL(fileURLWithPath: "/").resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]),
           let imp = v.volumeAvailableCapacityForImportantUsage, UInt64(imp) > diskFree {
            diskPurgeable = UInt64(imp) - diskFree
        }
        loadNeeds()
        if let d = try? Data(contentsOf: URL(fileURLWithPath: STATE)),
           let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
            level = j["level"] as? String ?? level
            mode = j["mode"] as? String ?? mode
            modeSetting = (j["mode_override"] as? String) ?? "auto"
            speedLimit = j["cpu_speed_limit"] as? Int ?? 100
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
            }.stroke(Color.orange, lineWidth: 1)
        }
        .frame(height: 28)
        .background(Color.secondary.opacity(0.08))
    }
}

struct Bar: View {
    let frac: Double
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Rectangle().fill(Color.secondary.opacity(0.15))
                Rectangle().fill(frac > 0.9 ? Color.red : frac > 0.7 ? Color.orange : Color.accentColor)
                    .frame(width: g.size.width * CGFloat(min(max(frac, 0), 1)))
            }
        }.frame(height: 6).cornerRadius(2)
    }
}

// One bar for RAM + VRAM: app | wired | compressed | GPU spill | VRAM | cached | free, swap drawn past the end
let POOL_SEGS: [(String, String, Color)] = [("app", "App", .blue), ("wired", "System", .orange), ("compressed", "Compressed", .yellow),
                                            ("spill", "Graphics overflow", .red), ("vram", "VRAM", .purple), ("cached", "Reusable cache", Color.green.opacity(0.45)),
                                            ("free", "Unused", Color.green.opacity(0.8))]

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
                Rectangle().fill(Color.red.opacity(0.55)).frame(width: w * CGFloat(over))   // swap overflow
                    .overlay(Rectangle().stroke(Color.red, style: StrokeStyle(lineWidth: 1, dash: [2])))
                Spacer(minLength: 0)
            }
        }.frame(height: 14).cornerRadius(3)
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
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 12, weight: .semibold)).foregroundColor(Theme.text)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(Capsule().fill(Theme.line.opacity(configuration.isPressed ? 1 : 0.7)))
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
        m.speedLimit < 90 ? ("Too hot", Theme.bad) : m.speedLimit < 100 ? ("Warm", Theme.warn) :
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
        if !m.enabled { return "MemGuard is off" }
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
        case "yellow": return "MemGuard is quietly slowing background apps."
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
                      detail: String(format: "%.0f%% busy%@", cpuAvg, m.speedLimit < 100 ? " · slowed by heat to \(m.speedLimit)%" : ""))
            Button(action: { open(0) }) {
                SimpleBar(title: "Storage", frac: 1 - Double(m.diskFree + m.diskPurgeable) / Double(max(m.diskTotal, 1)),
                          word: diskWord.0, color: diskWord.1,
                          detail: "\(gb(m.diskFree + m.diskPurgeable)) free of \(gb(m.diskTotal)) · SSD healthy\(m.ssdToday > 100 << 30 ? ", busy today" : "")")
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
struct DetailsView: View {
    @ObservedObject var m: Model
    @StateObject var mm = MaintModel()
    var body: some View {
        TabView(selection: $m.detailsTab) {
            ScrollView { StoragePage(m: m, mm: mm).padding(12) }.tabItem { Text("Storage") }.tag(0)
            ScrollView { monitor.padding(12) }.tabItem { Text("Live") }.tag(1)
            ScrollView { MaintView(mm: mm).padding(12) }.tabItem { Text("Clean up & checks") }.tag(2)
        }.frame(minWidth: 480, minHeight: 640)
        .background(Theme.bg).preferredColorScheme(.dark).tint(Theme.accent)
    }

    var monitor: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Circle().fill(Color(m.color)).frame(width: 10, height: 10)
                Text("Memory state: \(m.level)").bold()
            }
            Group {
                HStack {
                    Text("Memory pool · 16 GB RAM + 4 GB VRAM").font(.caption).foregroundColor(.secondary)
                    Spacer()
                    Text("how hard it's working to find memory: \(m.pressurePct)%").font(.caption)
                }
                Text("Ready to use: \(gb((m.poolSeg["free"] ?? 0) + (m.poolSeg["cached"] ?? 0))) (\(gb(m.poolSeg["free"] ?? 0)) unused + \(gb(m.poolSeg["cached"] ?? 0)) cache macOS reuses instantly)")
                    .font(.callout).bold()
                PoolBar(seg: m.poolSeg, total: m.poolTotal, swap: m.swapUsed)
                LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 2), count: 4), alignment: .leading, spacing: 1) {
                    ForEach(POOL_SEGS, id: \.0) { k, label, c in
                        HStack(spacing: 3) { Rectangle().fill(c).frame(width: 7, height: 7); Text("\(label) \(gb(m.poolSeg[k] ?? 0))") }
                    }
                    HStack(spacing: 3) { Rectangle().fill(Color.red.opacity(0.55)).frame(width: 7, height: 7); Text("Swap \(gb(m.swapUsed))") }
                }.font(.system(size: 9))
                if !m.poolWhy.isEmpty { Text(m.poolWhy.joined(separator: " · ")).font(.caption2).foregroundColor(.orange) }
                HStack {
                    Text("Swap \(mb(m.swapUsed)) · out \(String(format: "%.1f", m.swapRate)) MB/s")
                    if let t = m.tte { Text("· full in ~\(Int(t))s").foregroundColor(.red) }
                }.font(.caption)
                Sparkline(values: m.swapHist)
            }
            Divider()
            Group {
                Text("GPU · Radeon Pro 575X").font(.caption).foregroundColor(.secondary)
                HStack { Text("VRAM \(mb(m.vramUsed)) / 4.0 GB"); Spacer(); Text("GPU \(m.gpuUtil)%") }
                Text(String(format: "Spill into RAM %@ · %+.1f MB/s%@", mb(m.poolSeg["spill"] ?? 0), m.spillRate,
                            m.spilling ? " · SPILLING" : "")).font(.caption).foregroundColor(m.spilling ? .red : .primary)
                Text("Disk \(gb(m.diskFree)) free · fsync \(String(format: "%.1f", m.diskLat)) ms\(m.tight ? " · tight: budgets apply early" : "") · next Xcode build \(m.xcodeJobs) jobs")
                    .font(.caption2).foregroundColor(m.tight ? .orange : .secondary)
            }
            Divider()
            Group {
                HStack {
                    Text("CPU").font(.caption).foregroundColor(.secondary)
                    Spacer()
                    Text(m.temp.map { String(format: "%.0f °C", $0) } ?? "temp n/a").font(.caption)
                    Text("speed limit \(m.speedLimit)%").font(.caption)
                        .foregroundColor(m.speedLimit < 100 ? .red : .secondary)
                }
                HStack(spacing: 2) {
                    ForEach(Array(m.cores.enumerated()), id: \.offset) { _, v in
                        VStack { Spacer(minLength: 0)
                            Rectangle().fill(v > 85 ? Color.red : Color.accentColor).frame(height: CGFloat(v / 100 * 24)) }
                            .frame(height: 24).background(Color.secondary.opacity(0.12))
                    }
                }
                ForEach(m.topCPU) { p in
                    HStack {
                        Text(p.name).lineLimit(1); Spacer()
                        Text(String(format: "%.0f%%", p.cpu)).monospacedDigit()
                        if m.throttled.contains(p.id) {
                            Button("Unthrottle") { m.act(["unthrottle-pid", "\(p.id)"]) }
                        } else {
                            Button("Throttle") { m.act(["throttle-pid", "\(p.id)"]) }
                        }
                    }.font(.caption).buttonStyle(.borderless)
                }
            }
            Divider()
            Group {
                Text("Top memory").font(.caption).foregroundColor(.secondary)
                ForEach(m.topMem) { p in
                    HStack(spacing: 6) {
                        Text(p.name).lineLimit(1); Spacer()
                        Text(mb(p.mem)).monospacedDigit()
                        if m.paused.contains(where: { $0.pid == "\(p.id)" }) {
                            Button("Resume") { m.act(["resume-pid", "\(p.id)"]) }
                        } else {
                            Button("Pause") { m.act(["pause-pid", "\(p.id)"]) }
                        }
                        Button(m.bg.contains(p.id) ? "Fg" : "Bg") {
                            m.act([m.bg.contains(p.id) ? "fg-pid" : "bg-pid", "\(p.id)"])
                        }
                        Button("Quit") { m.act(["quit-pid", "\(p.id)"]) }
                    }.font(.caption).buttonStyle(.borderless)
                }
            }
            if !m.paused.isEmpty {
                Divider()
                HStack {
                    Text("Paused (\(m.paused.count))").font(.caption).foregroundColor(.secondary)
                    Spacer()
                    Button("Resume all") { m.act(["resume", "all"]) }.font(.caption)
                }
                ForEach(m.paused, id: \.pid) { p in
                    Text("\(p.name) · \(p.pid) · \(p.why)").font(.caption2).lineLimit(1)
                }
            }
            Divider()
            HStack {
                Spacer()
                Button("Open settings file") { NSWorkspace.shared.open(URL(fileURLWithPath: MG_DIR + "/memguard.conf")) }
            }.font(.caption)
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

struct MaintView: View {
    @ObservedObject var mm: MaintModel
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                let score = (mm.health["score"] as? Int) ?? 0
                Text("Health \(score)/100").font(.title3).bold()
                    .foregroundColor(score >= 85 ? .green : score >= 65 ? .orange : .red)
                Spacer()
                if let b = mm.busy { ProgressView().controlSize(.small); Text(b).font(.caption) }
                Button("Full check") { mm.run("Checking…", ["all"]) }.disabled(mm.busy != nil)
            }
            if mm.healthHist.count > 1 { Sparkline(values: mm.healthHist) }
            ForEach((mm.health["why"] as? [String]) ?? [], id: \.self) { Text("• " + $0).font(.caption) }
            Divider()
            HStack {
                Text("Cleanup").font(.caption).foregroundColor(.secondary)
                Spacer()
                Button("Scan") { mm.run("Scanning…", ["scan"]) }.font(.caption)
                Button("Move selected to Trash") {
                    mm.run("Cleaning…", ["clean", mm.selected.joined(separator: ",")]); mm.selected = []
                }.font(.caption).disabled(mm.selected.isEmpty || mm.busy != nil)
            }
            ForEach(Array(mm.junk.enumerated()), id: \.offset) { _, c in
                let id = c["id"] as? String ?? "", action = c["action"] as? String ?? ""
                HStack {
                    if action == "trash" {
                        Toggle("", isOn: Binding(get: { mm.selected.contains(id) },
                                                 set: { if $0 { mm.selected.insert(id) } else { mm.selected.remove(id) } }))
                            .labelsHidden().controlSize(.mini)
                    } else { Text(" ").frame(width: 14) }
                    Text(c["title"] as? String ?? "").lineLimit(1)
                    Spacer()
                    Text(mb(num(c["size"]))).monospacedDigit()
                }.font(.caption)
            }
            Text("Moves to the Trash (recoverable). Empty the Trash yourself to free the space.").font(.caption2).foregroundColor(.secondary)
            Divider()
            HStack {
                Text("Startup items").font(.caption).foregroundColor(.secondary)
                Spacer()
                Button("Refresh") { mm.run("Listing…", ["startup"]) }.font(.caption)
            }
            ForEach(Array(mm.startup.enumerated()), id: \.offset) { _, s in
                let label = s["label"] as? String ?? "", scope = s["scope"] as? String ?? ""
                let on = (s["loaded"] as? Bool) ?? false
                HStack {
                    Circle().fill(on ? Color.green : Color.secondary.opacity(0.4)).frame(width: 6, height: 6)
                    Text(label).lineLimit(1)
                    Spacer()
                    Text(mb(num(s["rss"]))).monospacedDigit()
                    if scope == "user" && !label.hasPrefix("com.user.memguard") {
                        Button(on ? "Disable" : "Enable") { mm.run("…", [on ? "disable" : "enable", label]) }
                    } else { Text(scope == "login item" ? "Login Items" : "admin").foregroundColor(.secondary) }
                }.font(.caption2).buttonStyle(.borderless)
            }
            Divider()
            Group {
                HStack {
                    Text("Updates").font(.caption).foregroundColor(.secondary)
                    Spacer()
                    Button("Check") { mm.run("Checking updates…", ["updates"]) }.font(.caption)
                    Button("Software Update") {
                        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Software-Update-Settings.extension")!)
                    }.font(.caption)
                }
                ForEach(Array(((mm.updates["macos"] as? [[String: Any]]) ?? []).enumerated()), id: \.offset) { _, u in
                    Text("macOS: \(u["title"] as? String ?? "") \(u["version"] as? String ?? "")\((u["restart"] as? Bool ?? false) ? " (restart)" : "")").font(.caption2)
                }
                ForEach(Array(((mm.updates["brew"] as? [[String: Any]]) ?? []).enumerated()), id: \.offset) { _, b in
                    Text("brew: \(b["name"] as? String ?? "") → \(b["current"] as? String ?? "")").font(.caption2)
                }
            }
            Divider()
            Group {
                HStack {
                    Text("Security").font(.caption).foregroundColor(.secondary)
                    Spacer()
                    Button("Audit") { mm.run("Auditing…", ["security"]) }.font(.caption)
                }
                let sec = mm.security
                Text("XProtect \(sec["xprotect"] as? String ?? "?") · Remediator \(sec["xprotect_remediator"] as? String ?? "?") · SIP \(sec["sip"] as? String ?? "?")").font(.caption2)
                Text("\(sec["gatekeeper"] as? String ?? "") · \(sec["filevault"] as? String ?? "") · \(sec["firewall"] as? String ?? "")").font(.caption2)
                ForEach(Array(((sec["persistence"] as? [[String: Any]]) ?? []).filter { ($0["flag"] as? Bool) ?? false }.enumerated()), id: \.offset) { _, a in
                    Text("⚠︎ \(a["label"] as? String ?? ""): \(a["detail"] as? String ?? "")").font(.caption2).foregroundColor(.orange)
                }
            }
            Divider()
            StorageView(mm: mm)
            Divider()
            OptimizeView(mm: mm)
            Divider()
            HStack {
                Text("Large & old files").font(.caption).foregroundColor(.secondary)
                Spacer()
                Button("Find") { mm.run("Searching…", ["large"]) }.font(.caption)
            }
            ForEach(Array(mm.large.prefix(6).enumerated()), id: \.offset) { _, f in
                let p = f["path"] as? String ?? ""
                HStack {
                    Text((p as NSString).lastPathComponent).lineLimit(1)
                    Spacer()
                    Text(mb(num(f["size"])))
                    Button("Reveal") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)]) }
                }.font(.caption2).buttonStyle(.borderless)
            }
            Divider()
            HStack {
                TextField("App to uninstall (e.g. Zoom)", text: $mm.appName).textFieldStyle(.roundedBorder).font(.caption)
                Button("Preview") { mm.run("Finding files…", ["uninstall", mm.appName]) }.font(.caption)
                Button("Uninstall") { mm.run("Uninstalling…", ["uninstall", mm.appName, "--apply"]) }.font(.caption)
                    .disabled(mm.appName.isEmpty)
            }
            if !mm.output.isEmpty {
                ScrollView { Text(mm.output).font(.system(size: 9, design: .monospaced)).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading) }.frame(height: 90)
            }
        }
    }
}

// Storage, in plain words: how full, how healthy, how fast, what's using it, what to do.
struct StoragePage: View {
    @ObservedObject var m: Model
    @ObservedObject var mm: MaintModel
    var body: some View {
        let s = mm.storage, d = s["drive"] as? [String: Any] ?? [:], sp = s["speed"] as? [String: Any] ?? [:]
        let total = Double(max(m.diskTotal, 1)), used = Double(m.diskTotal - m.diskFree - m.diskPurgeable)
        let ssd = mm.j["ssd"] as? [String: Any] ?? [:], days = ssd["days"] as? [String: Any] ?? [:]
        let today = { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; return f.string(from: Date()) }()
        VStack(alignment: .leading, spacing: 10) {
            Text("Your drive").font(.title3).bold()
            GeometryReader { g in
                HStack(spacing: 0) {
                    Rectangle().fill(Color.blue).frame(width: g.size.width * CGFloat(used / total))
                    Rectangle().fill(Color.teal.opacity(0.6)).frame(width: g.size.width * CGFloat(Double(m.diskPurgeable) / total))
                    Rectangle().fill(Color.secondary.opacity(0.18))
                }
            }.frame(height: 16).cornerRadius(4)
            HStack(spacing: 14) {
                Label("Used \(gb(UInt64(used)))", systemImage: "square.fill").foregroundColor(.blue)
                Label("macOS can clear \(gb(m.diskPurgeable))", systemImage: "square.fill").foregroundColor(.teal)
                Label("Free \(gb(m.diskFree))", systemImage: "square.fill").foregroundColor(.secondary)
            }.font(.caption)
            Text("“macOS can clear” is space macOS frees by itself when you need it (iCloud/Drive copies, caches). Finder counts it as available, so Finder shows \(gb(m.diskFree + m.diskPurgeable)) available.")
                .font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
            Divider()
            Text("Health and speed").font(.headline)
            Text("• Type: \(d["kind"] as? String ?? "?") (\(d["model"] as? String ?? "?"), \(d["protocol"] as? String ?? "?")): a fast internal drive, not the old Fusion/hard-drive kind.")
            Text("• Health check (SMART): \(d["smart"] as? String ?? "?")\((d["smart"] as? String) == "Verified" ? ": no signs of failure." : "")")
            Text("• TRIM: \(d["trim"] as? String ?? "?")\((d["trim"] as? String) == "Yes" ? ": keeps the drive fast as it fills." : "")")
            if !sp.isEmpty { Text("• Measured speed: writes \(num(sp["write_mb_s"])) MB/s. Fast enough; an external drive wouldn't help.") }
            Text("• Written today: \(gb(num(days[today]))) (since the last restart \(gb(num(ssd["boot_w"]))). Most of this is memory overflow (swap); MemGuard keeps it down, and more RAM would end it.")
                .fixedSize(horizontal: false, vertical: true)
            Divider()
            StoragePlanView(mm: mm)
            Divider()
            HStack {
                Text("What's using space").font(.headline)
                Spacer()
                Button(mm.busy ?? "Measure again") { mm.run("Measuring…", ["storage"]) }.disabled(mm.busy != nil)
            }
            ForEach(Array(((s["map"] as? [[String: Any]]) ?? []).prefix(12).enumerated()), id: \.offset) { _, f in
                let p = f["path"] as? String ?? ""
                HStack {
                    Text(p.replacingOccurrences(of: NSHomeDirectory() + "/Library/CloudStorage", with: "Google Drive (cloud copies)")
                          .replacingOccurrences(of: NSHomeDirectory(), with: "~")).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Text(mb(num(f["size"]))).monospacedDigit()
                    Button("Show") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)]) }.buttonStyle(.borderless)
                }.font(.callout)
            }
            let dups = (s["duplicates"] as? [[String: Any]]) ?? []
            Divider()
            Text("Duplicate files (your documents and media only)").font(.headline)
            if s["duplicates"] == nil { Text("Click “Measure again” to look for them.").foregroundColor(.secondary) }
            else if dups.isEmpty { Text("None over 50 MB.").foregroundColor(.secondary) }
            ForEach(Array(dups.prefix(10).enumerated()), id: \.offset) { _, g in
                let paths = g["paths"] as? [String] ?? []
                ForEach(Array(paths.dropFirst().enumerated()), id: \.offset) { _, p in  // the oldest copy is kept
                    HStack {
                        Text("\((p as NSString).lastPathComponent) · \(mb(num(g["size"])))").lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Button("Show") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)]) }
                        Button("Move to Trash") { mm.run("Moving to Trash…", ["trash", p]) }
                    }.font(.callout).buttonStyle(.borderless)
                }
            }
        }.font(.callout)
    }
}

struct StorageView: View {
    @ObservedObject var mm: MaintModel
    var body: some View {
        let s = mm.storage, d = s["drive"] as? [String: Any] ?? [:], sp = s["speed"] as? [String: Any] ?? [:]
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("Storage").font(.caption).foregroundColor(.secondary)
                Spacer()
                Button("Scan storage") { mm.run("Measuring disk…", ["storage"]) }.font(.caption)
            }
            Text("\(d["kind"] as? String ?? "?") \(d["model"] as? String ?? "") · SMART \(d["smart"] as? String ?? "?") · TRIM \(d["trim"] as? String ?? "?")").font(.caption2)
            Text("\(gb(num(d["free"]))) free + \(gb(num(d["purgeable"]))) purgeable of \(gb(num(d["size"])))\(sp.isEmpty ? "" : " · write \(num(sp["write_mb_s"])) / read \(num(sp["read_mb_s"])) MB/s")").font(.caption2)
            Text("Local snapshots: \((s["snapshots"] as? [Any])?.count ?? 0)").font(.caption2)
            let ssd = mm.j["ssd"] as? [String: Any] ?? [:], days = ssd["days"] as? [String: Any] ?? [:]
            let today = { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; return f.string(from: Date()) }()
            Text("SSD written: today \(gb(num(days[today]))) · since boot \(gb(num(ssd["boot_w"]))) (read \(gb(num(ssd["boot_r"]))))\(ssd["lifetime_w"] != nil ? " · lifetime \(gb(num(ssd["lifetime_w"])))" : "")")
                .font(.caption2).foregroundColor(num(days[today]) > 100 << 30 ? .orange : .primary)
            ForEach(Array(((s["map"] as? [[String: Any]]) ?? []).prefix(10).enumerated()), id: \.offset) { _, f in
                let p = f["path"] as? String ?? ""
                HStack {
                    Text(p.replacingOccurrences(of: NSHomeDirectory(), with: "~")).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Text(mb(num(f["size"]))).monospacedDigit()
                    Button("Reveal") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)]) }
                }.font(.caption2).buttonStyle(.borderless)
            }
            let dups = (s["duplicates"] as? [[String: Any]]) ?? []
            if !dups.isEmpty {
                Text("Duplicates > 50 MB: \(dups.count) groups, \(gb(dups.reduce(0) { $0 + num($1["wasted"]) })) reclaimable").font(.caption2).bold()
                ForEach(Array(dups.prefix(8).enumerated()), id: \.offset) { _, g in
                    let paths = g["paths"] as? [String] ?? []
                    ForEach(Array(paths.dropFirst().enumerated()), id: \.offset) { _, p in  // the oldest copy is kept
                        HStack {
                            Text("\((p as NSString).lastPathComponent) · \(mb(num(g["size"])))").lineLimit(1).truncationMode(.middle)
                            Spacer()
                            Button("Reveal") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: p)]) }
                            Button("Trash") { mm.run("Moving to Trash…", ["trash", p]) }
                        }.font(.caption2).buttonStyle(.borderless)
                    }
                }
            }
        }
    }
}

struct OptimizeView: View {
    @ObservedObject var mm: MaintModel
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("Optimize (max potential)").font(.caption).foregroundColor(.secondary)
                Spacer()
                Button("Measure") { mm.run("Measuring…", ["optimize"]) }.font(.caption)
                Button("Apply safe") { mm.run("Applying…", ["optimize", "--apply"]) }.font(.caption)
            }
            ForEach(Array(mm.optimize.enumerated()), id: \.offset) { _, i in
                let st = i["status"] as? String ?? ""
                VStack(alignment: .leading, spacing: 0) {
                    HStack(alignment: .top) {
                        Text(st).frame(width: 58, alignment: .leading)
                            .foregroundColor(["ok", "applied", "n/a"].contains(st) ? .green : st == "hardware" ? .purple : .orange)
                        Text("\(i["title"] as? String ?? ""): \(i["measured"] as? String ?? "")")
                    }
                    if !["ok", "applied", "n/a"].contains(st), let a = i["action"] as? String, !a.isEmpty {
                        Text("→ " + a).foregroundColor(.secondary).textSelection(.enabled).padding(.leading, 62)
                    }
                }.font(.caption2)
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
                Image(nsImage: dotImage(m.color))
                Text(String(format: "%.1fG", Double(m.availB) / 1073741824))
            }
        }
        .menuBarExtraStyle(.window)
        Window("MemGuard Details", id: "details") { DetailsView(m: m) }
    }
}
