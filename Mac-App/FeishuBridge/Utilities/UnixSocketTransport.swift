import Foundation

/// Low-level Unix domain socket transport for line-delimited JSON-RPC.
///
/// Uses POSIX `AF_UNIX`/`SOCK_STREAM` sockets directly instead of
/// `Network.framework`'s `NWConnection`. `NWConnection` only offers a TCP
/// protocol stack for unix endpoints (`using: .tcp`); it functions but layers a
/// TCP state machine over a domain socket and emits spurious
/// `getsockopt TCP_INFO failed` / `Connection has no local endpoint` warnings.
/// A plain POSIX socket matches the bridge's
/// ``socketserver.ThreadingUnixStreamServer`` exactly and keeps failure modes
/// predictable (connect refused, read timeout, EOF).
///
/// Each ``send(_:)`` opens a fresh connection, writes one JSON line, reads one
/// response line (up to the `\n` delimiter), then closes — mirroring the
/// bridge's one-request-per-line handler and avoiding idle sockets during the
/// 2–10 s polling interval.
///
/// Blocking I/O runs on a background queue; the public surface is `async`.
final class UnixSocketTransport: Sendable {

    let sockPath: String
    private let timeout: TimeInterval

    init(sockPath: String, timeout: TimeInterval = 3) {
        self.sockPath = sockPath
        self.timeout = timeout
    }

    // MARK: - Public

    /// Send raw bytes, return the response up to (excluding) the newline delimiter.
    func send(_ data: Data) async throws -> Data {
        let path = sockPath
        let timeout = self.timeout
        return try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    cont.resume(returning: try Self.sendSync(path: path, data: data, timeout: timeout))
                } catch {
                    cont.resume(throwing: error)
                }
            }
        }
    }

    /// Quick connectivity probe — returns true if the socket accepts a connection.
    func probe() async -> Bool {
        let path = sockPath
        let timeout = self.timeout
        return await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                guard let fd = Self.connectSync(path: path, timeout: timeout) else {
                    cont.resume(returning: false)
                    return
                }
                close(fd)
                cont.resume(returning: true)
            }
        }
    }

    // MARK: - POSIX implementation

    /// Open and connect an `AF_UNIX`/`SOCK_STREAM` socket. Returns a connected
    /// fd the caller MUST `close`, or `nil` on any failure.
    private static func connectSync(path: String, timeout: TimeInterval) -> Int32? {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let capacity = MemoryLayout.size(ofValue: addr.sun_path)   // 104 on Darwin
        let pathBytes = Array(path.utf8)
        guard pathBytes.count < capacity else { close(fd); return nil }
        withUnsafeMutablePointer(to: &addr.sun_path) { tuplePtr in
            tuplePtr.withMemoryRebound(to: CChar.self, capacity: capacity) { dst in
                for (i, byte) in pathBytes.enumerated() {
                    dst[i] = CChar(bitPattern: byte)
                }
                dst[pathBytes.count] = 0
            }
        }

        applyTimeouts(fd: fd, timeout: timeout)
        // Convert a write-to-closed-peer SIGPIPE into an EPIPE error instead of
        // killing the process.
        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))

        let rc = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                connect(fd, sa, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard rc == 0 else { close(fd); return nil }
        return fd
    }

    private static func sendSync(path: String, data: Data, timeout: TimeInterval) throws -> Data {
        guard let fd = connectSync(path: path, timeout: timeout) else {
            throw TransportError.connectionFailed
        }
        defer { close(fd) }

        try writeAll(fd: fd, data: data)
        return try readLine(fd: fd)
    }

    /// Apply `SO_RCVTIMEO`/`SO_SNDTIMEO` so a hung peer can never block forever.
    private static func applyTimeouts(fd: Int32, timeout: TimeInterval) {
        var tv = timeval(
            tv_sec: Int(timeout),
            tv_usec: Int32((timeout - floor(timeout)) * 1_000_000)
        )
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    }

    private static func writeAll(fd: Int32, data: Data) throws {
        try data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            guard let base = raw.bindMemory(to: UInt8.self).baseAddress else { return }
            var sent = 0
            while sent < raw.count {
                let n = write(fd, base + sent, raw.count - sent)
                if n > 0 {
                    sent += n
                } else {
                    throw TransportError.writeFailed
                }
            }
        }
    }

    /// Read until the first `\n` and return the bytes before it. A response
    /// without that delimiter (EOF or read error mid-stream) is treated as a
    /// failure rather than a truncated success, so timeouts surface as timeouts.
    private static func readLine(fd: Int32) throws -> Data {
        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 65_536)
        while true {
            let n = chunk.withUnsafeMutableBytes { read(fd, $0.baseAddress, $0.count) }
            if n > 0 {
                buffer.append(contentsOf: chunk[0..<n])
                if let idx = buffer.firstIndex(of: 0x0A) {
                    return Data(buffer[buffer.startIndex..<idx])
                }
                continue
            }
            if n == 0 {
                // EOF before the newline delimiter. The bridge always frames
                // responses with a trailing "\n"; missing it means the response
                // was truncated. NEVER surface partial bytes as success — that
                // masks the failure as a confusing downstream JSON parse error.
                throw buffer.isEmpty
                    ? TransportError.emptyResponse
                    : TransportError.truncatedResponse
            }
            // n < 0: capture errno immediately (before any other libc call).
            let err = errno
            throw (err == EAGAIN || err == EWOULDBLOCK)
                ? TransportError.timedOut
                : TransportError.readFailed
        }
    }

    enum TransportError: LocalizedError, Equatable {
        case connectionFailed
        case writeFailed
        case emptyResponse
        case truncatedResponse
        case timedOut
        case readFailed

        var errorDescription: String? {
            switch self {
            case .connectionFailed: return "Cannot connect to bridge socket"
            case .writeFailed: return "Failed to send request to bridge"
            case .emptyResponse: return "Empty response from bridge"
            case .truncatedResponse: return "Truncated response from bridge"
            case .timedOut: return "Bridge did not respond in time"
            case .readFailed: return "Failed to read response from bridge"
            }
        }
    }
}
