import AVFoundation
import Foundation

/// Real microphone capture via `AVAudioEngine`. Converts whatever format
/// the input hardware provides into mono 16kHz signed 16-bit PCM
/// (`pcm_s16le` on the wire — the app targets Apple Silicon only, which is
/// little-endian, so `Int16`'s native byte layout already matches without
/// any extra byte-swapping), chunks it into `chunkDuration`-second pieces,
/// and yields them through a bounded `AsyncStream`. Once the bounded
/// buffer is full, the newest chunk is dropped and counted rather than
/// growing unbounded or blocking the real-time audio thread.
///
/// `AVAudioEngine`'s tap callback is documented to run serially on its own
/// dedicated thread, never concurrently with itself — the same reasoning
/// `FileHandleLineReading.swift`'s `LineBufferBox` already relies on for
/// its own `@unchecked Sendable` — so the manual locking below only needs
/// to guard the tap thread against `start()`/`stop()`/`droppedChunkCount`
/// calls from the main actor, not against itself.
///
/// Real capture was not exercised against physical hardware while
/// building this — this dev environment has no audio input device/GUI
/// session — see `docs/REALTIME_TRANSCRIPTION.md`'s manual verification
/// checklist.
final class MicrophoneAudioCapture: AudioCapturing, @unchecked Sendable {
    private let engine: AVAudioEngine
    private let permission: MicrophonePermissionChecking
    private let targetSampleRate: Double
    private let chunkDuration: TimeInterval
    private let maxPendingChunks: Int

    private let lock = NSLock()
    private var continuation: AsyncStream<AudioChunk>.Continuation?
    private var cachedStream: AsyncStream<AudioChunk>?
    private var converter: AVAudioConverter?
    private var pcmAccumulator: [Int16] = []
    private var sequence = 0
    private var elapsedSeconds: TimeInterval = 0
    private var _droppedChunkCount = 0
    private var isRunning = false

    var droppedChunkCount: Int {
        withLock { _droppedChunkCount }
    }

    /// Plain synchronous helper — calling `NSLock.lock()`/`unlock()`
    /// directly inside an `async` function body is flagged as unavailable
    /// under strict concurrency, even with no `await` in between (the same
    /// issue `FakeIPCTransport.swift`'s `appendSent(_:)` works around).
    /// Wrapping the critical section in a synchronous, non-`async` helper
    /// sidesteps that restriction.
    private func withLock<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    init(
        engine: AVAudioEngine = AVAudioEngine(),
        permission: MicrophonePermissionChecking = AVFoundationMicrophonePermission(),
        targetSampleRate: Double = 16000,
        chunkDuration: TimeInterval = 0.5,
        maxPendingChunks: Int = 8
    ) {
        self.engine = engine
        self.permission = permission
        self.targetSampleRate = targetSampleRate
        self.chunkDuration = chunkDuration
        self.maxPendingChunks = maxPendingChunks
    }

    func chunks() -> AsyncStream<AudioChunk> {
        lock.lock(); defer { lock.unlock() }
        if let cachedStream { return cachedStream }
        let (stream, continuation) = AsyncStream<AudioChunk>.makeStream(
            bufferingPolicy: .bufferingNewest(maxPendingChunks)
        )
        self.continuation = continuation
        self.cachedStream = stream
        return stream
    }

