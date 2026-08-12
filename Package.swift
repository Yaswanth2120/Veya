// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Veya",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "Veya", targets: ["Veya"])
    ],
    dependencies: [
        .package(url: "https://github.com/groue/GRDB.swift.git", from: "6.29.0")
    ],
    targets: [
        .executableTarget(
            name: "Veya",
            dependencies: [
                .product(name: "GRDB", package: "GRDB.swift")
            ],
            path: "Sources/Veya"
        ),
        .testTarget(
            name: "VeyaTests",
            dependencies: ["Veya"],
            path: "Tests/VeyaTests",
            swiftSettings: [
                // Xcode.app isn't installed on this machine (CLT only), so
                // swift-testing's framework lives under the CLT toolchain
                // rather than a system search path `swift test` finds by
                // default — point it there explicitly. Harmless once a
                // full Xcode.app provides it on the default search path.
                .unsafeFlags([
                    "-F", "/Library/Developer/CommandLineTools/Library/Developer/Frameworks",
                    // This CLT install's _Testing_Foundation cross-import
                    // overlay (Testing <-> Foundation) ships without its
                    // .swiftmodule, so importing both Testing and
                    // Foundation in one file fails to resolve it. Disable
                    // cross-import overlay auto-loading — the tests don't
                    // need its extra Foundation-specific matchers.
                    "-Xfrontend", "-disable-cross-import-overlays"
                ])
            ],
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-F", "-Xlinker", "/Library/Developer/CommandLineTools/Library/Developer/Frameworks",
                    "-Xlinker", "-rpath", "-Xlinker", "/Library/Developer/CommandLineTools/Library/Developer/Frameworks"
                ])
            ]
        )
    ]
)
