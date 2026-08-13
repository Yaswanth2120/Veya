import Foundation

extension FileHandle {
    /// Yields complete lines (newline stripped) as they arrive, using
    /// `readabilityHandler` so reads happen in OS-buffer-sized chunks
    /// rather than one byte at a time.
    ///
    /// `FileHandle.bytes` (`AsyncBytes`) looks like the natural async
    /// choice here, but iterating it one `UInt8` at a time pays an async
    /// suspension per byte — for a chatty pipe (the worker's mocked event
    /// stream emits dozens of small JSON lines per second) that overhead
    /// alone stretched a ~3 second mocked session to over 10 seconds in
    /// testing. `readabilityHandler` delivers each available chunk in one
    /// callback, which is what actually matters for a line-oriented
    /// protocol like this one.
    func linesStream() -> AsyncStream<String> {
        // `readabilityHandler` is documented to be invoked serially (never
        // concurrently) by its internal dispatch source, so a plain
        // reference-type box for the buffer is safe despite not being
        // provably `Sendable` to the type checker.
        let bufferBox = LineBufferBox()

        return AsyncStream { continuation in
            self.readabilityHandler = { handle in
                let data = handle.availableData
                guard !data.isEmpty else {
                    handle.readabilityHandler = nil
                    continuation.finish()
                    return
                }

                for line in bufferBox.appendAndExtractLines(data) {
                    continuation.yield(line)
                }
            }

            continuation.onTermination = { _ in
                self.readabilityHandler = nil
            }
        }
    }
}

/// `@unchecked Sendable` because `FileHandle.readabilityHandler` is only
/// ever invoked serially, one call at a time — see `linesStream()`.
private final class LineBufferBox: @unchecked Sendable {
    private var buffer = Data()
    private let newline = UInt8(ascii: "\n")

    func appendAndExtractLines(_ data: Data) -> [String] {
        buffer.append(data)
        var lines: [String] = []
        while let newlineIndex = buffer.firstIndex(of: newline) {
            let lineData = buffer[buffer.startIndex..<newlineIndex]
            if let line = String(data: lineData, encoding: .utf8) {
                lines.append(line)
            }
            buffer.removeSubrange(buffer.startIndex...newlineIndex)
        }
        return lines
    }
}
