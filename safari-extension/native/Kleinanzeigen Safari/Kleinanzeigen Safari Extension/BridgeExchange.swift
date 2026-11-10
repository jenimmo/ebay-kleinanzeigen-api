import Foundation

/// One bounded local exchange. It neither follows redirects nor stores cookies.
final class BridgeExchange: NSObject, URLSessionDataDelegate, URLSessionTaskDelegate, @unchecked Sendable {
    private let completion: ([String: Any]) -> Void
    private var session: URLSession?
    private var data = Data()
    private var failure: BridgeFailure?
    private var completed = false

    init(completion: @escaping ([String: Any]) -> Void) {
        self.completion = completion
    }

    func start(configuration: BridgeConfiguration, body: Data) throws {
        var request = URLRequest(url: try configuration.validatedURL())
        request.httpMethod = "POST"
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer " + configuration.token, forHTTPHeaderField: "Authorization")
        // Fixed native capability proof, separate from every JavaScript field.
        request.setValue("1", forHTTPHeaderField: "X-Kleinanzeigen-Native-Image-Preview")
        request.httpShouldHandleCookies = false
        request.timeoutInterval = 30
        let settings = URLSessionConfiguration.ephemeral
        settings.httpCookieStorage = nil
        settings.httpShouldSetCookies = false
        settings.urlCache = nil
        settings.requestCachePolicy = .reloadIgnoringLocalCacheData
        settings.connectionProxyDictionary = [:]
        settings.waitsForConnectivity = false
        settings.timeoutIntervalForRequest = 30
        settings.timeoutIntervalForResource = 35
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        let session = URLSession(configuration: settings, delegate: self, delegateQueue: queue)
        self.session = session
        BridgeDiagnostics.once("loopback_request_started")
        session.dataTask(with: request).resume()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        failure = .invalidResponse
        BridgeDiagnostics.once("redirect_rejected")
        completionHandler(nil)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        completionHandler(.cancelAuthenticationChallenge, nil)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                    didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        if let response = response as? HTTPURLResponse {
            BridgeDiagnostics.once("http_status=" + String(response.statusCode))
        }
        guard let response = response as? HTTPURLResponse,
              (200..<300).contains(response.statusCode),
              response.mimeType == "application/json" else {
            failure = .invalidResponse
            completionHandler(.cancel)
            return
        }
        guard response.expectedContentLength <= BridgeEnvelope.maximumBytes else {
            failure = .responseTooLarge
            completionHandler(.cancel)
            return
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive chunk: Data) {
        guard data.count + chunk.count <= BridgeEnvelope.maximumBytes else {
            failure = .responseTooLarge
            dataTask.cancel()
            return
        }
        data.append(chunk)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard !completed else { return }
        completed = true
        defer { session.invalidateAndCancel(); self.session = nil }
        if let failure {
            BridgeDiagnostics.once("transport_failure=" + failure.code)
            completion(failure.response); return
        }
        if let error {
            BridgeDiagnostics.once("url_error_code=" + String((error as NSError).code))
            completion(BridgeFailure.connectionFailed.response); return
        }
        guard let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            completion(BridgeFailure.invalidResponse.response)
            return
        }
        BridgeDiagnostics.once("json_response_error=" + String(result["error"] != nil)
            + ";pending=" + String(result["pending_accounts"] is [Any])
            + ";command=" + String(result.keys.contains("command"))
            + ";echo=" + String(result.keys.contains("echo")))
        completion(result)
    }
}
