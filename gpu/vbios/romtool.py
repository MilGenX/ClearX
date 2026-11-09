#!/usr/bin/env python3
"""Safety checks for AMD (ATOM) vBIOS images before flashing.

  python3 romtool.py check backup.rom
  python3 romtool.py compare backup.rom modded.rom     # must be same GPU, same size
  python3 romtool.py fix modded.rom                    # recompute checksum in place
Exit code 0 = pass, 1 = fail. flash.ps1 refuses to flash unless these pass.
"""
import sys

def info(rom: bytes) -> dict:
    e = []
    if rom[:2] != b"\x55\xAA":
        e.append("missing 55AA option-ROM signature")
    img = rom[2] * 512
    if b"761295520" not in rom[0x30:0x40]:
        e.append("ATOM BIOS marker not found")
    pcir = int.from_bytes(rom[0x18:0x1A], "little")
    vendor = device = None
    if rom[pcir:pcir + 4] == b"PCIR":
        vendor = int.from_bytes(rom[pcir + 4:pcir + 6], "little")
        device = int.from_bytes(rom[pcir + 6:pcir + 8], "little")
        if vendor != 0x1002:
            e.append(f"vendor 0x{vendor:04X} is not AMD")
    else:
        e.append("PCIR structure not found")
    if not 0 < img <= len(rom):
        e.append(f"image size {img} invalid")
    elif sum(rom[:img]) & 0xFF:
        e.append("checksum mismatch (run: romtool.py fix)")
    return {"errors": e, "size": img, "file": len(rom), "vendor": vendor, "device": device}

def show(name, i):
    dev = f"{i['vendor'] or 0:04X}:{i['device'] or 0:04X}"
    print(f"{name}: device {dev}, image {i['size']} B, file {i['file']} B -> " + ("OK" if not i["errors"] else "FAIL"))
    for x in i["errors"]:
        print("   - " + x)

def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd, files = sys.argv[1], sys.argv[2:]
    roms = [open(f, "rb").read() for f in files]
    if cmd == "check":
        i = info(roms[0]); show(files[0], i); sys.exit(1 if i["errors"] else 0)
    if cmd == "compare":
        a, b = info(roms[0]), info(roms[1]); show(files[0], a); show(files[1], b)
        bad = a["errors"] or b["errors"]
        if (a["vendor"], a["device"]) != (b["vendor"], b["device"]):
            print("FAIL: different GPU device IDs"); bad = True
        if a["file"] != b["file"] or a["size"] != b["size"]:
            print("FAIL: size differs from original"); bad = True
        if roms[0] == roms[1]:
            print("Note: files are identical (nothing modified)")
        sys.exit(1 if bad else 0)
    if cmd == "fix":
        r = bytearray(roms[0]); n = r[2] * 512
        r[0x21] = 0; r[0x21] = (-sum(r[:n])) & 0xFF   # ATOM checksum byte
        open(files[0], "wb").write(r); show(files[0], info(bytes(r)))
        return
    sys.exit(__doc__)

main()
