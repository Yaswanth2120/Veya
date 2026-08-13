import AVFoundation
@preconcurrency import ScreenCaptureKit
import CoreMedia
import Foundation

/// Section 16: real native macOS meeting/system-audio capture, via
/// `ScreenCaptureKit`'s per-application audio capture where the OS
/// supports it (macOS 13+) — the same `SCStream` mechanism
/// `SafeShareCaptureEngine.swift` already uses for screen capture,
/// configured for `.audio` output instead of `.screen`.
///
/// Never requires a virtual audio driver: this captures real system/app
/// audio directly through Apple's own supported API. Prefers a specific
/// shareable application (see `selectableApplications()`) over
/// indiscriminate all-system audio when the caller selects one; falls
/// back to system-wide audio (still excluding Veya's own process, the
/// same feedback-loop guard `SafeShareCaptureEngine` uses) when the
/// caller doesn't or can't.
///
/// Real capture was not exercised against a live meeting app (Zoom/Google
/// Meet/Teams) while building this — see docs/REALTIME_TRANSCRIPTION.md's
/// manual verification checklist; this needs a human with a real meeting
/// call to actually confirm captured audio is correct.
enum SystemAudioSource: Sendable, Equatable {
    /// Captures only the named application's audio — the preferred mode,
    /// avoiding capture of anything else running on the machine.
    case application(processID: pid_t, bundleIdentifier: String, displayName: String)
    /// Falls back to all system audio (still excluding Veya's own
    /// process) — used when no specific app is selected, or per-app audio
    /// filtering isn't available on this macOS version.
    case allSystemAudio
}

struct SelectableAudioApplication: Sendable, Equatable, Hashable, Identifiable {
    let processID: pid_t
    let bundleIdentifier: String
    let displayName: String
    var id: pid_t { processID }
}

final class SystemAudioCapture: AudioCapturing, @unchecked Sendable {
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

    private var stream: SCStream?
    private var output: SystemAudioStreamOutputBridge?
    private let streamQueue = DispatchQueue(label: "com.veya.systemaudio.stream", qos: .userInitiated)

    /// Set before `start()` (e.g. from a preflight source picker); `nil`
    /// means "fall back to all system audio."
    var selectedSource: SystemAudioSource?

    var droppedChunkCount: Int {
        withLock { _droppedChunkCount }
    }

    init(targetSampleRate: Double = 16000, chunkDuration: TimeInterval = 0.5, maxPendingChunks: Int = 8) {
        self.targetSampleRate = targetSampleRate
        self.chunkDuration = chunkDuration
        self.maxPendingChunks = maxPendingChunks
    }

    private func withLock<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    /// Real, running applications ScreenCaptureKit can see right now,
    /// excluding Veya's own process — for a "select a specific meeting
    /// app" preflight picker, per the product requirement to prefer a
    /// specific application over indiscriminate all-system audio.
    static func selectableApplications() async throws -> [SelectableAudioApplication] {
        guard CGPreflightScreenCaptureAccess() else {
            throw AudioCaptureError.microphonePermissionDenied
        }
        let content: SCShareableContent
        do {
            content = try await SCShareableContent.current
        } catch {
            throw AudioCaptureError.engineStartFailed("Screen/system-audio capture could not enumerate running applications.")
        }
        let currentPID = ProcessInfo.processInfo.processIdentifier
        return content.applications
            .filter { $0.processID != currentPID && !$0.applicationName.isEmpty }
            .map { SelectableAudioApplication(processID: $0.processID, bundleIdentifier: $0.bundleIdentifier, displayName: $0.applicationName) }
    }

    func chunks() -> AsyncStream<AudioChunk> {
        lock.lock(); defer { lock.unlock() }
        if let cachedStream { return cachedStream }
        let (stream, continuation) = AsyncStream<AudioChunk>.makeStream(bufferingPolicy: .bufferingNewest(maxPendingChunks))
        self.continuation = continuation
        self.cachedStream = stream
        return stream
    }

