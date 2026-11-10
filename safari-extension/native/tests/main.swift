import Foundation

var checks = 0
func check(_ condition: @autoclosure () throws -> Bool, _ label: String) throws {
    guard try condition() else { fatalError("FAILED: " + label) }
    checks += 1
}
func rejects(_ label: String, _ body: () throws -> Void) {
    do { try body(); fatalError("FAILED: " + label) }
    catch { checks += 1 }
}
let path = BridgeConfiguration.exchangePath
let token = String(repeating: "x", count: 48)
for endpoint in ["http://127.0.0.1:50489" + path, "http://[::1]:50489" + path] {
    try check(BridgeConfiguration(version: 1, endpoint: endpoint, token: token).validatedURL().absoluteString == endpoint,
              "literal loopback")
}
for endpoint in [
    "https://127.0.0.1:50489" + path,
    "http://localhost:50489" + path,
    "http://127.0.0.2:50489" + path,
    "http://example.org:50489" + path,
    "http://127.0.0.1:50489/other",
    "http://127.0.0.1:50489" + path + "?token=x",
    "http://127.0.0.1:50489" + path + "#fragment",
    "http://user@127.0.0.1:50489" + path,
    "http://127.0.0.1:0" + path,
    "http://127.0.0.1:65536" + path,
    "http://127.0.0.1:50489/messaging/%73afari-bridge/exchange"
] {
    rejects("unsafe endpoint") { _ = try BridgeConfiguration(version: 1, endpoint: endpoint, token: token).validatedURL() }
}
rejects("weak token") { _ = try BridgeConfiguration(version: 1, endpoint: "http://127.0.0.1:50489" + path, token: "short").validatedURL() }
rejects("configuration version") { _ = try BridgeConfiguration(version: 2, endpoint: "http://127.0.0.1:50489" + path, token: token).validatedURL() }
rejects("huge import") { _ = try BridgeConfiguration.decode(Data(repeating: 65, count: 8193)) }
let profile = UUID(uuidString: "AABBCCDD-0000-1111-2222-334455667788")!
let data = try BridgeEnvelope.data(message: ["version": 1, "action": "poll", "profile_id": "forged", "token": token, "endpoint": "https://example.org"], profile: profile)
let message = try JSONSerialization.jsonObject(with: data) as! [String: Any]
try check(message["profile_id"] as? String == profile.uuidString.lowercased(), "native profile overrides JavaScript")
try check(message["token"] == nil && message["endpoint"] == nil, "configuration absent from forwarded payload")
let normal = try JSONSerialization.jsonObject(with: BridgeEnvelope.data(message: ["version": 1, "action": "poll"], profile: nil)) as! [String: Any]
try check(normal["profile_id"] as? String == "default", "default Safari profile")
rejects("unknown action") { _ = try BridgeEnvelope.data(message: ["version": 1, "action": "execute"], profile: nil) }
rejects("oversized message") { _ = try BridgeEnvelope.data(message: ["version": 1, "action": "result", "result": String(repeating: "x", count: 65536)], profile: nil) }
rejects("non JSON") { _ = try BridgeEnvelope.data(message: ["version": 1, "action": "result", "result": Date()], profile: nil) }

let previewDigest = String(repeating: "a", count: 64)
let pixelDigest = String(repeating: "b", count: 64)
let preview: [String: Any] = [
    "version": 1, "action": "image_preview", "command_id": "command-1", "preview_sha256": previewDigest
]
let pixelProof: [String: Any] = [
    "algorithm": "srgb-rgba8-v1", "width": 1024, "height": 768, "sha256": pixelDigest
]
func encodedPreview(_ value: [String: Any]) throws -> [String: Any] {
    try JSONSerialization.jsonObject(with: BridgeEnvelope.data(message: value, profile: profile)) as! [String: Any]
}
let bytePreview = try encodedPreview(preview)
try check(Set(bytePreview.keys) == Set(["version", "action", "command_id", "preview_sha256", "profile_id"]), "byte-only preview has exact forwarded fields")
try check(bytePreview["preview_sha256"] as? String == previewDigest, "preview digest preserved")
var pixelMessage = preview
pixelMessage["image_pixels"] = pixelProof
pixelMessage["profile_id"] = "forged-profile"
let canonicalPixels = try encodedPreview(pixelMessage)
try check(canonicalPixels["profile_id"] as? String == profile.uuidString.lowercased(), "pixel preview uses native profile")
let forwardedPixels = canonicalPixels["image_pixels"] as! [String: Any]
try check(Set(forwardedPixels.keys) == Set(["algorithm", "width", "height", "sha256"]), "pixel proof has exact fields")
try check(forwardedPixels["width"] as? Int == 1024 && forwardedPixels["height"] as? Int == 768, "pixel dimensions preserved")
try check(forwardedPixels["sha256"] as? String == pixelDigest, "pixel digest preserved separately")

