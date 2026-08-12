import Foundation

struct OverlayPreferences: Codable, Equatable {
    var opacity: Double
    var alwaysOnTop: Bool
    var compactMode: Bool

    static let `default` = OverlayPreferences(opacity: 0.92, alwaysOnTop: true, compactMode: false)
}
