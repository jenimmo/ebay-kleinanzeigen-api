import Foundation

@main
struct ExchangeTestClient {
    static func main() throws {
        let semaphore = DispatchSemaphore(value: 0)
        let configuration = BridgeConfiguration(version: 1, endpoint: CommandLine.arguments[1],
                                                token: String(repeating: "x", count: 48))
        let client = BridgeExchange { reply in
            if let error = reply["error"] as? [String: Any], let code = error["code"] as? String {
                print(code)
            } else if reply["ok"] as? Bool == true {
                print("ok")
            } else {
                print("unexpected_reply")
            }
            semaphore.signal()
        }
        let body = try BridgeEnvelope.data(message: ["version": 1, "action": "poll"], profile: nil)
        try client.start(configuration: configuration, body: body)
        guard semaphore.wait(timeout: .now() + 40) == .success else {
            fatalError("Local exchange test timed out")
        }
    }
}
