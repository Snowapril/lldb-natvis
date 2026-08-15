// swift-tools-version:5.5
// Xcode-debuggable wrapper around tests/test.cpp (copied to Sources/.../main.cpp).
// Open this directory in Xcode, set a breakpoint at the "BREAK HERE" line, run,
// and the Variables view shows natvis-formatted values (lldb-natvis must be
// imported from ~/.lldbinit and sample.natvis loaded).
import PackageDescription

let package = Package(
    name: "NatvisSample",
    targets: [
        .executableTarget(
            name: "NatvisSample",
            path: "Sources/NatvisSample"
        )
    ],
    cxxLanguageStandard: .cxx17
)