    func start() async throws {
        let authState = await permission.requestAccess()
        guard authState == .authorized else {
            throw AudioCaptureError.microphonePermissionDenied
        }

        _ = chunks() // ensures the continuation exists even if the caller never called chunks() first.

        let inputNode = engine.inputNode
        let inputFormat = inputNode.inputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
            throw AudioCaptureError.unsupportedFormat("Input node reported an invalid format: \(inputFormat).")
        }
        guard let targetFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: targetSampleRate,
            channels: 1,
            interleaved: true
        ) else {
            throw AudioCaptureError.unsupportedFormat("Could not construct the target 16kHz mono PCM format.")
        }
        guard let converter = AVAudioConverter(from: inputFormat, to: targetFormat) else {
            throw AudioCaptureError.unsupportedFormat(
                "Could not build a converter from \(inputFormat) to \(targetFormat)."
            )
        }

        withLock {
            self.converter = converter
            pcmAccumulator = []
            sequence = 0
            elapsedSeconds = 0
        }

        inputNode.installTap(onBus: 0, bufferSize: 1024, format: inputFormat) { [weak self] buffer, _ in
            self?.handleTap(buffer: buffer, targetFormat: targetFormat)
        }

        engine.prepare()
        do {
            try engine.start()
        } catch {
            inputNode.removeTap(onBus: 0)
            throw AudioCaptureError.engineStartFailed(error.localizedDescription)
        }

        withLock { isRunning = true }
    }

    func stop() async {
        let wasRunning = withLock {
            let previous = isRunning
            isRunning = false
            return previous
        }

        guard wasRunning else { return }

        engine.inputNode.removeTap(onBus: 0)
        if engine.isRunning {
            engine.stop()
        }

        withLock {
            continuation?.finish()
            // Clear the finished stream/continuation so the *next*
            // `start()` builds a fresh one via `chunks()`. Without this,
            // a reused `MicrophoneAudioCapture` instance (as
            // `AppDelegate` injects for the app's lifetime) would hand
            // every subsequent Live Session the same already-finished
            // stream — real transcription would silently stop receiving
            // any audio from the second session onward.
            continuation = nil
            cachedStream = nil
        }
    }

    private func handleTap(buffer: AVAudioPCMBuffer, targetFormat: AVAudioFormat) {
        lock.lock()
        guard isRunning, let converter else {
            lock.unlock()
            return
        }
        lock.unlock()

        let ratio = targetFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 16)
        guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity) else { return }

        // `AVAudioConverterInputBlock` is inferred `@Sendable`, but this
        // one-shot conversion is invoked synchronously by `convert(to:
        // error:withInputFrom:)` on this same thread before it returns —
        // never actually shared across threads — so suppressing the
        // Sendable capture check here is safe.
        nonisolated(unsafe) var hasSuppliedInput = false
        nonisolated(unsafe) let inputSource = buffer
        let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
            if hasSuppliedInput {
                outStatus.pointee = .noDataNow
                return nil
            }
            hasSuppliedInput = true
            outStatus.pointee = .haveData
            return inputSource
        }

        var conversionError: NSError?
        let status = converter.convert(to: outputBuffer, error: &conversionError, withInputFrom: inputBlock)
        guard status != .error, conversionError == nil, outputBuffer.frameLength > 0 else { return }
        guard let channelData = outputBuffer.int16ChannelData else { return }

        let frameCount = Int(outputBuffer.frameLength)
        let samples = UnsafeBufferPointer(start: channelData[0], count: frameCount)

        lock.lock()
        pcmAccumulator.append(contentsOf: samples)
        emitCompleteChunksLocked(targetFormat: targetFormat)
        lock.unlock()
    }

    /// Must be called with `lock` held.
    private func emitCompleteChunksLocked(targetFormat: AVAudioFormat) {
        let samplesPerChunk = Int(targetFormat.sampleRate * chunkDuration)
        guard samplesPerChunk > 0 else { return }

        while pcmAccumulator.count >= samplesPerChunk {
            let chunkSamples = Array(pcmAccumulator.prefix(samplesPerChunk))
            pcmAccumulator.removeFirst(samplesPerChunk)

            let pcmData = chunkSamples.withUnsafeBufferPointer { Data(buffer: $0) }
            let chunk = AudioChunk(
                sequence: sequence,
                startedAt: elapsedSeconds,
                duration: chunkDuration,
                pcm: pcmData,
                sampleRate: Int(targetFormat.sampleRate),
                channels: 1
            )
            sequence += 1
            elapsedSeconds += chunkDuration

            if let result = continuation?.yield(chunk), case .dropped = result {
                _droppedChunkCount += 1
            }
        }
    }
}
