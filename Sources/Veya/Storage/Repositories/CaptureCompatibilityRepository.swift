import Foundation
import GRDB

/// Persists a limited local history of capture-compatibility test results.
/// No telemetry upload — this is purely for the user's own "Compatibility
/// History" view.
final class CaptureCompatibilityRepository: Sendable {
    /// Keep the history small and local-only, per the build prompt.
    static let historyLimit = 20

    private let db: DatabaseManager

    init(db: DatabaseManager = .shared) {
        self.db = db
    }

    func save(_ record: CaptureCompatibilityRecord) async throws {
        try await db.dbWriter.write { db in
            try record.insert(db)
            let excessCount = try CaptureCompatibilityRecord
                .order(Column("testedAt").desc)
                .fetchCount(db) - Self.historyLimit
            guard excessCount > 0 else { return }
            let staleIDs = try CaptureCompatibilityRecord
                .order(Column("testedAt").desc)
                .limit(excessCount, offset: Self.historyLimit)
                .fetchAll(db)
                .map(\.id)
            _ = try CaptureCompatibilityRecord.deleteAll(db, keys: staleIDs)
        }
    }

    func fetchAll() async throws -> [CaptureCompatibilityRecord] {
        try await db.dbWriter.read { db in
            try CaptureCompatibilityRecord
                .order(Column("testedAt").desc)
                .fetchAll(db)
        }
    }
}
