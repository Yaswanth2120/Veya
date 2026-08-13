import Foundation
import GRDB

/// Owns the app's single SQLite database and its versioned migrations. All
/// repositories share one `DatabaseManager`.
///
/// Backed by `DatabasePool` (WAL mode) for the real on-disk database, and by
/// `DatabaseQueue` for unit tests — GRDB's `DatabasePool` cannot open
/// `:memory:` databases, since WAL mode requires a real file.
final class DatabaseManager: Sendable {
    let dbWriter: any DatabaseWriter

    /// Shared instance backed by a file under Application Support.
    static let shared: DatabaseManager = {
        do {
            return try DatabaseManager(url: DatabaseManager.defaultDatabaseURL())
        } catch {
            fatalError("Failed to open Veya database: \(error)")
        }
    }()

    /// In-memory instance for unit tests — same schema, no disk I/O.
    static func makeInMemory() -> DatabaseManager {
        do {
            let queue = try DatabaseQueue()
            let manager = DatabaseManager(writer: queue)
            try manager.migrate()
            return manager
        } catch {
            fatalError("Failed to open in-memory Veya database: \(error)")
        }
    }

    private init(url: URL) throws {
        self.dbWriter = try DatabasePool(path: url.path)
        try migrate()
    }

    private init(writer: any DatabaseWriter) {
        self.dbWriter = writer
    }

