# Can ClearX ship? App Store, Developer ID, and the competition (2026-10-31)

Short answer: **the Mac App Store is not possible** for what ClearX does. **Developer ID (signed, notarized, sold
from a website) is possible**, but ClearX isn't ready for it yet. Most of the work is moving the Python daemon into
the app, plus removing the setup that only fits this Mac.

## 1. Mac App Store: no

Every App Store app must run in the App Sandbox. The sandbox blocks exactly what ClearX does to manage memory, and
Apple has no entitlement that turns it back on:

| ClearX needs | In the sandbox | Way around it |
|---|---|---|
| Pause, resume and freeze other apps (`SIGSTOP`/`SIGCONT`) | Blocked: a sandboxed app can only signal itself and its own children | None |
| Background QoS and lower priority for other apps (`taskpolicy`, `renice`) | Blocked for other processes | None |
| A daemon that runs every 10 s (LaunchAgent in `~/Library/LaunchAgents`) | Blocked. `SMAppService` can register a helper, but the helper is sandboxed too | Helper allowed, still can't touch other apps |
| CPU temperature from the SMC (IOKit `AppleSMC` user client) | Blocked | Only a `temporary-exception` entitlement, which App Review rejects |
| `launchctl setenv` (build job limits), editing other apps' preferences (Brave) | Blocked | None |
| Reading `pmset`, `vm_stat`, per-process memory | Mostly fine (Mach and libproc reads work) | — |

A sandboxed "ClearX Lite" could only show numbers and give advice. That's what the App Store's memory apps already
are, so it isn't worth building.

## 2. Developer ID (outside the App Store): yes, after this work

Apple doesn't sandbox Developer ID apps. A same-user app may signal, reprioritize and read the user's own processes
under the Hardened Runtime. That's all ClearX does, and it never needs `sudo` for its core work.

**What it needs from Apple:** the Apple Developer Program ($99/yr), a *Developer ID Application* certificate, the
Hardened Runtime (`codesign --options runtime --timestamp`), notarization (`xcrun notarytool submit --wait`) and
`xcrun stapler staple`. It's shipped as a signed .dmg or .zip.

**Entitlements:** almost none. The only one is `com.apple.security.automation.apple-events` (plus an
`NSAppleEventsUsageDescription`), and only if the app keeps using `osascript` (the "quit app" step and the
notification fallback). It's better to drop `osascript` and use `NSRunningApplication.terminate()` plus
UserNotifications, which removes the entitlement and the permission prompt.

**Blockers found in the code:**

1. **The daemon is Python and uses the system `python3`.** That's the Xcode Command Line Tools stub. On a Mac
   without them, the first run shows an install dialog and ClearX doesn't run. Notarization doesn't cover scripts.
   Fix: port `clearx.py` (about 2,100 lines, standard library only) to Swift. It becomes a login-item agent inside
   the app, registered with `SMAppService.agent(plistName:)`. This is the big job: roughly the size of the app
   itself. Bundling a Python runtime is the quicker alternative, but it's heavier and still has to be signed.
2. **Paths are fixed to this Mac.** `~/Developer/ClearX` is in `main.swift`, the LaunchAgents and `install.sh`. State
   should move to `~/Library/Application Support/ClearX`, and settings to the app (it now opens `clearx.conf`).
3. **The bundle ID is `com.user.clearx`.** It needs a reverse-DNS ID you own.
4. **It's built for Intel only.** `build.sh` targets `x86_64` only. It needs a universal build (`arm64` + `x86_64`,
   `lipo`), and a test pass on Apple silicon. The page-size fix in this sweep was part of that.
5. **The settings fit this Mac.** `clearx.conf` names MyApp, MyDAW, Brave, `llama-server` and this Mac's 4 GB Radeon.
   A public build needs neutral defaults: detect the GPU's VRAM, and no project names.
6. **No onboarding or uninstall in the app.** Today `install.sh` and `uninstall.sh` do it. The app also needs a
   first-run explanation of what it pauses and why, since freezing apps surprises people.

**Order of work:** (1) Swift agent + `SMAppService`, (2) paths/bundle ID/universal build, (3) neutral defaults and
onboarding, (4) Developer ID signing + notarization in `build.sh`. Steps 2 and 4 are a day or two. Step 1 is the
real project.

## 3. How it compares

Competitors are described as I know them, up to mid-2026. I didn't re-check their sites today.

| Tool | Where sold | What it does to memory | Process control |
|---|---|---|---|
| **macOS alone** | built in | Compressor, swap, App Nap | App Nap only |
| **CleanMyMac** (MacPaw) | Website + App Store (cut-down) | One-click "free up RAM" (a purge) | No continuous control |
| **Memory Clean 3** (FIPLAB) | App Store | One-shot purge of inactive memory | None |
| **iStat Menus** (Bjango) | Website/Setapp | Monitoring only (very good) | None |
| **App Tamer** (St. Clair) | Website (Developer ID) | Nothing direct | Slows/stops background apps' CPU per app. The closest rival |
| **ClearX** | Not shipped | Acts only under strain: freezes hidden idle apps and wakes them on activation, pauses big background jobs, guards the 3 GB reserve, watches the 4 GB VRAM and its spill into RAM | Pressure-, heat- and build-aware; sets build job counts (Xcode, CMake) |

**Where ClearX is ahead (measured on the test iMac, [MEASUREMENTS.md](MEASUREMENTS.md)):** app switch 225 ms vs 1378 ms with macOS alone
under the same load. It acts continuously but only under strain (no "purge" button that just refills a cache). It
is the only tool here that manages builds and graphics memory. It gives back control on its own once the Mac is
fine.

**Where it's behind:** it isn't signed or installable by anyone else. It's tuned to one Mac. It needs Python and
has no onboarding. It isn't tested on Apple silicon (where 16 KB pages and unified memory change the VRAM side
completely). And it's English only, with no accessibility pass.

## 4. About the macOS 27 look

The redesign imitates Liquid Glass with SwiftUI shapes and gradients, because this iMac runs macOS 15. It can't run
macOS 26 or later, and the SDK it builds with has no `glassEffect`. A public build made with Xcode 26 or later
should use the real `glassEffect` behind `if #available(macOS 26, *)` and keep the current look as the fallback.
