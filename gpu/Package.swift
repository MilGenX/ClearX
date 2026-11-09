// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "ClearXGPU",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "ClearXGPU",
            path: "Sources/ClearXGPU",
            linkerSettings: [.linkedFramework("IOKit"), .linkedFramework("CoreGraphics")]
        )
    ]
)
