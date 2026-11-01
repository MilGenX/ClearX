// Settings page and first-run welcome. Settings are read with `clearx settings` and each change is written to
// clearx.local.conf with `clearx set` (the daemon reads it on its next tick, within 10 s).
import SwiftUI

final class SettingsModel: ObservableObject {
    @Published var v: [String: String] = [:]
    @Published var saved: String? = nil
    @Published var loaded = false  // nothing is written until the real values are in (or a default would overwrite them)
    func load() {
        DispatchQueue.global().async {
            let out = mg(["settings"])
            let j = out.split(separator: "\n").last.flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: String] }
            DispatchQueue.main.async { self.v = j ?? [:]; self.loaded = j != nil }
        }
    }
    func set(_ key: String, _ value: String) {
        guard loaded, v[key] != value else { return }
        v[key] = value
        DispatchQueue.global().async {
            _ = mg(["set", key, value])
            DispatchQueue.main.async { self.saved = "Saved. ClearX uses it within 10 seconds." }
        }
    }
    func bool(_ key: String) -> Binding<Bool> {
        Binding(get: { (self.v[key] ?? "true").lowercased() == "true" }, set: { self.set(key, $0 ? "true" : "false") })
    }
}

/// One setting: a title, a sentence on what it does, and its control.
struct SettingRow<Control: View>: View {
    let title: String, detail: String
    let control: Control
    init(_ title: String, _ detail: String, @ViewBuilder control: () -> Control) {
        self.title = title; self.detail = detail; self.control = control()
    }
    var body: some View {
        HStack(alignment: .top, spacing: 16) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .semibold)).foregroundColor(Theme.text)
                Text(detail).font(.system(size: 11)).foregroundColor(Theme.sub).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 12)
            control
        }
        .accessibilityElement(children: .contain).accessibilityLabel(title)
    }
}

/// A comma-separated list of app names, saved when you press Return or leave the field.
struct ListField: View {
    @ObservedObject var s: SettingsModel
    let key: String, placeholder: String
    @State private var text = ""
    @State private var edited = false  // saved only after you type in it (focus moving in or out alone never saves)
    @FocusState private var focused: Bool
    var body: some View {
        TextField(placeholder, text: $text)
            .textFieldStyle(.plain).font(.system(size: 12)).foregroundColor(Theme.text)
            .padding(.horizontal, 12).padding(.vertical, 8)
            .glass(10)
            .focused($focused)
            .disabled(!s.loaded)
            .onAppear { text = s.v[key] ?? "" }
            .onChange(of: s.v[key] ?? "") { if !edited { text = $0 } }
            .onChange(of: text) { if focused && s.loaded && $0 != (s.v[key] ?? "") { edited = true } }
            .onChange(of: focused) { if !$0 { save() } }
            .onSubmit { save() }
    }
    func save() {
        guard edited else { return }
        edited = false
        s.set(key, text)
    }
}

