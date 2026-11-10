import Foundation
import CoreFoundation
import OSLog

/// Only fixed event names, numeric transport codes and schema flags are logged.
/// Never pass a request, response, URL, token or error description here.
enum BridgeDiagnostics {
    private static let logger = Logger(subsystem: "de.matthias.kleinanzeigen.safari", category: "NativeBridge")
    private static let lock = NSLock()
    private static var seen = Set<String>()

    static func once(_ fixedEvent: String) {
        lock.lock()
        let inserted = seen.count < 64 && seen.insert(fixedEvent).inserted
        lock.unlock()
        if inserted { logger.info("\(fixedEvent, privacy: .public)") }
    }
}

/// Local pairing data. Never accepts a server-selected endpoint or a redirect.
struct BridgeConfiguration: Codable {
    static let groupIdentifier = "EUXB3Z2QNQ.de.matthias.kleinanzeigen.safari"
    static let exchangePath = "/messaging/safari-bridge/exchange"
    let version: Int
    let endpoint: String
    let token: String

    func validatedURL() throws -> URL {
        guard version == 1,
              token.range(of: "^[A-Za-z0-9_-]{32,256}$", options: .regularExpression) != nil,
              let components = URLComponents(string: endpoint),
              components.scheme == "http", ["127.0.0.1", "[::1]"].contains(components.host ?? ""),
              let port = components.port, (1...65535).contains(port),
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil,
              components.percentEncodedPath == Self.exchangePath,
              let url = components.url,
              url.absoluteString == endpoint else {
            throw BridgeFailure.invalidConfiguration
        }
        return url
    }

    static func decode(_ data: Data) throws -> Self {
        guard data.count <= 8192 else { throw BridgeFailure.invalidConfiguration }
        let config = try JSONDecoder().decode(Self.self, from: data)
        _ = try config.validatedURL()
        return config
    }
}

enum BridgeFailure: Error {
    case invalidConfiguration, missingConfiguration, invalidMessage, messageTooLarge
    case connectionFailed, invalidResponse, responseTooLarge

    var code: String {
        switch self {
        case .invalidConfiguration: return "safari_configuration_invalid"
        case .missingConfiguration: return "safari_configuration_missing"
        case .invalidMessage: return "safari_message_invalid"
        case .messageTooLarge: return "safari_message_too_large"
        case .connectionFailed: return "safari_backend_unavailable"
        case .invalidResponse: return "safari_backend_response_invalid"
        case .responseTooLarge: return "safari_response_too_large"
        }
    }

    var message: String {
        switch self {
        case .invalidConfiguration: return "Die lokale Kopplungsdatei ist ungültig."
        case .missingConfiguration: return "Kopplungsdatei zuerst in der App Kleinanzeigen Safari importieren."
        case .invalidMessage: return "Ungültiger Brückenauftrag."
        case .messageTooLarge, .responseTooLarge: return "Der Brückenauftrag überschreitet die Größenbegrenzung."
        case .connectionFailed: return "Das lokale Nachrichten-Backend ist nicht erreichbar."
        case .invalidResponse: return "Das lokale Backend lieferte keine gültige Brückenantwort."
        }
    }

    var response: [String: Any] { ["error": ["code": code, "message": message]] }
}

