import Foundation
import GRDB

/// Veya is single-user; there is exactly one `UserProfile` row, upserted in
/// place.
final class UserProfileRepository: Sendable {
    private let db: DatabaseManager

    init(db: DatabaseManager = .shared) {
        self.db = db
    }

    func fetch() async throws -> UserProfile? {
        try await db.dbWriter.read { db in
            try UserProfile.fetchOne(db)
        }
    }

    func save(_ profile: UserProfile) async throws {
        try await db.dbWriter.write { db in
            try profile.save(db)
        }
    }
}
