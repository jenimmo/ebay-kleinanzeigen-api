import SafariServices

final class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
    func beginRequest(with context: NSExtensionContext) {
        BridgeDiagnostics.once("native_request_received")
        BridgeDiagnostics.once("native_build=bridge-v1")
        func reply(_ payload: [String: Any]) {
            let item = NSExtensionItem()
            var response = payload
            response["native_bridge"] = "bridge-v1"
            item.userInfo = [SFExtensionMessageKey: response]
            context.completeRequest(returningItems: [item], completionHandler: nil)
        }
        guard let item = context.inputItems.first as? NSExtensionItem,
              let message = item.userInfo?[SFExtensionMessageKey] as? [String: Any] else {
            reply(BridgeFailure.invalidMessage.response)
            return
        }
        do {
            let body = try BridgeEnvelope.data(message: message, profile: item.userInfo?[SFExtensionProfileKey] as? UUID)
            let configuration = try BridgeConfigurationStore.load()
            BridgeDiagnostics.once("private_configuration_loaded")
            let exchange = BridgeExchange(completion: reply)
            try exchange.start(configuration: configuration, body: body)
        } catch let failure as BridgeFailure {
            BridgeDiagnostics.once("native_failure=" + failure.code)
            reply(failure.response)
        } catch {
            BridgeDiagnostics.once("native_failure=safari_message_invalid")
            reply(BridgeFailure.invalidMessage.response)
        }
    }
}