enum BridgeConfigurationStore {
    static func directory() throws -> URL {
        guard let container = FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier: BridgeConfiguration.groupIdentifier
        ) else { throw BridgeFailure.missingConfiguration }
        return container.appendingPathComponent("Library/Application Support/KleinanzeigenBridge", isDirectory: true)
    }

    static func load() throws -> BridgeConfiguration {
        do {
            let file = try directory().appendingPathComponent("pairing.json")
            let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
            guard attributes[.type] as? FileAttributeType == .typeRegular,
                  let size = attributes[.size] as? NSNumber, size.intValue <= 8192 else {
                throw BridgeFailure.invalidConfiguration
            }
            return try BridgeConfiguration.decode(Data(contentsOf: file))
        } catch let failure as BridgeFailure { throw failure }
        catch { throw BridgeFailure.missingConfiguration }
    }

    static func save(_ configuration: BridgeConfiguration) throws {
        _ = try configuration.validatedURL()
        let manager = FileManager.default
        let directory = try directory()
        try manager.createDirectory(at: directory, withIntermediateDirectories: true,
                                    attributes: [.posixPermissions: 0o700])
        try manager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        let temporary = directory.appendingPathComponent(UUID().uuidString + ".tmp")
        let destination = directory.appendingPathComponent("pairing.json")
        let data = try JSONEncoder().encode(configuration)
        guard manager.createFile(atPath: temporary.path, contents: data,
                                 attributes: [.posixPermissions: 0o600]) else {
            throw BridgeFailure.missingConfiguration
        }
        defer { try? manager.removeItem(at: temporary) }
        // Atomic rename inside the same private directory, preserving mode 0600.
        guard rename(temporary.path, destination.path) == 0 else {
            throw BridgeFailure.missingConfiguration
        }
    }
}

enum BridgeEnvelope {
    static let maximumBytes = 64 * 1024

    private static func matches(_ value: Any?, pattern: String) -> String? {
        guard let text = value as? String,
              let range = text.range(of: pattern, options: .regularExpression),
              range == text.startIndex..<text.endIndex else { return nil }
        return text
    }

    private static func boundedInteger(_ value: Any?, maximum: Int) -> Int? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        // Safari bridges JavaScript numbers as NSNumber. Canonicalize integral
        // values to JSON integers; never accept booleans, fractions or nonfinite values.
        let scalar = number.doubleValue
        guard scalar.isFinite, scalar >= 1, scalar <= Double(maximum),
              let integer = Int(exactly: scalar) else { return nil }
        return integer
    }

    private static func imagePreview(_ message: [String: Any]) throws -> [String: Any] {
        let allowed: Set<String> = ["version", "action", "command_id", "preview_sha256", "image_pixels", "profile_id"]
        guard Set(message.keys).isSubset(of: allowed),
              boundedInteger(message["version"], maximum: 1) == 1,
              let command = matches(message["command_id"], pattern: "^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"),
              let preview = matches(message["preview_sha256"], pattern: "^[a-f0-9]{64}$") else {
            throw BridgeFailure.invalidMessage
        }
        var result: [String: Any] = [
            "version": 1, "action": "image_preview", "command_id": command, "preview_sha256": preview
        ]
        if let raw = message["image_pixels"] {
            guard let pixels = raw as? [String: Any],
                  Set(pixels.keys) == Set(["algorithm", "width", "height", "sha256"]),
                  pixels["algorithm"] as? String == "srgb-rgba8-v1",
                  let width = boundedInteger(pixels["width"], maximum: 4_000_000),
                  let height = boundedInteger(pixels["height"], maximum: 4_000_000),
                  width <= 4_000_000 / height,
                  let digest = matches(pixels["sha256"], pattern: "^[a-f0-9]{64}$") else {
                throw BridgeFailure.invalidMessage
            }
            result["image_pixels"] = [
                "algorithm": "srgb-rgba8-v1", "width": width, "height": height, "sha256": digest
            ]
        }
        return result
    }

    static func data(message: [String: Any], profile: UUID?) throws -> Data {
        guard message["version"] as? Int == 1,
              let action = message["action"] as? String,
              ["poll", "pair", "result", "image_chunk", "archive_image_chunk", "image_preview"].contains(action) else {
            throw BridgeFailure.invalidMessage
        }
        var payload = action == "image_preview" ? try imagePreview(message) : message
        // Safari is the authority for this value; JavaScript cannot choose it.
        payload["profile_id"] = profile?.uuidString.lowercased() ?? "default"
        payload.removeValue(forKey: "token")
        payload.removeValue(forKey: "endpoint")
        guard JSONSerialization.isValidJSONObject(payload) else {
            throw BridgeFailure.invalidMessage
        }
        let data = try JSONSerialization.data(withJSONObject: payload)
        guard data.count <= maximumBytes else { throw BridgeFailure.messageTooLarge }
        return data
    }
}