for (field, value) in [
    ("version", true), ("version", 1.5), ("command_id", ""),
    ("command_id", "https://example.org/private"), ("command_id", "command-1\n"),
    ("command_id", String(repeating: "a", count: 257)), ("command_id", 42),
    ("preview_sha256", String(repeating: "A", count: 64)), ("preview_sha256", previewDigest + "\n"),
    ("preview_sha256", String(repeating: "a", count: 63)), ("preview_sha256", NSNull()),
    ("image_pixels", NSNull()), ("image_pixels", []),
    ("url", "https://example.org/private"), ("text", "PRIVATE MESSAGE"),
    ("data", "PRIVATE IMAGE"), ("token", token), ("endpoint", "http://127.0.0.1:1234"),
    ("account_id", "own-account"), ("result", ["accepted": true])
] as [(String, Any)] {
    var invalid = preview; invalid[field] = value
    rejects("invalid preview field " + field) { _ = try encodedPreview(invalid) }
}
for field in ["version", "command_id", "preview_sha256"] {
    var invalid = preview; invalid.removeValue(forKey: field)
    rejects("missing preview field " + field) { _ = try encodedPreview(invalid) }
}
for (field, value) in [
    ("algorithm", "other"), ("algorithm", NSNull()), ("width", true), ("height", false),
    ("width", 0), ("height", -1), ("width", 4_000_001), ("height", 4_000_001),
    ("width", 1.5), ("height", "2"), ("width", Double.nan), ("height", Double.infinity),
    ("width", UInt64.max), ("sha256", String(repeating: "B", count: 64)),
    ("sha256", pixelDigest + "\n"), ("url", "https://example.org/private"), ("data", "PRIVATE IMAGE")
] as [(String, Any)] {
    var pixels = pixelProof; pixels[field] = value
    var invalid = preview; invalid["image_pixels"] = pixels
    rejects("invalid pixel field " + field) { _ = try encodedPreview(invalid) }
}
for field in ["algorithm", "width", "height", "sha256"] {
    var pixels = pixelProof; pixels.removeValue(forKey: field)
    var invalid = preview; invalid["image_pixels"] = pixels
    rejects("missing pixel field " + field) { _ = try encodedPreview(invalid) }
}
for dimensions in [(2000, 2001), (4_000_000, 4_000_000)] {
    var pixels = pixelProof; pixels["width"] = dimensions.0; pixels["height"] = dimensions.1
    var invalid = preview; invalid["image_pixels"] = pixels
    rejects("pixel area exceeds bound") { _ = try encodedPreview(invalid) }
}
for dimensions in [(2000, 2000), (4_000_000, 1), (1, 4_000_000)] {
    var pixels = pixelProof; pixels["width"] = dimensions.0; pixels["height"] = dimensions.1
    var valid = preview; valid["image_pixels"] = pixels
    try check(try encodedPreview(valid)["image_pixels"] != nil, "pixel area boundary accepted")
}
var bridgedPixels = pixelProof
bridgedPixels["width"] = NSNumber(value: 1024.0)
var bridgedPreview = preview; bridgedPreview["image_pixels"] = bridgedPixels
let bridgedData = try BridgeEnvelope.data(message: bridgedPreview, profile: nil)
try check(String(data: bridgedData, encoding: .utf8)!.contains("\"width\":1024"), "integral NSNumber canonicalized to JSON integer")
print("Native bridge validation: \(checks) checks passed")
