import Foundation

enum AudioIPCLimits {
    /// Raw PCM bytes per chunk, before base64 encoding — must match
    /// `MAX_AUDIO_CHUNK_BYTES` in `core/veya/ipc/dispatcher.py`. A real
    /// chunk (`MicrophoneAudioCapture`'s default: 0.5s @ 16kHz mono 16-bit)
    /// is ~16,000 bytes, comfortably under this cap.
    static let maxChunkBytes = 65536
}

/// Sends captured audio chunks to the Python worker without ever letting a
/// slow/backed-up worker block real-time capture. Chunks are handed off
/// with a bounded number of concurrent in-flight `transcription.audio_chunk`
/// RPCs (`maxInFlight`) rather than one-at-a-time synchronous waits — once
/// that bound is reached, new chunks are dropped and counted (never
/// queued unboundedly) until an in-flight request completes.
///
/// The actual RPC call is injected as a plain closure (`sendChunk`) rather
/// than depending on `PythonWorkerManager` directly — same reasoning as
/// `IPCClient`/`IPCTransport`: it lets tests exercise the bounded-queue and
/// drop-counting behavior deterministically with a closure they control
/// (e.g. one that blocks until signaled) instead of a real subprocess.
actor AudioChunkSender {
    typealias Sender = @Sendable (AudioChunkParams) async throws -> Void

    private let sessionId: String
    private let maxInFlight: Int
    private let sendChunk: Sender

    private(set) var inFlightCount = 0
    private(set) var droppedChunkCount = 0
    private(set) var failedChunkCount = 0
    private(set) var succeededChunkCount = 0

    init(sessionId: String, maxInFlight: Int = 2, sendChunk: @escaping Sender) {
        self.sessionId = sessionId
        self.maxInFlight = maxInFlight
        self.sendChunk = sendChunk
    }

    init(workerManager: PythonWorkerManager, sessionId: String, maxInFlight: Int = 2) {
        self.sessionId = sessionId
        self.maxInFlight = maxInFlight
        self.sendChunk = { params in
            let _: OkResult = try await workerManager.call(method: "transcription.audio_chunk", params: params)
        }
    }

    /// Fire-and-forget from the caller's perspective: returns immediately
    /// after either scheduling the send or dropping the chunk, never
    /// awaiting the RPC's response itself.
    func send(_ chunk: AudioChunk) {
        guard chunk.pcm.count <= AudioIPCLimits.maxChunkBytes else {
            droppedChunkCount += 1
            BridgeLog.error("dropping oversized audio chunk, bytes=\(chunk.pcm.count)")
            return
        }
        guard inFlightCount < maxInFlight else {
            droppedChunkCount += 1
            return
        }

        inFlightCount += 1
        let params = AudioChunkParams(
            sessionId: sessionId,
            sequence: chunk.sequence,
            startedAtSeconds: chunk.startedAt,
            durationSeconds: chunk.duration,
            audioBase64: chunk.pcm.base64EncodedString()
        )
        let sendChunk = self.sendChunk
        Task {
            do {
                try await sendChunk(params)
                self.finishInFlight(failed: false)
            } catch {
                self.finishInFlight(failed: true)
            }
        }
    }

    /// Safe metadata only (counts, never audio/transcript content) — for
    /// developer diagnostics display.
    func counts() -> (sent: Int, dropped: Int, failed: Int) {
        (succeededChunkCount, droppedChunkCount, failedChunkCount)
    }

    private func finishInFlight(failed: Bool) {
        inFlightCount -= 1
        if failed {
            failedChunkCount += 1
        } else {
            succeededChunkCount += 1
        }
    }
}
