// App Freezer, app side. The daemon (clearx.py) freezes hidden, idle, big apps with SIGSTOP while memory is
// tight; this wakes them with SIGCONT the instant macOS activates them (Cmd-Tab, Dock, a click on a window),
// and records every activation so the daemon knows what you've just used. If ClearX.app isn't running,
// the daemon wakes everything and stops freezing.
import SwiftUI
import AppKit
import Darwin

final class Freezer: ObservableObject {
    static let shared = Freezer()
    @Published var frozen: [String] = []       // app names currently frozen
    @Published var lastWakeMs: Double? = nil
    private var activations: [String: Double] = [:]
    private var timer: Timer?
    private let actFile = MG_DIR + "/state/activations.json"
    private let wakeLog = MG_DIR + "/logs/wake.log"

    private init() {
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: nil) { [weak self] n in
            guard let app = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication else { return }
            self?.wake(app.processIdentifier, name: app.localizedName ?? "?")
        }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
    }

    private static let shells: Set<String> = ["zsh", "bash", "sh", "fish", "login", "tmux", "screen"]  // SHELLS in clearx.py

    private static func name(_ pid: pid_t) -> String {
        var buf = [CChar](repeating: 0, count: 256)
        proc_name(pid, &buf, 256)
        return String(cString: buf)
    }

    /// The app and its descendants (helpers, renderers), not through shells, like tree() in clearx.py: what a shell
    /// started is a job of its own, and may be held for a reason that has nothing to do with this app being in front.
    static func tree(_ root: pid_t) -> [pid_t] {
        var out: [pid_t] = [], todo = [root]
        while let p = todo.popLast() {
            out.append(p)
            var kids = [pid_t](repeating: 0, count: 512)
            let n = proc_listchildpids(p, &kids, Int32(kids.count * MemoryLayout<pid_t>.size))
            if n > 0 { todo += kids.prefix(Int(n)).filter { $0 > 0 && !out.contains($0) && !shells.contains(name($0)) } }
        }
        return out
    }

    static func stopped(_ pid: pid_t) -> Bool {
        var info = proc_bsdinfo()
        return proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, Int32(MemoryLayout<proc_bsdinfo>.size)) > 0 && info.pbi_status == 4  // SSTOP
    }

    func wake(_ pid: pid_t, name: String) {
        let t0 = DispatchTime.now().uptimeNanoseconds
        let stoppedPids = Freezer.tree(pid).filter { Freezer.stopped($0) }
        for p in stoppedPids { kill(p, SIGCONT) }
        let ms = Double(DispatchTime.now().uptimeNanoseconds - t0) / 1e6
        activations[name] = Date().timeIntervalSince1970
        if let d = try? JSONSerialization.data(withJSONObject: activations) {
            try? d.write(to: URL(fileURLWithPath: actFile), options: .atomic)
        }
        if !stoppedPids.isEmpty {
            DispatchQueue.main.async { self.lastWakeMs = ms; self.frozen.removeAll { $0 == name } }
            let line = String(format: "%@ woke %@ (%d processes) in %.2f ms\n",
                              ISO8601DateFormatter().string(from: Date()), name, stoppedPids.count, ms)
            if let h = FileHandle(forWritingAtPath: wakeLog) { h.seekToEndOfFile(); h.write(Data(line.utf8)); try? h.close() }
            else { try? line.write(toFile: wakeLog, atomically: true, encoding: .utf8) }
        }
    }

    func refresh() {
        guard let d = try? Data(contentsOf: URL(fileURLWithPath: STATE)),
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let paused = j["paused"] as? [String: [String: Any]] else { if !frozen.isEmpty { frozen = [] }; return }
        let now = Array(Set(paused.values.filter { $0["kind"] as? String == "freeze" }.compactMap { $0["app"] as? String })).sorted()
        if now != frozen { frozen = now }  // (an unchanged assignment would still redraw the menu every 2 s)
    }

    func wakeAll() {
        for (_, app) in NSWorkspace.shared.runningApplications.enumerated() where frozen.contains(app.localizedName ?? "") {
            wake(app.processIdentifier, name: app.localizedName ?? "")
        }
        DispatchQueue.global().async { mg(["wake", "all"]); DispatchQueue.main.async { self.refresh() } }
    }
}

/// "Frozen: N apps (tap to wake)" row for the menu.
struct FrozenRow: View {
    @ObservedObject var f = Freezer.shared
    var body: some View {
        if !f.frozen.isEmpty {
            Button(action: { f.wakeAll() }) {
                HStack(spacing: 8) {
                    Image(systemName: "snowflake").foregroundColor(Theme.info)
                    VStack(alignment: .leading, spacing: 1) {
                        Text("Frozen: \(f.frozen.count) app\(f.frozen.count == 1 ? "" : "s") (tap to wake)")
                            .font(.system(size: 12, weight: .semibold)).foregroundColor(Theme.text)
                        Text(f.frozen.joined(separator: ", ") + ". They wake instantly when you switch to them.")
                            .font(.system(size: 11)).foregroundColor(Theme.sub).lineLimit(2)
                    }
                    Spacer()
                }.contentShape(Rectangle())
            }.buttonStyle(.plain)
        }
    }
}
