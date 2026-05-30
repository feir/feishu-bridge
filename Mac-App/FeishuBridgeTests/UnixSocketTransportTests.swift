import Foundation
import Testing

@testable import FeishuBridge

/// Exercises the POSIX `AF_UNIX` transport against a real line-oriented server,
/// matching the bridge's one-request-per-line model. Covers the happy path plus
/// the failure modes the rewrite exists to make predictable: connect refused,
/// no-reply timeout, peer close mid-exchange, and partial (un-delimited) data.
@Suite("UnixSocketTransport")
struct UnixSocketTransportTests {

    @Test func sendReturnsLineBeforeNewline() async throws {
        let path = Self.tempSockPath()
        let server = try LineServer(path: path, mode: .reply(#"{"ok":true}"#))
        defer { server.stop() }

        let transport = UnixSocketTransport(sockPath: path)
        let response = try await transport.send(Data(#"{"method":"health"}"#.utf8) + Data([0x0A]))

        // Newline delimiter MUST be stripped; payload decodes as JSON.
        #expect(!response.contains(0x0A))
        let json = try JSONSerialization.jsonObject(with: response) as? [String: Any]
        #expect(json?["ok"] as? Bool == true)
    }

    @Test func probeTrueWhenListening() async throws {
        let path = Self.tempSockPath()
        let server = try LineServer(path: path, mode: .reply("{}"))
        defer { server.stop() }

        let reachable = await UnixSocketTransport(sockPath: path).probe()
        #expect(reachable)
    }

    @Test func probeFalseWhenNoServer() async {
        let path = Self.tempSockPath()  // never bound
        let reachable = await UnixSocketTransport(sockPath: path).probe()
        #expect(!reachable)
    }

    @Test func sendThrowsConnectionFailedWhenNoServer() async {
        let path = Self.tempSockPath()  // never bound
        await #expect(throws: UnixSocketTransport.TransportError.connectionFailed) {
            _ = try await UnixSocketTransport(sockPath: path).send(Data([0x0A]))
        }
    }

    @Test func sendTimesOutWhenServerNeverReplies() async throws {
        let path = Self.tempSockPath()
        let server = try LineServer(path: path, mode: .silent)
        defer { server.stop() }

        // Short timeout so the test stays fast; SO_RCVTIMEO must fire as timedOut.
        let transport = UnixSocketTransport(sockPath: path, timeout: 0.5)
        await #expect(throws: UnixSocketTransport.TransportError.timedOut) {
            _ = try await transport.send(Data([0x0A]))
        }
    }

    @Test func sendThrowsTruncatedWhenPeerClosesWithoutNewline() async throws {
        let path = Self.tempSockPath()
        // Server writes bytes lacking the "\n" frame delimiter, then closes.
        let server = try LineServer(path: path, mode: .partial(#"{"ok":tr"#))
        defer { server.stop() }

        let transport = UnixSocketTransport(sockPath: path)
        await #expect(throws: UnixSocketTransport.TransportError.truncatedResponse) {
            _ = try await transport.send(Data([0x0A]))
        }
    }

    // MARK: - Helpers

    private static func tempSockPath() -> String {
        // Keep well under sockaddr_un's 104-byte sun_path limit.
        "/tmp/fbt-\(UUID().uuidString.prefix(8)).sock"
    }
}

/// Minimal POSIX `AF_UNIX` test server. Accepts connections on a background
/// queue and behaves per ``Mode`` until ``stop()``.
private final class LineServer: @unchecked Sendable {
    enum Mode {
        case reply(String)   // read a line, write reply + "\n", close
        case silent          // accept, hold the connection open, never reply
        case partial(String) // write bytes WITHOUT a newline, then close
    }

    private let listenFD: Int32
    private let path: String
    private let lock = NSLock()
    private var heldClients: [Int32] = []   // silent-mode fds kept open until stop()

    init(path: String, mode: Mode) throws {
        self.path = path
        // Bulletproof against writes to a peer that already closed (e.g. probe()
        // connects then immediately closes): ignore SIGPIPE for this test process
        // so such a write returns EPIPE instead of killing the runner.
        signal(SIGPIPE, SIG_IGN)
        unlink(path)

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Failure.socket }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let capacity = MemoryLayout.size(ofValue: addr.sun_path)
        let bytes = Array(path.utf8)
        precondition(bytes.count < capacity)
        withUnsafeMutablePointer(to: &addr.sun_path) { tuplePtr in
            tuplePtr.withMemoryRebound(to: CChar.self, capacity: capacity) { dst in
                for (i, b) in bytes.enumerated() { dst[i] = CChar(bitPattern: b) }
                dst[bytes.count] = 0
            }
        }

        let bound = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                bind(fd, sa, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bound == 0 else { close(fd); throw Failure.bind }
        guard listen(fd, 4) == 0 else { close(fd); throw Failure.listen }
        self.listenFD = fd

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            while true {
                let client = accept(fd, nil, nil)
                if client < 0 { break }   // listener closed by stop()
                // probe() connects then closes without reading; writing the
                // reply to that already-closed peer would raise SIGPIPE and
                // crash the test process. Convert it to an EPIPE error instead.
                var on: Int32 = 1
                setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &on,
                           socklen_t(MemoryLayout<Int32>.size))
                self?.serve(client: client, mode: mode)
            }
        }
    }

    private func serve(client: Int32, mode: Mode) {
        let got = drainRequest(client)
        switch mode {
        case .reply(let body):
            // Only reply if the client actually sent a request. probe() sends
            // nothing and closes, so skipping avoids a write to a dead socket.
            if got > 0 { writeAll(client, Array((body + "\n").utf8)) }
            close(client)
        case .partial(let body):
            if got > 0 { writeAll(client, Array(body.utf8)) }  // no trailing newline
            close(client)
        case .silent:
            // Hold the fd open so the client read blocks until its own timeout.
            lock.lock(); heldClients.append(client); lock.unlock()
        }
    }

    @discardableResult
    private func drainRequest(_ client: Int32) -> Int {
        var buf = [UInt8](repeating: 0, count: 4096)
        return buf.withUnsafeMutableBytes { read(client, $0.baseAddress, $0.count) }
    }

    private func writeAll(_ client: Int32, _ bytes: [UInt8]) {
        _ = bytes.withUnsafeBytes { write(client, $0.baseAddress, $0.count) }
    }

    func stop() {
        close(listenFD)
        lock.lock()
        for fd in heldClients { close(fd) }
        heldClients.removeAll()
        lock.unlock()
        unlink(path)
    }

    enum Failure: Error { case socket, bind, listen }
}
