import Foundation

/// The versioned JSON Lines wire protocol shared with `core/veya/ipc/protocol.py`.
/// See `docs/IPC_PROTOCOL.md` for the full schema.
enum IPCProtocolVersion {
    static let current = 1
}

// MARK: - Generic JSON value

/// A minimal, self-contained JSON value type used for `params`/`result`/
/// `data` fields whose concrete shape depends on the method/event at the
/// call site. Callers decode/encode a specific typed model through this
/// (`decoded(as:)` / `IPCJSONValue.from(_:)`) rather than working with the
/// untyped value directly — this is what keeps the RPC/event surface
/// "typed Codable models on each side" while still allowing one generic
/// envelope type to carry any of them.
enum IPCJSONValue: Codable, Equatable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([IPCJSONValue])
    case object([String: IPCJSONValue])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([IPCJSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: IPCJSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported JSON value in IPC message."
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }

    func decoded<T: Decodable>(as type: T.Type = T.self) throws -> T {
        let data = try IPCCoding.encoder.encode(self)
        return try IPCCoding.decoder.decode(T.self, from: data)
    }

    static func from(_ value: some Encodable) throws -> IPCJSONValue {
        let data = try IPCCoding.encoder.encode(value)
        return try IPCCoding.decoder.decode(IPCJSONValue.self, from: data)
    }
}

/// Shared coder configuration: snake_case on the wire, camelCase in Swift.
/// DTO property names deliberately spell out acronyms as `Id`/`Url`
/// (never `ID`/`URL`) — Foundation's snake_case converter treats each
/// capital as a new word, so `sessionID` would round-trip as
/// `session_i_d`, not `session_id`.
enum IPCCoding {
    static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return encoder
    }()

    static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()
}

// MARK: - Envelopes

struct EmptyIPCParams: Encodable, Sendable {}

/// What Swift sends. Params type is generic per call site.
struct IPCOutgoingRequest<Params: Encodable & Sendable>: Encodable, Sendable {
    let version: Int
    let id: String
    let type: String
    let method: String
    let params: Params

    init(id: String, method: String, params: Params) {
        self.version = IPCProtocolVersion.current
        self.id = id
        self.type = "request"
        self.method = method
        self.params = params
    }
}

struct IPCErrorPayload: Codable, Equatable, Sendable {
    let code: String
    let message: String
}

/// One line of incoming stdout, decoded just far enough to route it —
/// `result`/`data` stay as `IPCJSONValue` until the caller (a pending
/// request's continuation, or the event router) knows what type to
/// decode them into.
struct IPCIncomingEnvelope: Decodable {
    let version: Int
    let id: String?
    let type: String
    let event: String?
    let result: IPCJSONValue?
    let error: IPCErrorPayload?
    let data: IPCJSONValue?
}

/// A decoded worker event, still carrying its `data` untyped — routed by
/// `IPCEventRouter`, which knows which typed payload each `name` decodes to.
struct IPCEvent: Sendable {
    let name: String
    let data: IPCJSONValue
}

// MARK: - RPC method params/results

struct SessionIdentifierParams: Encodable, Sendable {
    let sessionId: String
}

/// `session.start`'s params. Swift remains the owner of full `Session`
/// persistence — these fields are sent read-only, once, so Python can
/// assemble an answer-generation prompt (see
/// `docs/QUESTION_AND_ANSWER_INTELLIGENCE.md`) without Swift resending
/// anything per-question. Every field beyond `sessionId`/`title`/
/// `sessionType` is optional/blank-safe server-side.
struct SessionStartParams: Encodable, Sendable {
    let sessionId: String
    let title: String
    let sessionType: String
    let company: String
    let roleOrTopic: String
    let sessionDescription: String
    let notes: String
    let preferredAnswerStyle: String
    let preferredProgrammingLanguage: String
    let customInstructions: String
}

