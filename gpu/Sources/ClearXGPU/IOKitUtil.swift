import Foundation
import IOKit

/// Caller owns returned objects (IOObjectRelease each).
func iterate(_ it: io_iterator_t) -> [io_object_t] {
    var a: [io_object_t] = []
    var o = IOIteratorNext(it)
    while o != 0 { a.append(o); o = IOIteratorNext(it) }
    IOObjectRelease(it)
    return a
}

func ioServices(_ cls: String) -> [io_object_t] {
    var it: io_iterator_t = 0
    guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching(cls), &it) == KERN_SUCCESS else { return [] }
    return iterate(it)
}

func ioChildren(_ o: io_object_t) -> [io_object_t] {
    var it: io_iterator_t = 0
    guard IORegistryEntryGetChildIterator(o, kIOServicePlane, &it) == KERN_SUCCESS else { return [] }
    return iterate(it)
}

func prop(_ o: io_object_t, _ k: String) -> Any? {
    IORegistryEntryCreateCFProperty(o, k as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue()
}

/// Radeon accelerator (skips the iMac's headless Intel iGPU).
func isAMD(_ o: io_object_t) -> Bool {
    let c = IOObjectCopyClass(o).takeRetainedValue() as String
    return c.contains("AMD") || c.contains("ATI")
}
