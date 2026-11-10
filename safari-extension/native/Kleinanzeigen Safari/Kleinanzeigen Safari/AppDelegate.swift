import AppKit
import SafariServices
import UniformTypeIdentifiers

@main
enum AppBootstrap {
    static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private let status = NSTextField(wrappingLabelWithString: "Kopplung wird geprüft …")

    func applicationDidFinishLaunching(_ notification: Notification) {
        let title = NSTextField(labelWithString: "Kleinanzeigen mit Safari verbinden")
        title.font = .boldSystemFont(ofSize: 20)
        let explanation = NSTextField(wrappingLabelWithString:
            "Importiere die lokale Kopplungsdatei der Nachrichten-API. Aktiviere danach die Safari-Erweiterung für Kleinanzeigen und ordne jeden Account im jeweiligen Safari-Profil zu. Deine Anmeldung bleibt in Safari.")
        let importButton = NSButton(title: "Kopplungsdatei importieren …", target: self, action: #selector(importConfiguration))
        let preferencesButton = NSButton(title: "Safari-Erweiterungen öffnen", target: self, action: #selector(openPreferences))
        let stack = NSStackView(views: [title, explanation, importButton, preferencesButton, status])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        let content = NSView()
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 28),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -28),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 28),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor, constant: -28)
        ])
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 340),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Kleinanzeigen Safari"
        window.contentView = content
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        refreshConfigurationStatus()
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Kleinanzeigen Safari beenden", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)
        NSApp.mainMenu = menu
    }

    private func refreshConfigurationStatus() {
        do {
            // Read and validate the existing private configuration; never display
            // its endpoint or token and do not contact the backend here.
            _ = try BridgeConfigurationStore.load()
            status.stringValue = "Kopplung vorhanden. Die Safari-Erweiterung kann die gespeicherte Konfiguration verwenden."
        } catch BridgeFailure.missingConfiguration {
            status.stringValue = "Keine lesbare Kopplungsdatei vorhanden. Bitte die lokale Kopplungsdatei importieren."
        } catch {
            status.stringValue = "Die gespeicherte Kopplungsdatei ist ungültig. Bitte erneut importieren."
        }
    }

    @objc private func importConfiguration() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.json]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.message = "Wähle die von der lokalen API erzeugte Safari-Kopplungsdatei."
        panel.beginSheetModal(for: window) { [weak self] result in
            guard result == .OK, let url = panel.url else { return }
            self?.loadConfiguration(from: url)
        }
    }

    private func loadConfiguration(from url: URL) {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        do {
            let size = try url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey])
            guard size.isRegularFile == true, size.isSymbolicLink != true,
                  let count = size.fileSize, count <= 8192 else { throw BridgeFailure.invalidConfiguration }
            let configuration = try BridgeConfiguration.decode(Data(contentsOf: url))
            try BridgeConfigurationStore.save(configuration)
            status.stringValue = "Kopplung gespeichert. Aktiviere die Erweiterung und verbinde den Account im gewünschten Safari-Profil."
        } catch let failure as BridgeFailure {
            status.stringValue = failure.message
        } catch {
            status.stringValue = "Die Kopplungsdatei konnte nicht gelesen oder privat gespeichert werden."
        }
    }

    @objc private func openPreferences() {
        SFSafariApplication.showPreferencesForExtension(withIdentifier: "de.matthias.kleinanzeigen.safari.Extension") { [weak self] error in
            guard error != nil else { return }
            DispatchQueue.main.async { self?.status.stringValue = "Bitte Safari → Einstellungen → Erweiterungen öffnen." }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
