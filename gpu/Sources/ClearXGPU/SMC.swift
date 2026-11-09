import Foundation
import IOKit

/// Mirrors the kernel's SMCKeyData_t (80 bytes). `padding` fixes Swift's packing after KeyInfo.
struct SMCParam {
    struct Vers { var major: UInt8 = 0, minor: UInt8 = 0, build: UInt8 = 0, reserved: UInt8 = 0, release: UInt16 = 0 }
    struct PLimit { var version: UInt16 = 0, length: UInt16 = 0, cpu: UInt32 = 0, gpu: UInt32 = 0, mem: UInt32 = 0 }
    struct KeyInfo { var dataSize: UInt32 = 0, dataType: UInt32 = 0, dataAttributes: UInt8 = 0 }
    var key: UInt32 = 0
    var vers = Vers()
    var pLimit = PLimit()
    var keyInfo = KeyInfo()
    var padding: UInt16 = 0
    var result: UInt8 = 0
    var status: UInt8 = 0
    var data8: UInt8 = 0
    var data32: UInt32 = 0
    var bytes: (UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8,
                UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8,
                UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8,
                UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8) =
        (0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0)
}

enum SMCError: Error { case open(kern_return_t), call(kern_return_t, UInt8), noKey(String), type(String) }

func fourCC(_ s: String) -> UInt32 { s.utf8.reduce(0) { $0 << 8 | UInt32($1) } }
func fromFourCC(_ v: UInt32) -> String {
    String(bytes: [24, 16, 8, 0].map { UInt8((v >> $0) & 0xFF) }, encoding: .ascii) ?? "????"
}

final class SMC {
    private var conn: io_connect_t = 0

    init() throws {
        let svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSMC"))
        defer { IOObjectRelease(svc) }
        let kr = IOServiceOpen(svc, mach_task_self_, 0, &conn)
        guard kr == KERN_SUCCESS else { throw SMCError.open(kr) }
    }
    deinit { IOServiceClose(conn) }

    private func call(_ inp: inout SMCParam) throws -> SMCParam {
        var out = SMCParam()
        var outSize = MemoryLayout<SMCParam>.stride
        let kr = IOConnectCallStructMethod(conn, 2, &inp, MemoryLayout<SMCParam>.stride, &out, &outSize)
        guard kr == KERN_SUCCESS, out.result == 0 else { throw SMCError.call(kr, out.result) }
        return out
    }

    func info(_ key: String) throws -> (size: Int, type: String) {
        var p = SMCParam(); p.key = fourCC(key); p.data8 = 9
        guard let o = try? call(&p) else { throw SMCError.noKey(key) }
        return (Int(o.keyInfo.dataSize), fromFourCC(o.keyInfo.dataType))
    }

    func readBytes(_ key: String) throws -> (bytes: [UInt8], type: String) {
        let i = try info(key)
        var p = SMCParam(); p.key = fourCC(key); p.keyInfo.dataSize = UInt32(i.size); p.data8 = 5
        var o = try call(&p)
        let b = withUnsafeBytes(of: &o.bytes) { Array($0.prefix(i.size)) }
        return (b, i.type)
    }

    /// Requires root.
    func writeBytes(_ key: String, _ data: [UInt8]) throws {
        let i = try info(key)
        guard data.count == i.size else { throw SMCError.type(key) }
        var p = SMCParam(); p.key = fourCC(key); p.keyInfo.dataSize = UInt32(i.size); p.data8 = 6
        withUnsafeMutableBytes(of: &p.bytes) { for (n, v) in data.enumerated() { $0[n] = v } }
        _ = try call(&p)
    }

    // MARK: typed helpers
    func double(_ key: String) -> Double? {
        guard let (b, t) = try? readBytes(key) else { return nil }
        switch t {
        case "sp78" where b.count == 2: return Double(Int16(bitPattern: UInt16(b[0]) << 8 | UInt16(b[1]))) / 256
        case "fpe2" where b.count == 2: return Double(UInt16(b[0]) << 8 | UInt16(b[1])) / 4
        case "flt " where b.count == 4: return Double(b.withUnsafeBytes { $0.loadUnaligned(as: Float32.self) })
        case "ui8 ": return Double(b[0])
        case "ui16" where b.count == 2: return Double(UInt16(b[0]) << 8 | UInt16(b[1]))
        default: return nil
        }
    }

    func setRPM(_ key: String, _ rpm: Double) throws {
        let t = try info(key).type
        switch t {
        case "fpe2": let v = UInt16(max(0, rpm) * 4); try writeBytes(key, [UInt8(v >> 8), UInt8(v & 0xFF)])
        case "flt ": var f = Float32(rpm); try writeBytes(key, withUnsafeBytes(of: &f) { Array($0) })
        default: throw SMCError.type("\(key):\(t)")
        }
    }

    /// First readable key wins (2019 iMac GPU sensors vary by board).
    func gpuTemp() -> Double? {
        for k in ["TG0D", "TGDD", "TG0P", "TG0H"] { if let v = double(k), v > 0, v < 130 { return v } }
        return nil
    }
}
