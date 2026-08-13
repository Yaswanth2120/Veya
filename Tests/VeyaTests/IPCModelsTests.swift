import Foundation
import Testing
@testable import Veya

@Suite("IPC models — Codable and snake_case")
struct IPCModelsTests {
    @Test("outgoing request encodes with snake_case keys and version/type/method")
    func outgoingRequestEncodesSnakeCase() throws {
        let request = IPCOutgoingRequest(
            id: "req-1",
            method: "session.start",
            params: SessionStartParams(
                sessionId: "sess-1",
                title: "Migration Recap",
                sessionType: "meeting",
                company: "Acme Corp",
                roleOrTopic: "Staff Engineer",
                sessionDescription: "Q3 recap",
                notes: "backend audience",
                preferredAnswerStyle: "concise",
                preferredProgrammingLanguage: "Swift",
                customInstructions: "keep it short"
            )
        )
        let data = try IPCCoding.encoder.encode(request)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]

        #expect(json?["version"] as? Int == 1)
        #expect(json?["id"] as? String == "req-1")
        #expect(json?["type"] as? String == "request")
        #expect(json?["method"] as? String == "session.start")

        let params = json?["params"] as? [String: Any]
        #expect(params?["session_id"] as? String == "sess-1")
        #expect(params?["session_type"] as? String == "meeting")
        #expect(params?["company"] as? String == "Acme Corp")
        #expect(params?["role_or_topic"] as? String == "Staff Engineer")
        #expect(params?["session_description"] as? String == "Q3 recap")
        #expect(params?["preferred_programming_language"] as? String == "Swift")
    }

    @Test("empty params encode as an empty object")
    func emptyParamsEncodeAsEmptyObject() throws {
        let request = IPCOutgoingRequest(id: "req-1", method: "system.ping", params: EmptyIPCParams())
        let data = try IPCCoding.encoder.encode(request)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let params = json?["params"] as? [String: Any]
        #expect(params?.isEmpty == true)
    }

    @Test("incoming response envelope decodes from snake_case")
    func incomingResponseEnvelopeDecodes() throws {
        let line = #"{"version":1,"id":"req-1","type":"response","result":{"pong":true}}"#
        let envelope = try IPCCoding.decoder.decode(IPCIncomingEnvelope.self, from: Data(line.utf8))
        #expect(envelope.version == 1)
        #expect(envelope.id == "req-1")
        #expect(envelope.type == "response")
        let pong: PingResult = try #require(envelope.result).decoded()
        #expect(pong.pong == true)
    }

    @Test("incoming error envelope decodes with code and message")
    func incomingErrorEnvelopeDecodes() throws {
        let line = #"{"version":1,"id":"req-1","type":"error","error":{"code":"INVALID_REQUEST","message":"bad"}}"#
        let envelope = try IPCCoding.decoder.decode(IPCIncomingEnvelope.self, from: Data(line.utf8))
        #expect(envelope.error?.code == "INVALID_REQUEST")
        #expect(envelope.error?.message == "bad")
    }

    @Test("incoming event envelope decodes typed data with snake_case fields")
    func incomingEventEnvelopeDecodesTypedData() throws {
        let line = #"""
        {"version":1,"type":"event","event":"transcript.final","data":{"session_id":"s1","id":"seg-1","text":"hello","started_at":0.0,"ended_at":1.5,"is_final":true}}
        """#
        let envelope = try IPCCoding.decoder.decode(IPCIncomingEnvelope.self, from: Data(line.utf8))
        #expect(envelope.event == "transcript.final")

        let data: TranscriptFinalEventData = try #require(envelope.data).decoded()
        #expect(data.sessionId == "s1")
        #expect(data.id == "seg-1")
        #expect(data.text == "hello")
        #expect(data.startedAt == 0.0)
        #expect(data.endedAt == 1.5)
        #expect(data.isFinal == true)
    }

    @Test("answer.completed event data decodes talking_points and structured sources")
    func answerCompletedEventDataDecodes() throws {
        let line = #"""
        {"version":1,"type":"event","event":"answer.completed","data":{"session_id":"s1","question_id":"q1","question":"why?","talking_points":["a","b"],"sources":[{"document_id":"doc1","file_name":"Notes.pdf","chunk_id":"chunk1","excerpt":"a short excerpt"}],"sequence":1,"caveat":""}}
        """#
        let envelope = try IPCCoding.decoder.decode(IPCIncomingEnvelope.self, from: Data(line.utf8))
        let data: AnswerCompletedEventData = try #require(envelope.data).decoded()
        #expect(data.questionId == "q1")
        #expect(data.talkingPoints == ["a", "b"])
        #expect(data.sources == [AnswerSourceEventData(documentId: "doc1", fileName: "Notes.pdf", chunkId: "chunk1", excerpt: "a short excerpt")])
    }

    @Test("IPCJSONValue round-trips through encode/decode")
    func jsonValueRoundTrips() throws {
        let value = IPCJSONValue.object([
            "a": .string("x"),
            "b": .number(2),
            "c": .array([.bool(true), .null]),
        ])
        let data = try IPCCoding.encoder.encode(value)
        let decoded = try IPCCoding.decoder.decode(IPCJSONValue.self, from: data)
        #expect(decoded == value)
    }

    @Test("worker.ready event data decodes protocol_version and worker_version")
    func workerReadyEventDataDecodes() throws {
        let line = #"{"version":1,"type":"event","event":"worker.ready","data":{"protocol_version":1,"worker_version":"0.1.0"}}"#
        let envelope = try IPCCoding.decoder.decode(IPCIncomingEnvelope.self, from: Data(line.utf8))
        let data: WorkerReadyEventData = try #require(envelope.data).decoded()
        #expect(data.protocolVersion == 1)
        #expect(data.workerVersion == "0.1.0")
    }

    @Test("transcription.start params encode with snake_case keys")
    func transcriptionStartParamsEncodeSnakeCase() throws {
        let request = IPCOutgoingRequest(
            id: "req-1",
            method: "transcription.start",
            params: TranscriptionStartParams(sessionId: "sess-1", sampleRateHz: 16000, channels: 1, encoding: "pcm_s16le")
        )
        let data = try IPCCoding.encoder.encode(request)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let params = json?["params"] as? [String: Any]

        #expect(params?["session_id"] as? String == "sess-1")
        #expect(params?["sample_rate_hz"] as? Int == 16000)
        #expect(params?["channels"] as? Int == 1)
        #expect(params?["encoding"] as? String == "pcm_s16le")
    }

    @Test("transcription.audio_chunk params encode sequence, timing, and base64 audio with snake_case keys")
    func audioChunkParamsEncodeSnakeCase() throws {
        let request = IPCOutgoingRequest(
            id: "req-1",
            method: "transcription.audio_chunk",
            params: AudioChunkParams(
                sessionId: "sess-1",
                sequence: 42,
                startedAtSeconds: 12.4,
                durationSeconds: 0.5,
                audioBase64: Data([1, 2, 3]).base64EncodedString()
            )
        )
        let data = try IPCCoding.encoder.encode(request)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let params = json?["params"] as? [String: Any]

        #expect(params?["session_id"] as? String == "sess-1")
        #expect(params?["sequence"] as? Int == 42)
        #expect(params?["started_at_seconds"] as? Double == 12.4)
        #expect(params?["duration_seconds"] as? Double == 0.5)
        #expect(params?["audio_base64"] as? String == Data([1, 2, 3]).base64EncodedString())
    }

    @Test("a chunk at exactly AudioIPCLimits.maxChunkBytes is not oversized")
    func maxChunkBytesBoundaryIsExactlyAtTheLimit() {
        let chunk = makeTestAudioChunk(byteCount: AudioIPCLimits.maxChunkBytes)
        #expect(chunk.pcm.count == AudioIPCLimits.maxChunkBytes)
        #expect(chunk.pcm.count <= AudioIPCLimits.maxChunkBytes)
    }

    @Test("knowledge.ingest params encode with snake_case keys and never include document contents")
    func knowledgeIngestParamsEncodeSnakeCase() throws {
        let request = IPCOutgoingRequest(
            id: "req-1",
            method: "knowledge.ingest",
            params: KnowledgeIngestParams(
                sessionId: "sess-1",
                documentId: "doc-1",
                fileName: "architecture.pdf",
                fileExtension: "pdf",
                filePath: "/local/app-support/path/architecture.pdf"
            )
        )
        let data = try IPCCoding.encoder.encode(request)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let params = json?["params"] as? [String: Any]

        #expect(params?["session_id"] as? String == "sess-1")
        #expect(params?["document_id"] as? String == "doc-1")
        #expect(params?["file_name"] as? String == "architecture.pdf")
        #expect(params?["file_extension"] as? String == "pdf")
        #expect(params?["file_path"] as? String == "/local/app-support/path/architecture.pdf")
        // Only the path is sent — never file contents.
        #expect(params?.count == 5)
    }

    @Test("knowledge.status result decodes the status string")
    func knowledgeStatusResultDecodes() throws {
        let line = #"{"version":1,"id":"req-1","type":"response","result":{"status":"ready"}}"#
        let envelope = try IPCCoding.decoder.decode(IPCIncomingEnvelope.self, from: Data(line.utf8))
        let result: KnowledgeStatusResult = try #require(envelope.result).decoded()
        #expect(result.status == "ready")
    }

    @Test("knowledge.retrieve result decodes structured sources")
    func knowledgeRetrieveResultDecodes() throws {
        let line = #"""
        {"version":1,"id":"req-1","type":"response","result":{"sources":[{"document_id":"doc1","file_name":"notes.txt","chunk_id":"chunk1","excerpt":"an excerpt"}]}}
        """#
        let envelope = try IPCCoding.decoder.decode(IPCIncomingEnvelope.self, from: Data(line.utf8))
        let result: KnowledgeRetrieveResult = try #require(envelope.result).decoded()
        #expect(result.sources.count == 1)
        #expect(result.sources[0].fileName == "notes.txt")
    }

    @Test("knowledge.ingestion_started event data decodes")
    func knowledgeIngestionStartedEventDataDecodes() throws {
        let line = #"{"version":1,"type":"event","event":"knowledge.ingestion_started","data":{"session_id":"s1","document_id":"doc1","file_name":"notes.txt"}}"#
        let envelope = try IPCCoding.decoder.decode(IPCIncomingEnvelope.self, from: Data(line.utf8))
        let data: KnowledgeIngestionStartedEventData = try #require(envelope.data).decoded()
        #expect(data.documentId == "doc1")
        #expect(data.fileName == "notes.txt")
    }

    @Test("knowledge.ingestion_completed event data decodes chunk_count")
    func knowledgeIngestionCompletedEventDataDecodes() throws {
        let line = #"{"version":1,"type":"event","event":"knowledge.ingestion_completed","data":{"session_id":"s1","document_id":"doc1","file_name":"notes.txt","chunk_count":3}}"#
        let envelope = try IPCCoding.decoder.decode(IPCIncomingEnvelope.self, from: Data(line.utf8))
        let data: KnowledgeIngestionCompletedEventData = try #require(envelope.data).decoded()
        #expect(data.chunkCount == 3)
    }

    @Test("knowledge.ingestion_failed event data decodes status and reason")
    func knowledgeIngestionFailedEventDataDecodes() throws {
        let line = #"{"version":1,"type":"event","event":"knowledge.ingestion_failed","data":{"session_id":"s1","document_id":"doc1","file_name":"notes.exe","status":"unsupported","reason":"'.exe' is not a supported document type."}}"#
        let envelope = try IPCCoding.decoder.decode(IPCIncomingEnvelope.self, from: Data(line.utf8))
        let data: KnowledgeIngestionFailedEventData = try #require(envelope.data).decoded()
        #expect(data.status == "unsupported")
        #expect(data.reason == "'.exe' is not a supported document type.")
    }
}
