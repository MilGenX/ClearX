// The user's two targets: a RAM reserve (ready-to-use memory) and a free-storage goal.
import SwiftUI
import AppKit

/// "Reserve: 10 GB ✓" in the menu, from the daemon's state.json.
struct ReserveRow: View {
    @ObservedObject var m: Model
    var body: some View {
        if let r = m.reserve {
            let target = num(r["target"]), ready = num(r["ready"])
            let ok = r["ok"] as? Bool ?? false, reachable = r["reachable"] as? Bool ?? true
            HStack(spacing: 8) {
                Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                    .foregroundColor(ok ? Theme.accent : Theme.warn)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Reserve: \(gb(target)) \(ok ? "✓" : "✗") · \(gb(ready)) ready now")
                        .font(.system(size: 12, weight: .semibold)).foregroundColor(Theme.text)
                    if !ok {
                        Text(reachable ? "Freezing hidden apps and slowing builds to get back to \(gb(target))."
                                       : "What you're using plus the build needs more than that. The reserve comes back when the build finishes.")
                            .font(.system(size: 11)).foregroundColor(Theme.sub).fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer()
            }
        }
    }
}

/// "Reach 250 GB free" plan on the Storage page. Nothing is deleted from here: each item opens or reveals.
struct StoragePlanView: View {
    @ObservedObject var mm: MaintModel
    var body: some View {
        let p = mm.j["plan"] as? [String: Any] ?? [:]
        let items = p["items"] as? [[String: Any]] ?? []
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Reach \(gbd(num(p["goal"])).replacingOccurrences(of: ".0", with: "")) free").font(.headline)
                Spacer()
                Button(mm.busy ?? "Work it out") { mm.run("Measuring…", ["plan", "250"]) }.disabled(mm.busy != nil)
            }
            if p.isEmpty {
                Text("Click “Work it out” to see what it would take.").foregroundColor(Theme.sub)
            } else {
                let reachable = p["reachable"] as? Bool ?? false
                Text("Available now \(gbd(num(p["free"]))) (as Finder counts it). Needed: \(gbd(num(p["gap"]))) more. Everything below adds up to \(gbd(num(p["candidates"]))): " +
                     (reachable ? "enough." : "not quite enough on its own; the rest would have to come from your own files."))
                    .fixedSize(horizontal: false, vertical: true)
                ForEach(Array(items.enumerated()), id: \.offset) { _, i in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(i["title"] as? String ?? "").bold().foregroundColor(Theme.text)
                            Spacer()
                            Text(gbd(num(i["size"]))).monospacedDigit()
                            Button("Show") { act(i["action"] as? String ?? "") }
                        }
                        Text(i["how"] as? String ?? "").font(.caption).foregroundColor(Theme.sub).fixedSize(horizontal: false, vertical: true)
                        if let n = i["note"] as? String, !n.isEmpty {
                            Text(n).font(.caption).foregroundColor(Theme.warn).fixedSize(horizontal: false, vertical: true)
                        }
                    }.padding(.vertical, 2)
                }
            }
        }
    }
    func act(_ a: String) {
        if a.hasPrefix("reveal:") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: String(a.dropFirst(7)))]) }
        else if a.hasPrefix("open:") { NSWorkspace.shared.open(URL(fileURLWithPath: String(a.dropFirst(5)))) }
    }
}
