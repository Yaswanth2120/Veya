import CryptoKit
import Foundation

/// One entry in the local Whisper model manifest: where to fetch a model
/// weight file, its expected integrity hash, and which hardware
/// architecture it targets. Never a hard-coded, unverified hash anywhere
/// else in this file — every download is checked against exactly this.
struct WhisperModelManifestEntry: Codable, Equatable, Sendable {
    let id: String
    let url: URL
    let sha256: String
    let architecture: String
    let sizeBytes: Int64
    let version: String
}

enum WhisperModelDownloadState: Equatable, Sendable {
    case idle
    case downloading(progress: Double)
    case verifying
    case ready(path: URL)
    case failed(reason: String)
}

enum WhisperModelManagerError: LocalizedError {
    case checksumMismatch
    case downloadFailed(String)

    var errorDescription: String? {
        switch self {
        case .checksumMismatch: return "The downloaded model failed integrity verification."
        case .downloadFailed(let reason): return "Model download failed: \(reason)"
        }
    }
}

/// Hardware-specific manifest resolution. Deliberately has **no**
/// hard-coded URL/SHA-256 struct literal in source — fabricating a
/// plausible-looking hash for an asset that was never actually downloaded
/// and verified here would be exactly the "hard-coded unverified hash"
/// the packaging build prompt says not to ship. Instead this resolves a
/// small JSON file shaped like `WhisperModelManifestEntry`, in priority
/// order:
///
/// 1. `VEYA_WHISPER_MODEL_MANIFEST_PATH` (explicit override, dev/CI).
/// 2. `Bundle.main`'s bundled `whisper_model_manifest.json` (a packaged
///    release — see `packaging/build_app.sh`, which copies
///    `packaging/whisper_model_manifest.json`).
/// 3. The dev-time, checkout-relative `packaging/whisper_model_manifest.json`
///    (only meaningful for `swift run`/`swift test` against a checkout of
///    this repository, same caveat as
///    `PythonWorkerConfiguration.projectRelativeDefaultWorkerDirectory()`).
///
/// Every manifest value the repository ships was produced by actually
/// downloading the referenced file and hashing it with `shasum -a 256` —
/// not guessed. See `docs/PYTHON_PACKAGING.md` for the release procedure
/// to follow when the referenced model release changes.
enum WhisperModelManifest {
    static func recommended(architecture: String = currentArchitecture(), environment: [String: String] = ProcessInfo.processInfo.environment) -> WhisperModelManifestEntry? {
        if let path = environment["VEYA_WHISPER_MODEL_MANIFEST_PATH"], let entry = decode(path: path) {
            return entry
        }
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("whisper_model_manifest.json").path,
           let entry = decode(path: bundled) {
            return entry
        }
        if let entry = decode(path: projectRelativeManifestPath()) {
            return entry
        }
        return nil
    }

    private static func decode(path: String) -> WhisperModelManifestEntry? {
        guard let data = FileManager.default.contents(atPath: path) else { return nil }
        return try? JSONDecoder().decode(WhisperModelManifestEntry.self, from: data)
    }

    private static func projectRelativeManifestPath() -> String {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // Transcription/
            .deletingLastPathComponent() // Veya/
            .deletingLastPathComponent() // Sources/
            .deletingLastPathComponent() // <repo root>
            .appendingPathComponent("packaging/whisper_model_manifest.json").path
    }

    static func currentArchitecture() -> String {
        #if arch(arm64)
        return "arm64"
        #else
        return "x86_64"
        #endif
    }
}

/// First-launch local model manager for Whisper weights (see the
/// packaging build prompt's "First-launch model manager" section).
/// Downloads over HTTPS on first use, verifies SHA-256 before the file is
/// ever considered usable, writes atomically (temp file + rename, never a
/// partially-written file mistaken for a valid model), and is fully
/// offline afterward — nothing here re-checks the network once a valid
/// cached file exists. Never logs model bytes, URLs beyond the manifest,
/// or file contents — only typed state transitions.
@MainActor
final class WhisperModelManager: ObservableObject {
    @Published private(set) var state: WhisperModelDownloadState = .idle

    private let session: URLSession
    private let cacheDirectory: URL

    init(session: URLSession = .shared, cacheDirectory: URL = WhisperModelManager.defaultCacheDirectory()) {
        self.session = session
        self.cacheDirectory = cacheDirectory
    }

    static func defaultCacheDirectory() -> URL {
        let appSupport = (try? FileManager.default.url(
            for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true
        )) ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return appSupport.appendingPathComponent("Veya/Models", isDirectory: true)
    }

    private func destinationURL(for entry: WhisperModelManifestEntry) -> URL {
        cacheDirectory.appendingPathComponent("\(entry.id)-\(entry.version).bin")
    }

