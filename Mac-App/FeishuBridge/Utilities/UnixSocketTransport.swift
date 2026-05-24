import Foundation
import Network

/// Low-level Unix socket transport for line-delimited JSON-RPC.
///
/// Each ``send(_:)`` call opens a fresh connection, writes one JSON line,
/// reads the response line, then tears down.  This matches the bridge's
/// ``socketserver`` model (one handler per connection) and avoids holding
/// idle sockets during the 2–10 s polling interval.
final class UnixSocketTransport: Sendable {

    let sockPath: String

    init(sockPath: String) {
        self.sockPath = sockPath
    }

    // MARK: - Public

    /// Send raw bytes, return raw response bytes (one newline-terminated JSON line).
    func send(_ data: Data) async throws -> Data {
        let endpoint = NWEndpoint.unix(path: sockPath)
        let connection = NWConnection(to: endpoint, using: .tcp)

        return try await withCheckedThrowingContinuation { cont in
            let guard_ = _SendGuard(cont)
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    connection.send(
                        content: data,
                        contentContext: .defaultMessage,
                        isComplete: false,
                        completion: .contentProcessed { sendError in
                            if let sendError {
                                connection.cancel()
                                guard_.resume(throwing: sendError)
                                return
                            }
                            // Accumulate reads until we see a newline
                            Self.readUntilNewline(connection: connection, buffer: Data()) { result in
                                connection.cancel()
                                switch result {
                                case .success(let data):
                                    guard_.resume(returning: data)
                                case .failure(let error):
                                    guard_.resume(throwing: error)
                                }
                            }
                        }
                    )

                case .failed(let error):
                    connection.cancel()
                    guard_.resume(throwing: error)

                default:
                    break
                }
            }
            connection.start(queue: .global(qos: .userInitiated))
        }
    }

    /// Recursively read chunks until a newline delimiter is found.
    private static func readUntilNewline(
        connection: NWConnection,
        buffer: Data,
        completion: @escaping @Sendable (Result<Data, Error>) -> Void
    ) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65_536) { chunk, _, isComplete, error in
            if let error {
                completion(.failure(error))
                return
            }
            var accumulated = buffer
            if let chunk { accumulated.append(chunk) }

            // Check for newline delimiter
            if accumulated.contains(0x0A) {
                // Trim trailing newline
                if let idx = accumulated.firstIndex(of: 0x0A) {
                    accumulated = accumulated[accumulated.startIndex..<idx]
                }
                completion(.success(accumulated))
            } else if isComplete {
                // Server closed — use whatever we have
                if accumulated.isEmpty {
                    completion(.failure(TransportError.emptyResponse))
                } else {
                    completion(.success(accumulated))
                }
            } else {
                // Need more data
                readUntilNewline(connection: connection, buffer: accumulated, completion: completion)
            }
        }
    }

    /// Quick connectivity probe — returns true if the socket accepts a connection.
    func probe() async -> Bool {
        let endpoint = NWEndpoint.unix(path: sockPath)
        let connection = NWConnection(to: endpoint, using: .tcp)

        return await withCheckedContinuation { cont in
            // Use a lock-protected flag to guarantee single-resume.
            let guard_ = _ProbeGuard(cont)
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    connection.cancel()
                    guard_.resume(returning: true)
                case .failed, .cancelled:
                    connection.cancel()
                    guard_.resume(returning: false)
                default:
                    break
                }
            }
            connection.start(queue: .global(qos: .userInitiated))

            // Timeout after 2 seconds
            DispatchQueue.global().asyncAfter(deadline: .now() + 2) {
                connection.cancel()
                guard_.resume(returning: false)
            }
        }
    }

    enum TransportError: LocalizedError {
        case emptyResponse

        var errorDescription: String? {
            switch self {
            case .emptyResponse: return "Empty response from bridge"
            }
        }
    }
}

/// Sendable one-shot continuation guard — ensures exactly one resume.
private final class _ProbeGuard: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Bool, Never>?

    init(_ continuation: CheckedContinuation<Bool, Never>) {
        self.continuation = continuation
    }

    func resume(returning value: Bool) {
        lock.lock()
        let cont = continuation
        continuation = nil
        lock.unlock()
        cont?.resume(returning: value)
    }
}

/// Sendable one-shot guard for throwing continuations.
private final class _SendGuard: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Data, Error>?

    init(_ continuation: CheckedContinuation<Data, Error>) {
        self.continuation = continuation
    }

    func resume(returning value: Data) {
        lock.lock()
        let cont = continuation
        continuation = nil
        lock.unlock()
        cont?.resume(returning: value)
    }

    func resume(throwing error: Error) {
        lock.lock()
        let cont = continuation
        continuation = nil
        lock.unlock()
        cont?.resume(throwing: error)
    }
}