struct SettingsView: View {
    @ObservedObject var m: Model
    @StateObject var s = SettingsModel()
    var reserveGB: Int { Int((s.v["reserve.target"] ?? "3G").filter(\.isNumber)) ?? 3 }
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            PageHeader(title: "Settings", sub: "ClearX acts only when your Mac is under strain. These choose what it may touch.")
            Card("ClearX") {
                SettingRow("ClearX is on", "Off resumes everything it paused and stops acting until you turn it back on.") {
                    Toggle("", isOn: Binding(get: { m.enabled }, set: { m.act([$0 ? "on" : "off"]) })).labelsHidden()
                }
                SettingRow("Notifications", "Tell me when memory gets very short or the Mac overheats.") {
                    Toggle("", isOn: s.bool("general.notify")).labelsHidden()
                }
            }
            Card("Memory") {
                SettingRow("Keep ready", "Memory ClearX keeps free for what you open next. More reacts sooner; 3 GB suits a 16 GB Mac.") {
                    Stepper("\(reserveGB) GB", value: Binding(get: { reserveGB }, set: { s.set("reserve.target", "\($0)G") }), in: 1...16)
                        .disabled(!s.loaded)
                        .font(.system(size: 13, weight: .semibold).monospacedDigit())
                }
                SettingRow("App Freezer", "Under strain, pause hidden apps you haven't used for 5 minutes. They wake the instant you switch to them.") {
                    Toggle("", isOn: s.bool("freezer.enabled")).labelsHidden()
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text("Never freeze").font(.system(size: 13, weight: .semibold)).foregroundColor(Theme.text)
                    ListField(s: s, key: "freezer.never", placeholder: "App names, separated by commas")
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text("Always leave alone").font(.system(size: 13, weight: .semibold)).foregroundColor(Theme.text)
                    Text("Whole apps and everything they start (servers, helpers) are never paused, frozen or slowed. * matches anything.")
                        .font(.system(size: 11)).foregroundColor(Theme.sub)
                    ListField(s: s, key: "protected.apps", placeholder: "e.g. MyApp*")
                }
            }
            Card("Work") {
                SettingRow("Builds step back while you work", "While you're using the Mac, automatic builds run at background priority; full speed once you step away.") {
                    Toggle("", isOn: s.bool("interactive.enabled")).labelsHidden()
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text("Audio apps").font(.system(size: 13, weight: .semibold)).foregroundColor(Theme.text)
                    Text("While one is open and the Mac is strained, everything else gets out of its way.")
                        .font(.system(size: 11)).foregroundColor(Theme.sub)
                    ListField(s: s, key: "modes.studio_apps", placeholder: "Logic Pro, Ableton Live*")
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text("Your build scripts").font(.system(size: 13, weight: .semibold)).foregroundColor(Theme.text)
                    Text("So they wait their turn with Xcode, CMake and Cargo builds instead of running all at once.")
                        .font(.system(size: 11)).foregroundColor(Theme.sub)
                    ListField(s: s, key: "queue.scripts", placeholder: "e.g. build.sh")
                }
            }
            HStack {
                if let t = s.saved { Label(t, systemImage: "checkmark.circle.fill").font(.system(size: 11)).foregroundColor(Theme.accent) }
                Spacer()
                Button("Advanced settings file") {
                    let local = MG_DIR + "/config/clearx.local.conf"
                    if !FileManager.default.fileExists(atPath: local) {
                        try? FileManager.default.copyItem(atPath: MG_DIR + "/config/clearx.local.conf.example", toPath: local)
                    }
                    NSWorkspace.shared.open(URL(fileURLWithPath: local))
                }
            }
        }
        .toggleStyle(MintSwitch())
        .onAppear { s.load() }
    }
}

/// Shown at the top of the menu until dismissed once.
struct WelcomeCard: View {
    @AppStorage("welcomed") var welcomed = false
    let openSettings: () -> Void
    var body: some View {
        if !welcomed {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    Image(systemName: "sparkles").foregroundColor(Theme.accent)
                    Text("Welcome to ClearX").font(.system(size: 14, weight: .semibold)).foregroundColor(Theme.text)
                }
                VStack(alignment: .leading, spacing: 6) {
                    bullet("gauge.with.dots.needle.33percent", "It acts only when memory, heat or the disk run short. On a healthy Mac it does nothing.")
                    bullet("snowflake", "Hidden idle apps may be paused under strain. They wake the instant you switch to them.")
                    bullet("lock.shield", "The app in front, audio, calls, downloads and macOS itself are never touched.")
                }
                HStack {
                    Button("Settings…") { welcomed = true; openSettings() }.buttonStyle(GhostButton())
                    Spacer()
                    Button("Got it") { welcomed = true }.buttonStyle(GhostButton())
                }
            }
            .padding(14).glass(18, tint: Theme.accent, wash: 0.07)
        }
    }
    func bullet(_ icon: String, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon).frame(width: 16).foregroundColor(Theme.accent).accessibilityHidden(true)
            Text(text).font(.system(size: 11)).foregroundColor(Theme.sub).fixedSize(horizontal: false, vertical: true)
        }
    }
}
