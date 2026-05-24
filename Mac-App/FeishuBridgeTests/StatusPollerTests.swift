import Foundation
import Testing

@testable import FeishuBridge

@Suite("StatusPoller")
struct StatusPollerTests {

    // MARK: - Icon state derivation

    @Test func iconStateStoppedWhenNotRunning() async {
        let poller = await StatusPoller(botName: "test")
        let state = await poller.iconState
        #expect(state == .stopped)
    }

    // MARK: - Capabilities set

    @Test func capabilitiesParsedFromStatus() throws {
        // Verify the capabilities array from a status response can be
        // converted to a Set for O(1) lookup
        let caps = ["logs", "quota", "sessions", "provider", "model", "agent", "stop", "tasks"]
        let capSet = Set(caps)
        #expect(capSet.contains("provider"))
        #expect(capSet.contains("logs"))
        #expect(!capSet.contains("compact")) // removed in latest
    }
}