    /// Returns the local file URL for `entry`, downloading and verifying
    /// it first if no valid cached copy already exists. A previously
    /// cached file is trusted only after re-hashing it — a corrupted or
    /// tampered cache is treated exactly like "not cached" and
    /// re-downloaded, never silently used.
    func ensureModelAvailable(for entry: WhisperModelManifestEntry) async -> URL? {
        let destination = destinationURL(for: entry)
        if FileManager.default.fileExists(atPath: destination.path), let hash = try? Self.sha256Hex(of: destination), hash == entry.sha256 {
            state = .ready(path: destination)
            return destination
        }

        do {
            try FileManager.default.createDirectory(at: cacheDirectory, withIntermediateDirectories: true)
            // A distinct temp path per attempt: an interrupted previous
            // download's partial file is simply abandoned/overwritten
            // here, never mistaken for a resumable or valid model — the
            // next attempt always starts from a clean, fully verified
            // state rather than risking a corrupt half-file.
            let temporaryURL = cacheDirectory.appendingPathComponent("\(entry.id)-\(UUID().uuidString).download")
            defer { try? FileManager.default.removeItem(at: temporaryURL) }

            try await download(from: entry.url, to: temporaryURL, expectedBytes: entry.sizeBytes)

            state = .verifying
            let hash = try Self.sha256Hex(of: temporaryURL)
            guard hash == entry.sha256 else {
                state = .failed(reason: "Checksum verification failed.")
                return nil
            }

            if FileManager.default.fileExists(atPath: destination.path) {
                try FileManager.default.removeItem(at: destination)
            }
            try FileManager.default.moveItem(at: temporaryURL, to: destination)
            state = .ready(path: destination)
            return destination
        } catch {
            state = .failed(reason: "The model could not be downloaded.")
            return nil
        }
    }

    /// Uses `URLSession`'s delegate-based download task (via the async
    /// `download(for:delegate:)` overload, so it still goes through the
    /// injected `session` — including a test's mocked `URLProtocol` —
    /// rather than a separate ad-hoc session) instead of iterating
    /// `AsyncBytes` one `UInt8` at a time. The naive byte-at-a-time
    /// approach was measured against this codebase's real ~75MB
    /// production model and sustained roughly 85KB/s — Swift's
    /// per-element `AsyncSequence` overhead dominating actual network
    /// throughput, which would make a first-launch download take on the
    /// order of 15 minutes. The delegate-based download task lets
    /// `URLSession` stream directly to disk at native speed, with
    /// `didWriteData` still providing real progress.
    private func download(from url: URL, to destination: URL, expectedBytes: Int64) async throws {
        state = .downloading(progress: 0)
        let total = max(expectedBytes, 1)
        let progressReporter: @Sendable (Int64) -> Void = { [weak self] bytesWritten in
            Task { @MainActor in
                self?.state = .downloading(progress: min(Double(bytesWritten) / Double(total), 1.0))
            }
        }

        let temporaryDownloadLocation: URL
        let response: URLResponse
        do {
            (temporaryDownloadLocation, response) = try await session.download(
                for: URLRequest(url: url),
                delegate: DownloadProgressDelegate(onProgress: progressReporter)
            )
        } catch {
            throw WhisperModelManagerError.downloadFailed(String(reflecting: type(of: error)))
        }
        guard let httpResponse = response as? HTTPURLResponse, (200..<300).contains(httpResponse.statusCode) else {
            throw WhisperModelManagerError.downloadFailed("Unexpected server response.")
        }

        if FileManager.default.fileExists(atPath: destination.path) {
            try FileManager.default.removeItem(at: destination)
        }
        try FileManager.default.moveItem(at: temporaryDownloadLocation, to: destination)
        state = .downloading(progress: 1.0)
    }

    private final class DownloadProgressDelegate: NSObject, URLSessionDownloadDelegate, URLSessionTaskDelegate, @unchecked Sendable {
        private let onProgress: @Sendable (Int64) -> Void

        init(onProgress: @escaping @Sendable (Int64) -> Void) {
            self.onProgress = onProgress
        }

        func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64, totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
            onProgress(totalBytesWritten)
        }

        // Required by `URLSessionDownloadDelegate`, but the async
        // `session.download(for:delegate:)` overload resumes its own
        // continuation with the temp file's URL directly — this delegate
        // exists purely for the `didWriteData` progress callback above.
        func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {}
    }

    private static func sha256Hex(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while true {
            let chunk = try handle.read(upToCount: 1 << 20) ?? Data()
            if chunk.isEmpty { break }
            hasher.update(data: chunk)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