/// `transcription.start`'s params — mirrors `core/veya/ipc/dispatcher.py`'s
/// `_handle_transcription_start` validation exactly (mono, `pcm_s16le`
/// only). See `docs/REALTIME_TRANSCRIPTION.md`.
struct TranscriptionStartParams: Encodable, Sendable {
    let sessionId: String
    let sampleRateHz: Int
    let channels: Int
    let encoding: String
}

/// `transcription.audio_chunk`'s params. `audioBase64` is capped
/// client-side at `AudioIPCLimits.maxChunkBytes` *before* encoding (see
/// `AudioChunkSender`) — the Python worker enforces the same cap
/// independently server-side.
struct AudioChunkParams: Encodable, Sendable {
    let sessionId: String
    let sequence: Int
    let startedAtSeconds: Double
    let durationSeconds: Double
    let audioBase64: String
}

/// `knowledge.ingest`'s params. `filePath` must already be the
/// app-managed copied-document path (`SessionDocument.storedPath`) —
/// Python validates it resolves beneath `VEYA_DOCUMENTS_DIRECTORY` before
/// ever reading it. Whole document contents are never sent over IPC.
struct KnowledgeIngestParams: Encodable, Sendable {
    let sessionId: String
    let documentId: String
    let fileName: String
    let fileExtension: String
    let filePath: String
}

/// `knowledge.remove`/`knowledge.status`'s params.
struct DocumentIdentifierParams: Encodable, Sendable {
    let documentId: String
}

/// `knowledge.retrieve`'s params — mainly useful for diagnostics/manual
/// testing; the production grounded-answer path retrieves internally in
/// Python during answer generation, not via a Swift-initiated round trip.
struct KnowledgeRetrieveParams: Encodable, Sendable {
    let sessionId: String
    let query: String
}

struct KnowledgeStatusResult: Decodable, Sendable {
    let status: String
}

struct KnowledgeRetrieveResult: Decodable, Sendable {
    let sources: [AnswerSourceEventData]
}

// MARK: - Coding / system-design workbench (Sections 11–12)

struct CodingFileParams: Encodable, Sendable { let sessionId: String; let name: String }
struct CodingRenameFileParams: Encodable, Sendable { let sessionId: String; let name: String; let newName: String }
struct CodingUpsertFileParams: Encodable, Sendable { let sessionId: String; let name: String; let language: String; let content: String; let baseVersion: Int? }
struct CodingEdit: Codable, Equatable, Sendable { let start: Int; let end: Int; let replacement: String }
struct CodingApplyEditsParams: Encodable, Sendable { let sessionId: String; let name: String; let baseVersion: Int; let edits: [CodingEdit] }
struct CodeFileResult: Codable, Equatable, Sendable { let name: String; let language: String; let content: String; let version: Int }
struct CodingFilesResult: Decodable, Sendable { let files: [CodeFileResult] }
struct CodingAnalysisResult: Decodable, Sendable { let syntaxOk: Bool; let diagnostics: [CodingDiagnostic]; let complexity: Int; let functionCount: Int?; let unsupportedLanguage: Bool? }
struct CodingDiagnostic: Decodable, Sendable { let line: Int; let message: String }
struct CodingRunResult: Decodable, Sendable { let exitCode: Int; let timedOut: Bool; let stdout: String; let stderr: String }

struct ArchitectureNode: Codable, Identifiable, Equatable, Sendable { let id: String; var label: String; var kind: String; var x: Double = 0; var y: Double = 0 }
struct ArchitectureEdge: Codable, Equatable, Sendable { var source: String; var target: String; var label: String }
struct ArchitectureStateResult: Codable, Sendable {
    let version: Int
    var title: String
    var nodes: [ArchitectureNode]
    var edges: [ArchitectureEdge]
    var decisions: [String]
    var assumptions: [String] = []
    var requirements: [String] = []
    var risks: [String] = []
    var tradeOffs: [String] = []
    var actionItems: [String] = []
    let mermaid: String
}
struct ArchitectureReplaceParams: Encodable, Sendable {
    let sessionId: String
    let baseVersion: Int?
    let title: String
    let nodes: [ArchitectureNode]
    let edges: [ArchitectureEdge]
    let decisions: [String]
    let assumptions: [String]
    let requirements: [String]
    let risks: [String]
    let tradeOffs: [String]
    let actionItems: [String]
}
struct ArchitectureFollowupParams: Encodable, Sendable { let sessionId: String; let request: String }
struct ArchitectureExportParams: Encodable, Sendable { let sessionId: String; let format: String }
struct ArchitectureExportResult: Decodable, Sendable { let format: String; let content: String?; let contentBase64: String? }

