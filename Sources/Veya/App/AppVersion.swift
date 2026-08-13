import Foundation

/// SPM executables don't have a real `.app` bundle Info.plist to read
/// `CFBundleShortVersionString` from, so Veya's version is a plain
/// constant. Bump it by hand alongside phase milestones.
enum AppVersion {
    static let current = "0.2.0"
}
