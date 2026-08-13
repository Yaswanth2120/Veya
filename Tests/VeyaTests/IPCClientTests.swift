import Foundation
import Testing
@testable import Veya

@Suite("IPCClient")
struct IPCClientTests {
    @Test("a successful response resolves the matching pending request")
    func successfulResponseResolves() async throws {
        let transport = FakeIPCTransport()
        let client = IPCClient(transport: transport)
        await client.start()

        async let result: PingResult = client.call(method: "system.ping", params: EmptyIPCParams(), timeout: 2)

        try await waitUntil { transport.sentLines.count == 1 }
        let id = try #require(transport.lastSentRequestID())
        transport.simulateIncoming(#"{"version":1,"id":"\#(id)","type":"response","result":{"pong":true}}"#)

        let resolved = try await result
        #expect(resolved.pong == true)
    }

    @Test("two concurrent requests correlate independently and can resolve out of order")
    func requestCorrelationOutOfOrder() async throws {
        let transport = FakeIPCTransport()
        let client = IPCClient(transport: transport)
        await client.start()

        async let first: SystemInfoResult = client.call(method: "system.info", params: EmptyIPCParams(), timeout: 2)
        try await waitUntil { transport.sentLines.count == 1 }
        let firstID = try #require(transport.lastSentRequestID())

        async let second: PingResult = client.call(method: "system.ping", params: EmptyIPCParams(), timeout: 2)
        try await waitUntil { transport.sentLines.count == 2 }
        let secondID = try #require(transport.lastSentRequestID())

        #expect(firstID != secondID)

        // Reply to the *second* request first, to prove correlation isn't
        // relying on send/receive order.
        transport.simulateIncoming(#"{"version":1,"id":"\#(secondID)","type":"response","result":{"pong":true}}"#)
        transport.simulateIncoming(
            #"{"version":1,"id":"\#(firstID)","type":"response","result":{"protocol_version":1,"worker_version":"0.1.0","pid":123}}"#
        )

        let secondResolved = try await second
        let firstResolved = try await first
        #expect(secondResolved.pong == true)
        #expect(firstResolved.workerVersion == "0.1.0")
    }

    @Test("a request that never gets a response times out")
    func requestTimesOut() async throws {
        let transport = FakeIPCTransport()
        let client = IPCClient(transport: transport)
        await client.start()

        await #expect(throws: IPCClientError.timeout(method: "system.ping")) {
            let _: PingResult = try await client.call(method: "system.ping", params: EmptyIPCParams(), timeout: 0.05)
        }
    }

    @Test("an error response throws protocolError with the code and message")
    func errorResponseThrows() async throws {
        let transport = FakeIPCTransport()
        let client = IPCClient(transport: transport)
        await client.start()

        async let result: PingResult = client.call(method: "system.ping", params: EmptyIPCParams(), timeout: 2)
        try await waitUntil { transport.sentLines.count == 1 }
        let id = try #require(transport.lastSentRequestID())
        transport.simulateIncoming(
            #"{"version":1,"id":"\#(id)","type":"error","error":{"code":"INVALID_REQUEST","message":"bad"}}"#
        )

        do {
            _ = try await result
            Issue.record("Expected call to throw a protocolError.")
        } catch let error as IPCClientError {
            #expect(error == IPCClientError.protocolError(code: "INVALID_REQUEST", message: "bad"))
        }
    }

    @Test("malformed worker output does not crash the client and surfaces a diagnostic event")
    func malformedOutputSurfacesDiagnosticEvent() async throws {
        let transport = FakeIPCTransport()
        let client = IPCClient(transport: transport)
        await client.start()

        var iterator = client.events.makeAsyncIterator()
        transport.simulateIncoming("{not valid json")

        let event = await iterator.next()
        #expect(event?.name == "_protocol.malformed")
    }

    @Test("a malformed line does not affect an unrelated pending request")
    func malformedLineDoesNotAffectPendingRequests() async throws {
        let transport = FakeIPCTransport()
        let client = IPCClient(transport: transport)
        await client.start()

        async let result: PingResult = client.call(method: "system.ping", params: EmptyIPCParams(), timeout: 2)
        try await waitUntil { transport.sentLines.count == 1 }
        let id = try #require(transport.lastSentRequestID())

        transport.simulateIncoming("not json at all")
        transport.simulateIncoming(#"{"version":1,"id":"\#(id)","type":"response","result":{"pong":true}}"#)

        let resolved = try await result
        #expect(resolved.pong == true)
    }

    @Test("an event line is yielded from the events stream with typed data")
    func eventLineIsYielded() async throws {
        let transport = FakeIPCTransport()
        let client = IPCClient(transport: transport)
        await client.start()

        var iterator = client.events.makeAsyncIterator()
        transport.simulateIncoming(#"{"version":1,"type":"event","event":"session.started","data":{"session_id":"s1"}}"#)

        let event = await iterator.next()
        #expect(event?.name == "session.started")
        let data: SessionStartedEventData = try #require(event?.data).decoded()
        #expect(data.sessionId == "s1")
    }

    @Test("stopping the client fails all pending requests with workerUnavailable")
    func stopFailsPendingRequests() async throws {
        let transport = FakeIPCTransport()
        let client = IPCClient(transport: transport)
        await client.start()

        async let result: PingResult = client.call(method: "system.ping", params: EmptyIPCParams(), timeout: 5)
        try await waitUntil { transport.sentLines.count == 1 }

        await client.stop()

        do {
            _ = try await result
            Issue.record("Expected call to throw workerUnavailable.")
        } catch let error as IPCClientError {
            #expect(error == IPCClientError.workerUnavailable)
        }
    }

    @Test("the transport closing (EOF) fails all pending requests")
    func transportClosingFailsPendingRequests() async throws {
        let transport = FakeIPCTransport()
        let client = IPCClient(transport: transport)
        await client.start()

        async let result: PingResult = client.call(method: "system.ping", params: EmptyIPCParams(), timeout: 5)
        try await waitUntil { transport.sentLines.count == 1 }

        transport.simulateClose()

        do {
            _ = try await result
            Issue.record("Expected call to throw workerUnavailable.")
        } catch let error as IPCClientError {
            #expect(error == IPCClientError.workerUnavailable)
        }
    }
}