// MARK: - Coding assist proposals (followup/debug/generate_tests/explain)

struct CodingAssistParams: Encodable, Sendable { let sessionId: String; let name: String; let request: String }
struct CodingProposalResult: Decodable, Sendable {
    let baseVersion: Int
    let explanation: String
    let edits: [CodingEdit]
    let tests: String
    let complexity: String
}

// MARK: - Session reports (Section 13)

struct TranscriptSegmentPayload: Encodable, Sendable { let text: String; let startedAt: Double; let endedAt: Double?; let isFinal: Bool }
struct DetectedQuestionPayload: Encodable, Sendable { let id: String; let text: String; let detectedAt: Double }
struct AnswerPayload: Encodable, Sendable { let questionId: String; let question: String; let talkingPoints: [String]; let sources: [AnswerSourceEventData] }
struct SessionAnalyzeParams: Encodable, Sendable {
    let sessionId: String
    let transcript: [TranscriptSegmentPayload]
    let questions: [DetectedQuestionPayload]
    let answers: [AnswerPayload]
}
struct SessionReportResult: Codable, Equatable, Sendable {
    let sessionId: String
    let summary: String
    let topics: [String]
    let questions: [String]
    let generatedAnswers: [ReportAnswer]
    let sources: [AnswerSourceEventData]
    let decisions: [String]
    let actionItems: [String]
    let unansweredQuestions: [String]
    let preparationGaps: [String]
    let memoryCandidateIds: [String]
}
struct ReportAnswer: Codable, Equatable, Sendable { let question: String; let talkingPoints: [String] }

// MARK: - Durable memory (Section 13)

struct MemoryRecordResult: Codable, Identifiable, Equatable, Sendable {
    let id: String
    let sessionId: String
    let text: String
    let status: String
    let createdAt: Double
    let updatedAt: Double
}
struct MemoryListResult: Decodable, Sendable { let memories: [MemoryRecordResult] }
struct MemoryListParams: Encodable, Sendable { let status: String? }
struct MemoryIdentifierParams: Encodable, Sendable { let memoryId: String }
struct MemoryUpdateParams: Encodable, Sendable { let memoryId: String; let text: String }

struct OkResult: Decodable, Sendable {
    let ok: Bool
}

/// `transcription.start`'s result. `answerIntelligenceAvailable` reports
/// whether Python's local Ollama check succeeded for *this* session —
/// false never fails the call itself (real transcription must not fall
/// back to the mock feed just because answer intelligence is
/// unavailable). See `docs/QUESTION_AND_ANSWER_INTELLIGENCE.md`.
struct TranscriptionStartResult: Decodable, Sendable {
    let ok: Bool
    let answerIntelligenceAvailable: Bool
}

struct PingResult: Decodable, Sendable {
    let pong: Bool
}

struct SystemInfoResult: Decodable, Sendable {
    let protocolVersion: Int
    let workerVersion: String
    let pid: Int
}

/// `system.llm_status`'s result — a diagnostic for the Local AI status
/// panel (Settings), never a gate on whether a real RPC is attempted.
/// `error` is a short, typed reason string, never a raw exception message.
struct LLMStatusResult: Decodable, Sendable {
    let reachable: Bool
    let baseUrl: String
    let configuredModel: String
    let modelInstalled: Bool
    let availableModels: [String]
    let error: String
}

// MARK: - Event payloads (mirror core/veya/ipc/events.py field-for-field)

// MARK: - Turn detection (Section 14)

struct TurnStateEventData: Decodable, Sendable {
    let sessionId: String
    let state: String
}