    private static func defaultDatabaseURL() throws -> URL {
        let appSupport = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let directory = appSupport.appendingPathComponent("Veya", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appendingPathComponent("veya.sqlite")
    }

    private func migrate() throws {
        var migrator = DatabaseMigrator()

        migrator.registerMigration("v1_createCoreTables") { db in
            try db.create(table: Session.databaseTableName) { t in
                t.column("id", .text).primaryKey()
                t.column("title", .text).notNull()
                t.column("company", .text).notNull()
                t.column("roleOrTopic", .text).notNull()
                t.column("sessionDescription", .text).notNull()
                t.column("expectedParticipants", .text).notNull()
                t.column("sessionType", .text).notNull()
                t.column("notes", .text).notNull()
                t.column("preferredAnswerStyle", .text).notNull()
                t.column("preferredProgrammingLanguage", .text).notNull()
                t.column("customInstructions", .text).notNull()
                t.column("status", .text).notNull()
                t.column("createdAt", .datetime).notNull()
                t.column("endedAt", .datetime)
            }

            try db.create(table: SessionDocument.databaseTableName) { t in
                t.column("id", .text).primaryKey()
                t.column("sessionID", .text).notNull()
                    .indexed()
                    .references(Session.databaseTableName, onDelete: .cascade)
                t.column("fileName", .text).notNull()
                t.column("fileExtension", .text).notNull()
                t.column("storedPath", .text).notNull()
                t.column("fileSizeBytes", .integer).notNull()
                t.column("addedAt", .datetime).notNull()
            }

            try db.create(table: TranscriptSegment.databaseTableName) { t in
                t.column("id", .text).primaryKey()
                t.column("sessionID", .text).notNull()
                    .indexed()
                    .references(Session.databaseTableName, onDelete: .cascade)
                t.column("text", .text).notNull()
                t.column("startedAt", .double).notNull()
                t.column("endedAt", .double)
                t.column("isFinal", .boolean).notNull()
            }

            try db.create(table: DetectedQuestion.databaseTableName) { t in
                t.column("id", .text).primaryKey()
                t.column("sessionID", .text).notNull()
                    .indexed()
                    .references(Session.databaseTableName, onDelete: .cascade)
                t.column("text", .text).notNull()
                t.column("detectedAt", .datetime).notNull()
            }

            try db.create(table: CopilotAnswer.databaseTableName) { t in
                t.column("id", .text).primaryKey()
                t.column("sessionID", .text).notNull()
                    .indexed()
                    .references(Session.databaseTableName, onDelete: .cascade)
                t.column("questionID", .text).notNull()
                    .references(DetectedQuestion.databaseTableName, onDelete: .cascade)
                t.column("question", .text).notNull()
                t.column("talkingPoints", .text).notNull()
                t.column("sources", .text).notNull()
                t.column("generatedAt", .datetime).notNull()
            }

            try db.create(table: UserProfile.databaseTableName) { t in
                t.column("id", .text).primaryKey()
                t.column("name", .text).notNull()
                t.column("headline", .text).notNull()
                t.column("background", .text).notNull()
                t.column("defaultAnswerStyle", .text).notNull()
                t.column("defaultProgrammingLanguage", .text).notNull()
                t.column("updatedAt", .datetime).notNull()
            }
        }

        migrator.registerMigration("v2_createCaptureCompatibilityRecord") { db in
            try db.create(table: CaptureCompatibilityRecord.databaseTableName) { t in
                t.column("id", .text).primaryKey()
                t.column("testedAt", .datetime).notNull()
                t.column("macOSVersion", .text).notNull()
                t.column("veyaVersion", .text).notNull()
                t.column("displayID", .integer).notNull()
                t.column("mode", .text).notNull()
                t.column("result", .text).notNull()
            }
        }

        migrator.registerMigration("v3_createSessionReport") { db in
            try db.create(table: SessionReport.databaseTableName) { t in
                t.column("id", .text).primaryKey()
                t.column("sessionID", .text).notNull()
                    .indexed()
                    .references(Session.databaseTableName, onDelete: .cascade)
                t.column("summary", .text).notNull()
                t.column("topics", .text).notNull()
                t.column("questions", .text).notNull()
                t.column("decisions", .text).notNull()
                t.column("actionItems", .text).notNull()
                t.column("unansweredQuestions", .text).notNull()
                t.column("preparationGaps", .text).notNull()
                t.column("generatedAt", .datetime).notNull()
            }
        }

        migrator.registerMigration("v4_addSessionReportAnswersAndSources") { db in
            // `generatedAnswers`/`sources` were part of Python's
            // `SessionReport` shape from the start (see
            // `core/veya/conversation/report.py`) but were dropped on the
            // way into GRDB — added as a separate migration (not folded
            // into v3) so an already-migrated local database still
            // upgrades cleanly instead of needing to be recreated.
            try db.alter(table: SessionReport.databaseTableName) { t in
                t.add(column: "generatedAnswers", .text).notNull().defaults(to: "[]")
                t.add(column: "sources", .text).notNull().defaults(to: "[]")
            }
        }

        migrator.registerMigration("v5_addSessionTypeSpecificFields") { db in
            try db.alter(table: Session.databaseTableName) { t in
                t.add(column: "codeExecutionConsent", .boolean).notNull().defaults(to: false)
                t.add(column: "expectedScale", .text).notNull().defaults(to: "")
            }
        }

        migrator.registerMigration("v6_addAnswerTextToGeneratedAnswer") { db in
            // Section 16: the model's natural, speakable answer sentence(s)
            // (Python's `ParsedAnswer.short_answer`) was being parsed out
            // but never actually sent over the wire — only `talkingPoints`
            // reached Swift, so the answer panel had no choice but to
            // render bullet points as the primary content. This column is
            // the durable home for that natural-language answer;
            // `talkingPoints` becomes optional supporting detail.
            try db.alter(table: CopilotAnswer.databaseTableName) { t in
                t.add(column: "answerText", .text).notNull().defaults(to: "")
            }
        }

        migrator.registerMigration("v7_addSpeakerRoleToTranscriptSegment") { db in
            // Section 16: dual-input interview audio — "interviewer"/
            // "user"/"unknown", driving the INTERVIEWER/YOU transcript
            // lanes. Existing rows (persisted before separated audio
            // tracks existed) default to "unknown", an honest label, not
            // a guess.
            try db.alter(table: TranscriptSegment.databaseTableName) { t in
                t.add(column: "speakerRole", .text).notNull().defaults(to: "unknown")
            }
        }

        try migrator.migrate(dbWriter)
    }
}
