import Foundation

func fmt(_ v: Double?, _ u: String) -> String { v.map { String(format: "%.0f%@", $0, u) } ?? "—" }

func status() {
    let s = GPU.stats(), smc = try? SMC()
    let temp = smc?.gpuTemp() ?? s.temp
    print("Radeon Pro 575X")
    print("  Temp \(fmt(temp, "°C"))  Util \(fmt(s.util, "%"))  Core \(fmt(s.coreMHz, " MHz"))  Mem \(fmt(s.memMHz, " MHz"))  Power \(fmt(s.watts, " W"))  VRAM \(fmt(s.vramUsedMB, " MB"))")
    if let smc {
        for i in 0..<Int(smc.double("FNum") ?? 1) {
            print("  Fan \(i): \(fmt(smc.double("F\(i)Ac"), " rpm")) (min \(fmt(smc.double("F\(i)Mn"), "")), max \(fmt(smc.double("F\(i)Mx"), "")))")
        }
    }
    if let t = temp, t >= 85 { print("  ⚠ Likely throttling — run: sudo clearx-gpu govern max") }
}

func uiLite() {
    for (dom, key) in [("com.apple.universalaccess", "reduceTransparency"), ("com.apple.universalaccess", "reduceMotion")] {
        let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/defaults")
        p.arguments = ["write", dom, key, "-bool", "true"]
        try? p.run(); p.waitUntilExit()
        print(p.terminationStatus == 0 ? "\(key): on" : "\(key): blocked — enable in System Settings › Accessibility › Display (or give Terminal Full Disk Access)")
    }
}

func signalPid(_ arg: String?, _ sig: Int32) {
    guard let a = arg, let pid = Int32(a) else { print("Need a pid (see: clearx-gpu clients)"); return }
    print(kill(pid, sig) == 0 ? "OK" : "Failed (errno \(errno)) — own processes only, or use sudo")
}

let args = CommandLine.arguments.dropFirst()
switch args.first ?? "status" {
case "status": status()
case "watch": while true { print("\u{1B}[2J\u{1B}[H", terminator: ""); status(); sleep(2) }
case "clients": for c in GPU.clients() { print(String(format: "%7d  %@", c.pid, c.name)) }
case "native": print(Display.setNative())
case "ui-lite": uiLite()
case "pause": signalPid(args.dropFirst().first, SIGSTOP)
case "resume": signalPid(args.dropFirst().first, SIGCONT)
case "govern":
    guard getuid() == 0 else { print("Fan control needs root: sudo clearx-gpu govern [max|balanced|quiet]"); exit(1) }
    let p = Profile(rawValue: args.dropFirst().first ?? "balanced") ?? .balanced
    do { try Governor(p).run() } catch { print("SMC error: \(error)"); exit(1) }
case "boost":   // one-shot "Max" profile: native scaling + lite UI + reminder to run governor
    print(Display.setNative()); uiLite()
    print("Now run: sudo clearx-gpu govern max  (or install the daemon: ./install.sh)")
case "restore":
    guard getuid() == 0 else { print("sudo clearx-gpu restore"); exit(1) }
    do { try Governor(.balanced).restore() } catch { print("SMC error: \(error)") }
case "raw": for (k, v) in GPU.stats().raw.sorted(by: { $0.key < $1.key }) { print("\(k): \(v)") }
default:
    print("""
    clearx-gpu status | watch | clients | raw
               native        set 5K display to native HiDPI (biggest GPU saving)
               ui-lite       reduce transparency + motion
               boost         native + ui-lite in one go
               pause|resume <pid>
               govern [max|balanced|quiet]   (sudo) thermal fan governor
               restore       (sudo) put fans back to stock
    """)
}