struct TurnDebugEventData: Decodable, Sendable {
    let sessionId: String
    let rms: Double
    let threshold: Double
    let isInSpeech: Bool
    let speechSeconds: Double
    let silenceSeconds: Double
}

struct QuestionClassifyingEventData: Decodable, Sendable {
    let sessionId: String
}

struct QuestionRejectedEventData: Decodable, Sendable {
    let sessionId: String
}

struct WorkerReadyEventData: Decodable, Sendable {
    let protocolVersion: Int
    let workerVersion: String
}

struct SessionStartedEventData: Decodable, Sendable {
    let sessionId: String
}

struct SessionEndedEventData: Decodable, Sendable {
    let sessionId: String
}

struct TranscriptPartialEventData: Decodable, Sendable {
    let sessionId: String
    let text: String
}

struct TranscriptFinalEventData: Decodable, Sendable {
    let sessionId: String
    let id: String
    let text: String
    let startedAt: Double
    let endedAt: Double?
    let isFinal: Bool
}

struct QuestionDetectedEventData: Decodable, Sendable {
    let sessionId: String
    let questionId: String
    let text: String
    let confidence: Double
    let detectedAt: Double
}

/// `sequence` is a per-session, per-answer-round counter (Python:
/// `ConversationOrchestrator`) — `IPCEventRouter` uses it to drop
/// stale/superseded answer events (see `docs/QUESTION_AND_ANSWER_INTELLIGENCE.md`).
struct AnswerStartedEventData: Decodable, Sendable {
    let sessionId: String
    let questionId: String
    let sequence: Int
}

struct AnswerDeltaEventData: Decodable, Sendable {
    let sessionId: String
    let questionId: String
    let delta: String
    let sequence: Int
}

/// A structured source reference (Section 9) — always corresponds to a
/// chunk Python actually retrieved for this answer, never invented. See
/// `docs/KNOWLEDGE_RETRIEVAL.md`.
struct AnswerSourceEventData: Codable, Equatable, Sendable {
    let documentId: String
    let fileName: String
    let chunkId: String
    let excerpt: String
}

struct AnswerCompletedEventData: Decodable, Sendable {
    let sessionId: String
    let questionId: String
    let question: String
    let talkingPoints: [String]
    let sources: [AnswerSourceEventData]
    let sequence: Int
    /// Optional caveat/clarifying assumption — folded into the persisted
    /// `CopilotAnswer.talkingPoints` as a final entry by `IPCEventRouter`
    /// rather than requiring a `CopilotAnswer` schema/migration change.
    let caveat: String
}

// MARK: - Knowledge ingestion events (Section 9)

struct KnowledgeIngestionStartedEventData: Decodable, Sendable {
    let sessionId: String
    let documentId: String
    let fileName: String
}

struct KnowledgeIngestionProgressEventData: Decodable, Sendable {
    let sessionId: String
    let documentId: String
    let stage: String
    let chunkCount: Int
}

struct KnowledgeIngestionCompletedEventData: Decodable, Sendable {
    let sessionId: String
    let documentId: String
    let fileName: String
    let chunkCount: Int
}

struct KnowledgeIngestionFailedEventData: Decodable, Sendable {
    let sessionId: String
    let documentId: String
    let fileName: String
    let status: String
    let reason: String
}

// MARK: - Errors

enum IPCClientError: LocalizedError, Equatable, Sendable {
    case timeout(method: String)
    case workerUnavailable
    case protocolError(code: String, message: String)
    case malformedLine(String)
    case decodingFailed(String)
    case cancelled

    var errorDescription: String? {
        switch self {
        case .timeout(let method):
            return "Timed out waiting for a response to '\(method)'."
        case .workerUnavailable:
            return "The Python worker is not available."
        case .protocolError(let code, let message):
            return "\(code): \(message)"
        case .malformedLine(let reason):
            return "Received malformed worker output: \(reason)"
        case .decodingFailed(let reason):
            return "Failed to decode worker message: \(reason)"
        case .cancelled:
            return "The request was cancelled."
        }
    }
}
