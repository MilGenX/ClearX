// ClearX "Turbine" mark for the menu bar: four swept blades + hub, drawn as a template so macOS tints it.
import AppKit
func turbineImage() -> NSImage {
    let img = NSImage(size: NSSize(width: 16, height: 16), flipped: true) { r in
        let s = r.width / 100
        NSColor.black.set()
        let c = NSPoint(x: 50 * s, y: 50 * s)
        for k in 0..<4 {
            let blade = NSBezierPath()
            blade.move(to: NSPoint(x: 50 * s, y: 50 * s))
            blade.curve(to: NSPoint(x: 6 * s, y: 6 * s), controlPoint1: NSPoint(x: 42 * s, y: 30 * s), controlPoint2: NSPoint(x: 26 * s, y: 12 * s))
            blade.curve(to: NSPoint(x: 38 * s, y: 50 * s), controlPoint1: NSPoint(x: 18 * s, y: 22 * s), controlPoint2: NSPoint(x: 28 * s, y: 36 * s))
            blade.close()
            var t = AffineTransform(translationByX: c.x, byY: c.y)
            t.rotate(byDegrees: CGFloat(90 * k))
            t.translate(x: -c.x, y: -c.y)
            blade.transform(using: t)
            blade.fill()
        }
        NSBezierPath(ovalIn: NSRect(x: 42 * s, y: 42 * s, width: 16 * s, height: 16 * s)).fill()
        return true
    }
    img.isTemplate = true
    return img
}
