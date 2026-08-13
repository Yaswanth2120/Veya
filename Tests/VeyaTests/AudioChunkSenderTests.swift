import Foundation
import Testing
@testable import Veya

/// A controllable gate so tests can hold an `AudioChunkSender.send` call
/// "in flight" deterministically, rather than racing against how fast a
/// real (or even a fake) RPC completes.
actor SendGate {
    private var waiters: [CheckedContinuation<Void, Never>] = []
    private var isOpen = false

    func waitUntilOpened() async {
        if isOpen { return }
        await withCheckedContinuation { waiters.append($0) }
    }

    func open() {
        isOpen = true
        let pending = waiters
        waiters = []
        for continuation in pending {
            continuation.resume()
        }
    }
}

@Suite("AudioChunkSender")
struct AudioChunkSenderTests {
    @Test("an oversized chunk is dropped before any send is attempted")
    func oversizedChunkIsDropped() async {
        let attemptedSends = CountBox()
        let sender = AudioChunkSender(sessionId: "s1", maxInFlight: 2) { _ in
            await attemptedSends.increment()
        }

        let oversized = makeTestAudioChunk(sequence: 0, byteCount: AudioIPCLimits.maxChunkBytes + 1)
        await sender.send(oversized)

        #expect(await sender.droppedChunkCount == 1)
        #expect(await attemptedSends.value == 0)
    }

    @Test("a chunk at exactly the maximum size is accepted")
    func chunkAtExactMaximumIsAccepted() async {
        let sender = AudioChunkSender(sessionId: "s1", maxInFlight: 2) { _ in }
        let chunk = makeTestAudioChunk(sequence: 0, byteCount: AudioIPCLimits.maxChunkBytes)

        await sender.send(chunk)
        try? await Task.sleep(nanoseconds: 20_000_000)

        #expect(await sender.droppedChunkCount == 0)
    }

    @Test("sends beyond maxInFlight are dropped and counted until an in-flight send completes")
    func boundedInFlightDropsExcessSends() async {
        let gate = SendGate()
        let sender = AudioChunkSender(sessionId: "s1", maxInFlight: 2) { _ in
            await gate.waitUntilOpened()
        }

        // First two sends occupy both in-flight slots (the gate keeps
        // their underlying "RPC" from ever completing until opened).
        await sender.send(makeTestAudioChunk(sequence: 0))
        await sender.send(makeTestAudioChunk(sequence: 1))
        #expect(await sender.inFlightCount == 2)

        // A third and fourth send now exceed the bound and must be dropped
        // rather than queued.
        await sender.send(makeTestAudioChunk(sequence: 2))
        await sender.send(makeTestAudioChunk(sequence: 3))

        #expect(await sender.droppedChunkCount == 2)
        #expect(await sender.inFlightCount == 2)

        await gate.open()
        try? await waitUntil { await sender.inFlightCount == 0 }

        #expect(await sender.succeededChunkCount == 2)
    }

    @Test("a failed send is counted separately from a dropped one")
    func failedSendIsCountedSeparately() async {
        struct SendFailure: Error {}
        let sender = AudioChunkSender(sessionId: "s1", maxInFlight: 2) { _ in
            throw SendFailure()
        }

        await sender.send(makeTestAudioChunk(sequence: 0))
        try? await waitUntil { await sender.failedChunkCount == 1 }

        #expect(await sender.failedChunkCount == 1)
        #expect(await sender.droppedChunkCount == 0)
        #expect(await sender.succeededChunkCount == 0)
    }

    @Test("the wire params carry the chunk's sequence, timing, and base64-encoded PCM")
    func wireParamsMatchChunkMetadata() async {
        let receivedParams = ReceivedParamsBox()
        let sender = AudioChunkSender(sessionId: "session-123", maxInFlight: 2) { params in
            await receivedParams.set(params)
        }

        let chunk = AudioChunk(sequence: 7, startedAt: 3.5, duration: 0.5, pcm: Data([1, 2, 3, 4]), sampleRate: 16000, channels: 1)
        await sender.send(chunk)
        try? await waitUntil { await receivedParams.value != nil }

        let params = await receivedParams.value
        #expect(params?.sessionId == "session-123")
        #expect(params?.sequence == 7)
        #expect(params?.startedAtSeconds == 3.5)
        #expect(params?.durationSeconds == 0.5)
        #expect(params?.audioBase64 == Data([1, 2, 3, 4]).base64EncodedString())
    }

    @Test("a stopped capture supplies a fresh stream to a sequential session")
    func fakeCaptureCreatesFreshStreamAfterStop() async throws {
        let capture = FakeAudioCapture()
        _ = capture.chunks()
        await capture.stop()

        let secondStream = capture.chunks()
        let received = ReceivedSequenceBox()
        let consumer = Task {
            for await chunk in secondStream {
                await received.set(chunk.sequence)
                break
            }
        }
        capture.simulateChunk(makeTestAudioChunk(sequence: 7))
        try await waitUntil { await received.value == 7 }
        consumer.cancel()
    }
}

private actor ReceivedParamsBox {
    private(set) var value: AudioChunkParams?
    func set(_ params: AudioChunkParams) { value = params }
}

private actor CountBox {
    private(set) var value = 0
    func increment() { value += 1 }
}

private actor ReceivedSequenceBox {
    private(set) var value: Int?
    func set(_ sequence: Int) { value = sequence }
}

private func waitUntil(timeout: TimeInterval = 2, _ condition: @escaping () async -> Bool) async throws {
    let deadline = Date().addingTimeInterval(timeout)
    while await !condition() {
        if Date() > deadline {
            throw WaitUntilTimeoutError()
        }
        try await Task.sleep(nanoseconds: 5_000_000)
    }
}
