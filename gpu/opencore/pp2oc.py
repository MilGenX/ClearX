#!/usr/bin/env python3
"""Turn a MorePowerTool PowerPlay table into an OpenCore DeviceProperties entry.

Input:  a .reg file saved by MorePowerTool (contains PP_PhmSoftPowerPlayTable)
        or a raw .bin table.
Output: a <dict> to paste under DeviceProperties > Add in OpenCore's config.plist,
        or --patch config.plist to insert it directly (backup written first).

  python3 pp2oc.py table.reg --path "PciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)"
  python3 pp2oc.py table.reg --path ... --patch /Volumes/EFI/EFI/OC/config.plist
"""
import argparse, plistlib, re, shutil, sys

KEY = "PP_PhmSoftPowerPlayTable"

def from_reg(text: str) -> bytes:
    m = re.search(r'"%s"\s*=\s*hex:([0-9a-fA-F,\s\\]+)' % KEY, text)
    if not m:
        sys.exit(f"No {KEY} in .reg file")
    hexstr = re.sub(r"[^0-9a-fA-F,]", "", m.group(1))
    return bytes(int(h, 16) for h in hexstr.split(",") if h)

def load(p: str) -> bytes:
    raw = open(p, "rb").read()
    if p.lower().endswith(".reg"):
        for enc in ("utf-16", "utf-8", "latin-1"):
            try:
                return from_reg(raw.decode(enc))
            except UnicodeDecodeError:
                continue
    return raw

def check(t: bytes):
    # ATOM common table header: uint16 size, uint8 format rev, uint8 content rev
    if len(t) < 4:
        sys.exit("Table too small")
    size = int.from_bytes(t[:2], "little")
    if size != len(t):
        sys.exit(f"Header size {size} != data length {len(t)} — wrong/corrupt table")
    print(f"PowerPlay table OK: {len(t)} bytes, format rev {t[2]}, content rev {t[3]}", file=sys.stderr)

ap = argparse.ArgumentParser()
ap.add_argument("table")
ap.add_argument("--path", required=True, help="GPU device path (from: gfxutil -f GFX0, or ioreg)")
ap.add_argument("--patch", help="OpenCore config.plist to update in place")
a = ap.parse_args()

table = load(a.table)
check(table)
entry = {a.path: {KEY: table}}

if a.patch:
    shutil.copy(a.patch, a.patch + ".bak")
    with open(a.patch, "rb") as f:
        cfg = plistlib.load(f)
    cfg.setdefault("DeviceProperties", {}).setdefault("Add", {}).setdefault(a.path, {})[KEY] = table
    with open(a.patch, "wb") as f:
        plistlib.dump(cfg, f)
    print(f"Patched {a.patch} (backup: {a.patch}.bak)")
else:
    sys.stdout.write(plistlib.dumps(entry).decode())