    func start() async throws {
        guard CGPreflightScreenCaptureAccess() else {
            throw AudioCaptureError.microphonePermissionDenied
        }

        _ = chunks()

        let shareableContent: SCShareableContent
        do {
            shareableContent = try await SCShareableContent.current
        } catch {
            throw AudioCaptureError.engineStartFailed("System audio capture could not be initialized.")
        }
        guard let display = shareableContent.displays.first else {
            throw AudioCaptureError.engineStartFailed("No capturable display was found.")
        }

        let currentPID = ProcessInfo.processInfo.processIdentifier
        let veyaApps = shareableContent.applications.filter { $0.processID == currentPID }

        let filter: SCContentFilter
        switch selectedSource {
        case .application(let processID, _, _):
            guard let app = shareableContent.applications.first(where: { $0.processID == processID }) else {
                throw AudioCaptureError.engineStartFailed("The selected application is no longer available.")
            }
            // Per-application audio via a content filter scoped to just
            // that app's windows — never indiscriminate system audio when
            // a specific app was explicitly chosen.
            filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])
        case .allSystemAudio, .none:
            // System-wide, minus Veya's own process — the same
            // feedback-loop guard `SafeShareCaptureEngine` uses for video.
            filter = SCContentFilter(display: display, excludingApplications: veyaApps, exceptingWindows: [])
        }

        guard let targetFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16, sampleRate: targetSampleRate, channels: 1, interleaved: true,
        ) else {
            throw AudioCaptureError.unsupportedFormat("Could not construct the target 16kHz mono PCM format.")
        }

        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = 48000
        configuration.channelCount = 2
        // Minimal video footprint — this stream exists for its audio
        // output only, but ScreenCaptureKit requires a valid video
        // configuration regardless.
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        withLock {
            converter = nil // built lazily from the first real sample buffer's actual format
            pcmAccumulator = []
            sequence = 0
            elapsedSeconds = 0
        }

        let bridge = SystemAudioStreamOutputBridge(
            onSampleBuffer: { [weak self] sampleBuffer in
                self?.handleAudioSampleBuffer(sampleBuffer, targetFormat: targetFormat)
            },
            onStreamStopped: { [weak self] error in
                self?.handleStreamStoppedUnexpectedly(error)
            }
        )

        let newStream = SCStream(filter: filter, configuration: configuration, delegate: bridge)
        do {
            try newStream.addStreamOutput(bridge, type: .audio, sampleHandlerQueue: streamQueue)
            try await newStream.startCapture()
        } catch {
            throw AudioCaptureError.engineStartFailed("Could not start system-audio capture: \(error.localizedDescription)")
        }

        withLock {
            self.stream = newStream
            self.output = bridge
            self.isRunning = true
        }
    }

    func stop() async {
        let (wasRunning, capturedStream) = withLock {
            let previous = isRunning
            isRunning = false
            let s = stream
            stream = nil
            output = nil
            return (previous, s)
        }
        guard wasRunning else { return }
        if let capturedStream {
            try? await capturedStream.stopCapture()
        }
        withLock {
            continuation?.finish()
            continuation = nil
            cachedStream = nil
        }
    }

    private func handleStreamStoppedUnexpectedly(_ error: Error) {
        // Source disappearance (meeting app closed/restarted) or any
        // other stream-level failure — finish the stream honestly rather
        // than silently going quiet; the caller (preflight/live UI) sees
        // this via the async stream ending and can surface "Meeting audio
        // unavailable."
        withLock {
            guard isRunning else { return }
            isRunning = false
            stream = nil
            output = nil
            continuation?.finish()
            continuation = nil
            cachedStream = nil
        }
    }

    private func handleAudioSampleBuffer(_ sampleBuffer: CMSampleBuffer, targetFormat: AVAudioFormat) {
        guard sampleBuffer.isValid, sampleBuffer.numSamples > 0,
              let formatDescription = sampleBuffer.formatDescription
        else { return }

        let sourceFormat = AVAudioFormat(cmAudioFormatDescription: formatDescription)
        guard let sourcePCMBuffer = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: AVAudioFrameCount(sampleBuffer.numSamples)) else { return }
        sourcePCMBuffer.frameLength = sourcePCMBuffer.frameCapacity

        let copyStatus = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer, at: 0, frameCount: Int32(sampleBuffer.numSamples), into: sourcePCMBuffer.mutableAudioBufferList,
        )
        guard copyStatus == noErr else { return }

        lock.lock()
        if converter == nil || converter?.inputFormat != sourceFormat {
            converter = AVAudioConverter(from: sourceFormat, to: targetFormat)
        }
        guard let converter else {
            lock.unlock()
            return
        }
        lock.unlock()

        let ratio = targetFormat.sampleRate / sourceFormat.sampleRate
        let capacity = AVAudioFrameCount(Double(sourcePCMBuffer.frameLength) * ratio + 16)
        guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity) else { return }

        nonisolated(unsafe) var hasSuppliedInput = false
        nonisolated(unsafe) let inputSource = sourcePCMBuffer
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
        guard isRunning else {
            lock.unlock()
            return
        }
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

/// Bridges `ScreenCaptureKit`'s Objective-C delegate callbacks (arriving
/// off-actor, on `streamQueue`) into plain closures — same pattern as
/// `SafeShareCaptureEngine`'s `StreamOutputBridge`.
private final class SystemAudioStreamOutputBridge: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private let onSampleBuffer: @Sendable (CMSampleBuffer) -> Void
    private let onStreamStopped: @Sendable (Error) -> Void

    init(onSampleBuffer: @escaping @Sendable (CMSampleBuffer) -> Void, onStreamStopped: @escaping @Sendable (Error) -> Void) {
        self.onSampleBuffer = onSampleBuffer
        self.onStreamStopped = onStreamStopped
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }
        onSampleBuffer(sampleBuffer)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        onStreamStopped(error)
    }
}
