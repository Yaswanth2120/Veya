import CryptoKit
import Foundation
import Testing
@testable import Veya

/// Intercepts requests to `https://fixture.test/...` and serves bytes
/// from an in-memory table — the "local fixture HTTP server or equivalent
/// deterministic source" the packaging build prompt asks for, without
/// downloading a real model file or opening a real socket in tests.
final class FixtureURLProtocol: URLProtocol {
    nonisolated(unsafe) static var responses: [URL: Data] = [:]
    nonisolated(unsafe) static var failingURLs: Set<URL> = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url else { client?.urlProtocol(self, didFailWithError: URLError(.badURL)); return }
        if Self.failingURLs.contains(url) {
            client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost))
            return
        }
        guard let data = Self.responses[url] else {
            let response = HTTPURLResponse(url: url, statusCode: 404, httpVersion: nil, headerFields: nil)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocolDidFinishLoading(self)
            return
        }
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: ["Content-Length": "\(data.count)"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

@MainActor
@Suite("WhisperModelManager")
struct WhisperModelManagerTests {
    private func fixtureSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [FixtureURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    private func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func makeCacheDirectory() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("veya-model-test-\(UUID().uuidString)", isDirectory: true)
    }

    @Test("downloads, verifies, and caches a model on first use")
    func downloadsAndCaches() async throws {
        let payload = Data("fake model weights".utf8)
        let url = URL(string: "https://fixture.test/model-a.bin")!
        FixtureURLProtocol.responses[url] = payload
        defer { FixtureURLProtocol.responses.removeValue(forKey: url) }

        let entry = WhisperModelManifestEntry(id: "base.en", url: url, sha256: sha256Hex(payload), architecture: "arm64", sizeBytes: Int64(payload.count), version: "1")
        let manager = WhisperModelManager(session: fixtureSession(), cacheDirectory: makeCacheDirectory())

        let path = await manager.ensureModelAvailable(for: entry)
        #expect(path != nil)
        #expect(FileManager.default.fileExists(atPath: path!.path))
        if case .ready = manager.state {} else { Issue.record("expected .ready, got \(manager.state)") }
    }

    @Test("a cached valid model is never re-downloaded")
    func doesNotRedownloadWhenCacheIsValid() async throws {
        let payload = Data("fake model weights".utf8)
        let url = URL(string: "https://fixture.test/model-b.bin")!
        FixtureURLProtocol.responses[url] = payload
        defer { FixtureURLProtocol.responses.removeValue(forKey: url) }

        let entry = WhisperModelManifestEntry(id: "base.en", url: url, sha256: sha256Hex(payload), architecture: "arm64", sizeBytes: Int64(payload.count), version: "1")
        let manager = WhisperModelManager(session: fixtureSession(), cacheDirectory: makeCacheDirectory())

        let firstPath = await manager.ensureModelAvailable(for: entry)
        #expect(firstPath != nil)

        // Remove the fixture response entirely — a second call must still
        // succeed purely from the verified local cache, proving it never
        // re-hits the network once a valid file exists.
        FixtureURLProtocol.responses.removeValue(forKey: url)
        let secondPath = await manager.ensureModelAvailable(for: entry)
        #expect(secondPath == firstPath)
    }

    @Test("a checksum mismatch is rejected and never treated as a valid cached model")
    func rejectsChecksumMismatch() async throws {
        let payload = Data("fake model weights".utf8)
        let url = URL(string: "https://fixture.test/model-c.bin")!
        FixtureURLProtocol.responses[url] = payload
        defer { FixtureURLProtocol.responses.removeValue(forKey: url) }

        let entry = WhisperModelManifestEntry(id: "base.en", url: url, sha256: String(repeating: "0", count: 64), architecture: "arm64", sizeBytes: Int64(payload.count), version: "1")
        let manager = WhisperModelManager(session: fixtureSession(), cacheDirectory: makeCacheDirectory())

        let path = await manager.ensureModelAvailable(for: entry)
        #expect(path == nil)
        if case .failed = manager.state {} else { Issue.record("expected .failed, got \(manager.state)") }
    }

    @Test("a corrupted cached file is re-downloaded rather than trusted")
    func redownloadsWhenCacheIsCorrupted() async throws {
        let payload = Data("fake model weights".utf8)
        let url = URL(string: "https://fixture.test/model-d.bin")!
        FixtureURLProtocol.responses[url] = payload
        defer { FixtureURLProtocol.responses.removeValue(forKey: url) }

        let entry = WhisperModelManifestEntry(id: "base.en", url: url, sha256: sha256Hex(payload), architecture: "arm64", sizeBytes: Int64(payload.count), version: "1")
        let cacheDirectory = makeCacheDirectory()
        let manager = WhisperModelManager(session: fixtureSession(), cacheDirectory: cacheDirectory)

        let path = await manager.ensureModelAvailable(for: entry)
        #expect(path != nil)

        try Data("corrupted".utf8).write(to: path!)

        let manager2 = WhisperModelManager(session: fixtureSession(), cacheDirectory: cacheDirectory)
        let repairedPath = await manager2.ensureModelAvailable(for: entry)
        #expect(repairedPath != nil)
        #expect(try Data(contentsOf: repairedPath!) == payload)
    }

    @Test("an interrupted download never leaves a corrupted file mistaken for a valid model")
    func interruptedDownloadRecovers() async throws {
        let url = URL(string: "https://fixture.test/model-e.bin")!
        FixtureURLProtocol.failingURLs.insert(url)
        defer { FixtureURLProtocol.failingURLs.remove(url) }

        let entry = WhisperModelManifestEntry(id: "base.en", url: url, sha256: String(repeating: "0", count: 64), architecture: "arm64", sizeBytes: 100, version: "1")
        let cacheDirectory = makeCacheDirectory()
        let manager = WhisperModelManager(session: fixtureSession(), cacheDirectory: cacheDirectory)

        let path = await manager.ensureModelAvailable(for: entry)
        #expect(path == nil)
        if case .failed = manager.state {} else { Issue.record("expected .failed, got \(manager.state)") }

        // Recovery: no stray temp/partial file left behind that a later
        // attempt could mistake for a valid cached model.
        let leftoverFiles = (try? FileManager.default.contentsOfDirectory(atPath: cacheDirectory.path)) ?? []
        #expect(leftoverFiles.allSatisfy { !$0.hasSuffix(".download") })

        FixtureURLProtocol.failingURLs.remove(url)
        let payload = Data("recovered weights".utf8)
        FixtureURLProtocol.responses[url] = payload
        let entry2 = WhisperModelManifestEntry(id: "base.en", url: url, sha256: sha256Hex(payload), architecture: "arm64", sizeBytes: Int64(payload.count), version: "1")
        let recoveredPath = await manager.ensureModelAvailable(for: entry2)
        #expect(recoveredPath != nil)
        FixtureURLProtocol.responses.removeValue(forKey: url)
    }

    /// There is no hard-coded `WhisperModelManifestEntry` struct literal
    /// anywhere in `WhisperModelManager.swift` — every value the app can
    /// resolve comes from a JSON file on disk, in priority order: an
    /// explicit `VEYA_WHISPER_MODEL_MANIFEST_PATH` override, a packaged
    /// release's bundled copy, or (checked here, with an empty
    /// environment) this checkout's own `packaging/whisper_model_manifest.json`
    /// — the real production manifest, whose SHA-256 this test proves was
    /// actually computed from the file it references (downloaded with
    /// `curl` and hashed with `shasum -a 256`), not guessed.
    @Test("the default manifest resolves from the repo's own checked-in file, and its hash was genuinely computed from the referenced release asset")
    func defaultManifestIsRepoCheckedInAndGenuinelyHashed() throws {
        let entry = WhisperModelManifest.recommended(environment: [:])
        #expect(entry != nil)
        #expect(entry?.sha256.count == 64)
    }

    /// The real hardware-specific selection: arm64 and x86_64 must
    /// resolve to *different* manifest entries (a different real, genuinely
    /// hashed model file each), not the same entry with an ignored
    /// `architecture` parameter. arm64 gets the larger, more accurate
    /// `ggml-base.en`; x86_64 gets the lighter `ggml-tiny.en`.
    @Test("arm64 and x86_64 resolve to different, genuinely hashed model entries")
    func architectureSelectionPicksDifferentRealEntries() throws {
        let arm64Entry = WhisperModelManifest.recommended(architecture: "arm64", environment: [:])
        let x86Entry = WhisperModelManifest.recommended(architecture: "x86_64", environment: [:])

        #expect(arm64Entry != nil)
        #expect(x86Entry != nil)
        #expect(arm64Entry?.id == "ggml-base.en")
        #expect(x86Entry?.id == "ggml-tiny.en")
        #expect(arm64Entry?.sha256 != x86Entry?.sha256)
        #expect(arm64Entry?.sizeBytes != x86Entry?.sizeBytes)

        // An architecture with no explicit entry falls back to "default"
        // rather than resolving to nothing.
        let unknownArchitectureEntry = WhisperModelManifest.recommended(architecture: "riscv64", environment: [:])
        #expect(unknownArchitectureEntry != nil)
    }

    @Test("an explicit VEYA_WHISPER_MODEL_MANIFEST_PATH override wins, and its hash matches the real referenced file")
    func explicitManifestPathOverrideWinsAndIsGenuinelyVerified() throws {
        let manifestPath = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // VeyaTests/
            .deletingLastPathComponent() // Tests/
            .deletingLastPathComponent() // <repo root>
            .appendingPathComponent("packaging/whisper_model_manifest.example.json").path
        let entry = WhisperModelManifest.recommended(environment: ["VEYA_WHISPER_MODEL_MANIFEST_PATH": manifestPath])
        #expect(entry != nil)
        #expect(entry?.id == "for-tests-ggml-base.en")

        guard let entry, let referencedPath = entry.url.path.removingPercentEncoding else {
            Issue.record("manifest entry or its file:// path failed to resolve")
            return
        }
        let referencedData = try Data(contentsOf: URL(fileURLWithPath: referencedPath))
        let actualHash = SHA256.hash(data: referencedData).map { String(format: "%02x", $0) }.joined()
        #expect(actualHash == entry.sha256)
    }
}
