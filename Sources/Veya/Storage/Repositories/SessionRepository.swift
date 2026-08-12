import Foundation
import GRDB

final class SessionRepository: Sendable {
    private let db: DatabaseManager

    init(db: DatabaseManager = .shared) {
        self.db = db
    }

    func create(_ session: Session) async throws {
        try await db.dbWriter.write { db in
            try session.insert(db)
        }
    }

    func update(_ session: Session) async throws {
        try await db.dbWriter.write { db in
            try session.update(db)
        }
    }

    func fetch(id: UUID) async throws -> Session? {
        try await db.dbWriter.read { db in
            try Session.fetchOne(db, key: id)
        }
    }

    func fetchAll() async throws -> [Session] {
        try await db.dbWriter.read { db in
            try Session
                .order(Column("createdAt").desc)
                .fetchAll(db)
        }
    }

    func delete(id: UUID) async throws {
        _ = try await db.dbWriter.write { db in
            try Session.deleteOne(db, key: id)
        }
    }
}
