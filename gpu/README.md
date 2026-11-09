# ClearX GPU — Radeon Pro 575X (iMac 2019, 27")

Three layers. Do them in order and stop when it's fast enough.

## 1. ClearX GPU module (safe, no SIP, no OpenCore)
The 575X loses most of its speed to heat and to the 5K screen, not to stock clocks. This keeps it at full boost.

```
swift build -c release
.build/release/ClearXGPU status      # temp, clocks, power, fans
.build/release/ClearXGPU boost       # native 5K scaling + reduce transparency/motion
.build/release/ClearXGPU clients     # which apps are using the GPU
./install.sh max                     # fan governor at boot (max | balanced | quiet)
./install.sh --uninstall             # removes it and puts fans back to stock
```
- The governor only raises the fan **minimum**, so macOS can still spin faster. It can never run cooler-than-stock fans slower. Stopping it restores stock fans.
- If `status` shows blank clocks, run `ClearXGPU raw` and send me the output; key names vary by macOS version.
- You can fold this into the ClearX app later; the Sources files are self-contained.

## 2. OpenCore PowerPlay (reversible — test from USB)
Raises the power limit and clocks, or undervolts, using Apple's own driver.
1. In Windows (Boot Camp), run GPU-Z, save the BIOS, and open it in **MorePowerTool**.
2. Make conservative changes first: power limit +10–15%, or a −50 mV undervolt at stock clocks. Click **Save** to get a `.reg` file.
3. In macOS, get the GPU path: `gfxutil -f GFX0`. On this iMac it's usually `PciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)`, but use what it prints.
4. Build an OpenCore EFI for the iMac on a **USB stick** (not the internal disk), then run:
   `python3 opencore/pp2oc.py table.reg --path "<path>" --patch /Volumes/EFI/EFI/OC/config.plist`
5. Hold Option at boot and pick the USB. Run `ClearXGPU watch` under load to check clocks, power and temps.
6. To undo, unplug the USB. Only move OpenCore to the internal disk once it's stable for days.

Honest caveat: this property is proven on Vega/Navi. On Polaris (575X) the driver may ignore it. Step 5 tells you: if the clocks and power don't change, it was ignored, and the vBIOS route is the remaining option.

## 3. vBIOS flash (permanent — last resort)

> ⚠️ **Don't, on the church iMac.** The 575X is soldered on: a bad flash leaves the Mac with no screen. Layer 1 (the fan
> governor, installed 2026-11-07 as `/Library/LaunchDaemons/com.clearx.gpu.plist`) is what's running; stop there.
The change lives on the GPU itself, so macOS uses it with no OpenCore and no SIP changes.
**Risk:** the 575X is soldered on. A bad flash means no display until you restore it blind.

Before you start, in Windows: install **OpenSSH Server** and confirm you can SSH in from another device. That's your way back if the screen goes dark.

```
.\flash.ps1 backup                 # 2 backups, verified identical. Copy backup1.rom off the Mac.
# edit a COPY in PolarisBiosEditor (power/clocks), then:
python romtool.py fix modded.rom
.\flash.ps1 flash modded.rom       # refuses if ID, size or checksum is wrong
.\flash.ps1 restore                # back to original, works blind over SSH
```
Keep the edits small: power +10–15% or a mild undervolt. Shut down fully after flashing.
