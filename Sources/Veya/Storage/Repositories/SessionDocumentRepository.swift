import Foundation
import GRDB

final class SessionDocumentRepository: Sendable {
    private let db: DatabaseManager

    init(db: DatabaseManager = .shared) {
        self.db = db
    }

    func create(_ document: SessionDocument) async throws {
        try await db.dbWriter.write { db in
            try document.insert(db)
        }
    }

    func fetchAll(sessionID: UUID) async throws -> [SessionDocument] {
        try await db.dbWriter.read { db in
            try SessionDocument
                .filter(Column("sessionID") == sessionID)
                .order(Column("addedAt"))
                .fetchAll(db)
        }
    }

    func delete(id: UUID) async throws {
        _ = try await db.dbWriter.write { db in
            try SessionDocument.deleteOne(db, key: id)
        }
    }
}
