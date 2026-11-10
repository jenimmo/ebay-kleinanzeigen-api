# Safari-Anbindung

Die Web-Erweiterung bedient ausdrücklich zugeordnete Kleinanzeigen-Tabs im normalen Safari. Jedes Kleinanzeigen-Konto erhält ein eigenes Safari-Profil. Anmeldedaten und Cookies bleiben im Browser. Der native Teil ergänzt die von Safari bestätigte Profil-ID und vermittelt ausschließlich zum lokalen Backend.

## Voraussetzungen und Build

- macOS 14 oder neuer, Safari 17 oder neuer und Xcode.
- Ein vorhandenes gültiges Apple-Development-Zertifikat. Im lokalen Projekt ist das bestehende Team `EUXB3Z2QNQ` eingetragen. Xcode erstellt beziehungsweise lädt die benötigten Projekt-Profile für die beiden Bundle-IDs; für diesen Build wurde kein Zertifikat neu angelegt.
- Für ein anderes Team müssen die Signierung und der Appgruppenname gemeinsam angepasst werden.

Vom Projektstamm aus:

```sh
./script/build_and_run.sh --build
./safari-extension/native/test.sh
```

Die native Prüfung umfasst 78 Validierungsfälle und sechs reine Loopback-Transporttests (unter anderem Redirect-Sperre, Größenlimits und den festen nativen Capability-Header). Sie verwendet keine Kleinanzeigen-Anmeldung und sendet keine Nachrichten.

Das signierte Archiv liegt anschließend unter:

`build/safari/Kleinanzeigen Safari.zip`

Nach `--build` liegt die ausführbare App im privaten lokalen Buildcache. Der folgende Pfad enthält die numerische UID des ausführenden Benutzers.

`/private/tmp/kleinanzeigen-safari-build-<UID>/Build/Products/Debug/Kleinanzeigen Safari.app`

Der Cache liegt absichtlich außerhalb des synchronisierten Documents-Ordners: Der macOS-Dateianbieter fügt dort App-Bundles Finder-Metadaten hinzu, die die Code-Signatur ungültig machen. Das ZIP im Projekt erhält die Signatur. Nach erfolgreichem `--install` wird ausschließlich die geprüfte generierte Cache-App entfernt, damit Safari nicht zwei Installationen entdeckt. Die Build-Zwischendateien bleiben erhalten; Xcode erzeugt das App-Produkt beim nächsten Build erneut.

Die geprüfte lokale Installation befindet sich unter:

`~/Applications/Kleinanzeigen Safari.app`

Version **1.0.23, Build 31** unterstützt das Archivieren von Einzelbildern,
Bildgruppen, öffentlichen Anbieterprofilen und Anzeigenimpressen. Die Abnahme
umfasste die Signaturen, die permanente PlugInKit-Registrierung und die
Übereinstimmung der installierten Webquellen mit dem Projekt.

Bei Bildgruppen verlangt die Erweiterung für das ausgewählte Vorschaubild den
bekannten Inline-Marker und die berechneten Rahmenwerte aller vier Seiten.
Damit erkennt sie auch einen in Safari über eine CSS-Variable gesetzten Rahmen.
Nach der Accountprüfung kontrolliert sie die Auswahl erneut. Der Fehler und
die Korrektur wurden in einer isolierten WKWebView reproduziert und mit
Mehrfachanhängen in Safari geprüft.

Führt eine frühere Anzeigenadresse beim späteren Abruf zu einem anderen Ziel,
kennzeichnet die Erweiterung die fehlende Zuordnung mit
`provider_guard=listing_url`. Bereits archivierte Anbieterinformationen bleiben
erhalten. Die unterstützten Angaben zu Bewertungsereignissen erscheinen im
Archiv als eigene Systemereignisse.

Vor einem Ressourcenupdate das Fenster der kleinen Begleit-App schließen.
Sie wird dafür nicht dauerhaft im Dock benötigt. Ein bereits laufender Host
kann nach einem atomaren App-Austausch noch das alte Programmabbild verwenden;
beim beobachteten Verbindungsfehler wurde dies anhand der geladenen Dateien
nachgewiesen. Die erneute Aktivierung der Erweiterung und das Schließen/Öffnen
des Hosts stellten den nativen Kanal wieder her, ohne Safari zu beenden oder
eine erneute Kleinanzeigen-Anmeldung zu verlangen.

Für private Konten liest die Identitätsprüfung die eindeutige `userId` über
den von der Kontoseite verwendeten GET `/m-mein-profil.json` in der aktuellen
Sitzung. Vor dem Sendeklick prüft die Erweiterung den Account erneut. Bei
gewerblichen Konten berücksichtigt sie die Kontoseite im Business-Portal.
Die Abnahme umfasst auch die Erkennung eines leeren Postfachs nach dem Laden
der Nachrichtenoberfläche.

`./script/build_and_run.sh --install` baut und installiert die App in `~/Applications`. App und eingebettete Erweiterung müssen die erwarteten Bundle-IDs, gültige Signaturen und das Projektteam besitzen. Zuerst wird eine vollständige Kopie in einem privaten temporären Ordner neben dem Installationsziel geprüft; anschließend tauscht macOS die beiden Verzeichnisse atomar aus. Ein Fehler vor dem Austausch lässt die bisherige App unverändert. Scheitert die abschließende Prüfung, wird die vorherige vollständige App zurückgetauscht.

`./script/build_and_run.sh` baut, installiert und öffnet die App. Der Build ändert weder Safari-Einstellungen noch die Zulassung unsignierter Erweiterungen. Die lokale Entwicklungs-Signatur ist für diesen Mac vorgesehen. Eine spätere Verteilung benötigt einen eigenen regulären Veröffentlichungs- oder Notarisierungsablauf.

## Icons und Host-Fenster

Die App und die Erweiterungsübersicht verwenden ein eigenes grünes Icon mit
Sprechblase und Archivablage. Die Safari-Symbolleiste erhält eine passende
monochrome Variante mit transparenten Aussparungen. Die editierbaren SVGs,
alle zehn macOS-Icongrößen, die ICNS-Datei und die sieben Web-PNGs liegen unter
[`assets/`](assets/README.md). Das Xcode-Projekt kopiert die Dateien in die
jeweiligen Ressourcen; `CFBundleIconFile` benennt das Host-Icon.

Für ein reines Iconupdate dient der gesonderte Ablauf:

```sh
.venv/bin/python script/update_safari_icons.py --version X.Y.Z --build N
```

Die festgelegten Versionsnummern müssen höher als die installierte Basis sein;
die Manifestversion muss übereinstimmen. Erlaubt sind ausschließlich die festen
Icondateien, ihre Manifestreferenzen, das Host-Iconfeld und die Versionsnummern.
Alle JavaScript-, HTML- und CSS-Dateien müssen bereits bytegleich zur
installierten App sein. Native Programmbytes, Provisioning-Profile und Rechte
bleiben erhalten. Der signierte Kandidat und das ZIP werden geprüft und die
App atomar ersetzt; Safari und Host-App werden dabei weder gestartet noch
beendet. Normale Web- oder native Folgeupdates erhalten die bestehenden Icons;
geänderte Iconbytes benötigen wieder den gesonderten Iconupdateweg.

Das Fenster **Kleinanzeigen Safari** ist nur für Einrichtung und Kopplung nötig.
Nach dem Import der Kopplungsdatei kann es geschlossen werden. Beim Schließen des
letzten Fensters beendet sich die Host-App; ihre bereits gespeicherte Kopplung
bleibt bestehen. Die aktivierte Safari-Erweiterung und deren nativer
Nachrichtenkanal laufen unabhängig von diesem Fenster. Für Live-Zugriffe müssen
Safari und die zugeordneten Kontotabs verfügbar bleiben. Es ist kein dauerhaft
laufendes Host-Dockprogramm nötig. Ein manuell angehefteter Dockeintrag bleibt
als Verknüpfung erhalten. Das lokale Archiv und portable HTML-Exporte können
ohne Safari-Anmeldung gelesen werden.

## Updates bei laufendem Safari

Der Installer verändert keine laufenden Bundle-Dateien einzeln. Die installierte App wird weder über PlugInKit abgemeldet noch erneut registriert; ihre Erkennung bleibt bei macOS. Es werden keine Safari- oder Erweiterungsprozesse beendet und keine Aktivierungsentscheidungen geändert. Fünfzehn isolierte Tests prüfen unter anderem fremde Bundle-IDs, Signatur-/Teamfehler, Symlinks, den tatsächlichen atomaren Austausch, die Rücknahme bei fehlgeschlagener Abschlussprüfung und die begrenzte Cache-Bereinigung.

Xcode registrierte zusätzlich zum installierten Bundle sein gleichnamiges Build-Produkt, wodurch Safari zwei identische Erweiterungen anzeigte. Nach einer erfolgreichen atomaren Installation prüft der Installer deshalb beide Bundle-Identitäten, Team und Signaturen, identische Datei-Inhalte sowie das vollständige ZIP-Archiv. Nur wenn kein eigener Prozess aus der Cache-App läuft, wird deren tatsächlich vorhandener PlugInKit-Eintrag gezielt anhand seines festen Pfads entfernt und anschließend genau diese generierte App gelöscht. Die dauerhafte Installation und ihre Aktivierung bleiben bestehen. `--build` ohne Installation behält sein App-Produkt absichtlich; für die gewöhnliche Aktualisierung ist `--install` vorgesehen.

Safari kann eine neue, von Xcode registrierte Cache-Version noch während des Builds starten. Dann meldet der Installer nach erfolgreichem App-Austausch eine ausstehende Bereinigung und lässt die laufende Cachekopie bestehen. Safari muss in diesem Fall manuell beendet und die Cache-Bereinigung anschließend mit `./script/build_and_run.sh --cleanup-cache` ohne Neubau fortgesetzt werden. Beim beobachteten Versionswechsel hielt PlugInKit außerdem die alte Versionsnummer des permanenten Pfads; dieser konkrete Eintrag wurde einmal mit `pluginkit -a` aktualisiert, ohne vorheriges Abmelden. Das Buildskript führt keine allgemeine Ab-/Anmeldefolge aus.

Die Buildregistrierung stammt aus Xcodes eigenem macOS-Buildschritt `RegisterWithLaunchServices`. Der geprüfte [Swift-Build-Quellcode](https://github.com/swiftlang/swift-build/blob/main/Sources/SWBTaskConstruction/TaskProducers/OtherTaskProducers/ProductPostprocessingTaskProducer.swift) zeigt dafür keinen separaten Abschaltschalter. Die lokale Xcode-Spezifikation `LSRegisterURL.xcspec` bestätigt den Aufruf von `lsregister`. Ein verlässlich unterbrechungsfreies Update bei laufendem Safari wird deshalb nicht zugesichert; das Projekt verändert weder Xcode-Systemdateien noch Safari-Sicherheitsoptionen.

## Web-only-Updates

Wenn ausschließlich die bestehenden Webdateien geändert werden und native
Swift-Funktionen, Manifeststruktur und Berechtigungen unverändert bleiben, kann
`script/update_safari_web.py` die bereits installierte, signaturgeprüfte App als
Basis verwenden. Für den geprüften Stand wurde ausgeführt:

```sh
.venv/bin/python script/update_safari_web.py --version 1.0.15 --build 23
```

Version und Build müssen gegenüber der installierten Basis steigen; die
Manifestversion muss genau übereinstimmen. Ein erneuter Aufruf mit derselben
Version wird vor dem Signieren abgewiesen. Der Ablauf benötigt das vorhandene
Projektzertifikat und dessen weiterhin gültige Provisioning-Profile. Er startet
weder Xcode noch Safari und registriert keine temporäre App.

In einem privaten temporären Ordner werden nur die zehn erlaubten Webdateien und
die beiden Versionsplists geändert. Bereits installierte Icons werden anhand
der festen Siebenerliste geprüft und bytegleich beibehalten. Signiert wird die Erweiterung vor der App,
mit den bestehenden Rechten und Anforderungen. Native Programmbytes werden
abgesehen von der neuen eingebetteten Signatur und deren Größenfeldern
verglichen; Dylibs und Provisioning-Profile müssen vollständig bytegleich
bleiben. Zusätzliche Rechte, Manifeständerungen, fremde Dateien, Native-Code-
Änderungen oder mehrere Plugin-Registrierungen stoppen den Ablauf. Fünfzehn gezielte
Offline-Tests prüfen diese Grenzen; die 15 Installer-Tests prüfen den atomaren
Austausch und die begrenzte Cache-Bereinigung.

Als einzige geprüfte Manifestmigration darf `messaging-read.js` zwischen
`protocol.js` und `content.js` in die bestehende Content-Skriptliste aufgenommen
werden. Andere Reihenfolgen, weitere Skripte, Matches, Hintergrunddefinitionen
oder Rechte bleiben gesperrt. Die erste Ergänzung von neun auf zehn Dateien wird
ebenfalls vor dem atomaren Austausch geprüft.

Nach Signatur- und ZIP-Prüfung wird die permanente App atomar ersetzt. Nur wenn
deren PlugInKit-Versionsmetadaten noch veraltet sind, wird genau dieser Pfad
einmal aktualisiert registriert, ohne vorheriges Abmelden. Temporäre Dateien
werden entfernt. Bereits geladene Safari-Kontexte können dennoch den vorherigen
Webstand halten; ein frisches Dokument und eine aktuelle Brückenantwort müssen
vor weiteren Nachrichten bestätigt werden. Native Änderungen benötigen einen
Xcode-Build; reine Extension-Änderungen können den folgenden engeren Ablauf nutzen.

## Native Extension ohne neue Host-App bauen

Wenn ausschließlich der native Extension-Code geändert wird, verwendet
`script/build_and_run.sh --update-extension --version X.Y.Z --build N` den
geprüften vorhandenen Host als Basis. Der zugehörige Updater baut nur den
abhängigkeitsfreien Xcode-Target **Kleinanzeigen Safari Extension**. Xcode verwendet
die vorhandene automatische Signierkonfiguration ohne Provisioning-Updates;
das erzeugte Profil muss bytegleich zum installierten Profil sein. Die abschließende
Signierung verwendet das festgelegte vorhandene Projektzertifikat. Er erzeugt
keine Host-App über Xcode und vermeidet damit dessen App-Registrierungsschritt.
Ein unerwartetes App-Produkt oder eine zusätzliche Registrierung bricht den
Vorgang ab. Änderungen am Host-Programmcode benötigen weiterhin einen vollständigen
nativen Build.

Die neue Extension wird in eine private Kopie der installierten App eingesetzt.
Host-Programmbytes, Profile, Rechte und Signaturanforderungen bleiben geprüft;
Webdateien stammen aus derselben festen Zehnerliste wie beim Web-only-Update,
ergänzt um die sieben bereits installierten, bytegleich geprüften Icon-PNGs.
Nach Signatur- und ZIP-Prüfung wird die App atomar ersetzt. Es werden keine
Safari-Prozesse beendet, temporären Apps gestartet oder Aktivierungsentscheidungen
geändert. Falls macOS eine private Buildkopie unerwartet registriert oder startet,
bleibt sie zur Prüfung erhalten und wird nicht im laufenden Zustand gelöscht.

Der neue native `image_preview`-Handshake bestätigt begrenzte Vorschau- und
Pixelbelege erst nach ihrer dauerhaften Speicherung im Backend. Der Swift-Code
setzt zusätzlich einen festen HTTP-Header, den JavaScript nicht vorgeben kann.
Vor einem neuen Bildauftrag muss der lokale Bridge-Status nach einem aktuellen
Poll für den betreffenden Account `image_preview_capable: true` zeigen. Ein
fehlender Marker kennzeichnet noch keinen nachgewiesenen neuen Native-Kontext;
frühere unklare Versandaufträge werden dadurch nicht wieder freigegeben.

Das garantiert keinen sofortigen Austausch aller bereits laufenden Safari-Skript- und Native-Messaging-Kontexte. Beim vollständigen Host-Build kann Xcode die zusätzliche Appkopie automatisch entdecken; Safari kann auch nach einem engeren Update ältere Erweiterungsstände noch halten. Nach einem Update müssen daher die lokale Brückenverbindung und ein frisch geladener Dokumentmarker bestätigt werden, bevor neue Nachrichten beauftragt werden. Alte oder unklare Versandoperationen werden dabei nicht erneut ausgelöst. Bleibt die Verbindung trotz erneuter Aktivierung der eigenen Erweiterung hängen, ist ein manueller Safari-Neustart weiterhin eine mögliche Reparatur; das Buildskript führt ihn nicht aus.

Apples regulärer macOS-Updateablauf ist ein erneuter Build. Laut lokaler `pluginkit(8)`-Dokumentation sind explizite Registrierungsänderungen Entwicklungs-/Wartungseingriffe; `-r` entfernt den angegebenen Pfad, während die Auswahl zwischen mehreren Instanzen auch von Version und Registrierungszeit abhängt. Der zuvor verwendete Registerwechsel war deshalb unnötig, ist aber nicht als alleinige Ursache sämtlicher beobachteter Safari-Timeouts bewiesen.

## Einrichten

1. Backend mit `KLEINANZEIGEN_MESSAGING_BROWSER_CHANNEL=safari` starten. Im Normalbetrieb gilt `KLEINANZEIGEN_MESSAGING_TEST_MODE=false`. Für einen eigenen Versandtest den Testmodus aktivieren und die beiden eigenen Accounts sowie die eigene Testanzeige lokal beim Backendstart freigeben.
2. In der App **Kleinanzeigen Safari** die vom Backend erzeugte Datei `data/messaging/safari-pairing.json` importieren. Die Datei enthält einen lokalen Zugangsschlüssel: nicht versenden, nicht in Logs kopieren und nicht ins Versionsarchiv aufnehmen.
3. Die signierte Erweiterung in Safari → Einstellungen → Erweiterungen aktivieren. Websitezugriff ausschließlich für Kleinanzeigen erteilen. Diese Berechtigung ist Voraussetzung für das Lesen und Bedienen der Nachrichtenoberfläche.
4. Für jedes Konto ein eigenes Safari-Profil verwenden und die Erweiterung dort aktivieren. Manuell bei Kleinanzeigen anmelden, `connect_account` für den gewünschten lokalen Account aufrufen und den passenden Tab über das Erweiterungsmenü zuordnen. Die verifizierte Nutzer-ID muss zum Account passen.

Der Import speichert eine zweite private Kopie der Kopplung im macOS-Appgruppencontainer `EUXB3Z2QNQ.de.matthias.kleinanzeigen.safari`, Unterordner `Library/Application Support/KleinanzeigenBridge`, mit Dateirecht 0600 und Verzeichnisrecht 0700. Der tatsächliche Gruppenpfad wird ausschließlich über die macOS-API bestimmt. Bei einem Wechsel von Port oder Datenordner die neu erzeugte Kopplungsdatei erneut importieren.

## Dauerhafter Backendstart und Neustarts

Die Projektkonfigurationen verwenden die feste Adresse
`http://127.0.0.1:50489`, den Safari-Adapter und den Archivordner
`data/messaging/`. Der erste MCP-Werkzeugaufruf verwendet das laufende Backend
oder startet es bei Bedarf. Die dauerhaften Einstellungen und das Verhalten
nach einem Mac-Neustart beschreibt die [Backend-Dokumentation](../README.MD#dauerhafte-safari-konfiguration).

Nach Python-Änderungen muss vor einem geordneten Neustart die reguläre lokale
Wheel-Installation offline aktualisiert werden; ein Test aus dem Projektordner
allein prüft diese installierte Kopie nicht. Der konkrete Befehl und die
Prüfung des echten Entry-Points aus `/private/tmp` stehen unter
[Python-Änderungen übernehmen](../README.MD#python-änderungen-in-die-lokale-mcp-installation-übernehmen).

Bei unverändertem Port und Datenordner bleiben gespeicherter Token und native
Kopplung nutzbar. Account- und Archivdaten bleiben lokal erhalten. Neue
Safari-Tab-IDs werden bei einem ausdrücklichen `connect_account` innerhalb der
bestehenden Profilbindung wiederhergestellt, wenn genau eine bekannte Konto-
oder Nachrichtenseite vorhanden ist und deren frisch geprüfte Nutzer-ID passt.
Bei fehlender oder mehrdeutiger Zuordnung bleibt der manuelle Verbindungsklick
erforderlich; dies ist vom Import einer Kopplungsdatei zu unterscheiden.
Bei der Abnahme mit zwei eigenen Accounts wurden die Zuordnung der Safari-Tabs,
die Paginierung, der Erstkontakt sowie Antworten mit Text und Bild geprüft.
Für eine Bildantwort wurden der vorab gespeicherte Pixelbeleg und das frisch
geladene Plattformbild verglichen. Nach dem Backendneustart ließen sich die
archivierten Nachrichten und JPEG-Bilder über beide MCP-Transporte lesen.
Die [Testdokumentation](../tests/README.md) beschreibt die Offline-Prüfungen
und die Voraussetzungen für den eigenen Live-Test.

## Anbieter- und Profilarchivierung

Beim ersten Seitenabruf eines Chats ergänzt der Safari-Reader die erreichbaren
Anbieterinformationen, das Anzeigenimpressum und das öffentliche Profil der
Gegenpartei. Die zusätzlichen öffentlichen HTML-Abrufe verwenden
`credentials: "omit"`, feste Zeit- und Größenlimits sowie ausschließlich
zugeordnete Kleinanzeigen-URLs. HTML wird in einem inaktiven Template gelesen;
Seitenskripte, Bilder und eingebettete Frames werden dabei nicht ausgeführt
oder nachgeladen.

Eine numerische Profilverknüpfung im Anzeigenkontakt muss zur geprüften
Gesprächspartei passen. Für PRO-Anbieter kann stattdessen eine eindeutig
verknüpfte `/pro/`-Adresse verwendet werden: Ziel-URL und kanonische Profil-URL
müssen exakt übereinstimmen. Zusätzlich müssen die numerischen Kennungen in
den beiden bekannten Komponenten `ProfileActions` und `BrandProfilePage`
dieselbe Gesprächspartei bestätigen. Widersprüchliche numerische Links dürfen
durch eine PRO-Adresse nicht ersetzt werden.

Die unterstützten PRO-Angaben und die Daten des zusätzlichen numerischen
Nutzerprofils werden als begrenzter Profiltext mit jeweils eigener Quell-URL
gesichert. Zu lange Texte, unbekannte Profilstrukturen und ein fehlendes
zusätzliches Profil werden ausdrücklich als unvollständig gekennzeichnet.
Anbieter-Snapshots erhalten ihre Quelle, Abrufzeit und Gegenüber-ID; veränderte
Inhalte werden zusätzlich archiviert. Fehlende oder später gelöschte Seiten
entfernen frühere Snapshots nicht. Bei einer eigenen Anzeige werden deren
Anbieterinformationen nicht dem Käufer zugeschrieben.

## Sicherheits- und Funktionsgrenzen

- Der native Teil akzeptiert ausschließlich die feste Route `/messaging/safari-bridge/exchange` auf einer expliziten IPv4- oder IPv6-Loopback-Adresse und einem konfigurierten Port. Redirects, Cookies und Proxyübernahme sind deaktiviert.
- Der Schlüssel bleibt im nativen Teil. Webseiten und Inhaltsskripte erhalten ihn nicht.
- Die Browser-Profil-ID stammt aus `SFExtensionProfileKey`, nicht aus JavaScript. Das normale Standardprofil wird als `default` erkannt.
- Einzelne Brückenanfragen und Antworten sind auf 64 KiB beschränkt. Medien werden über gesonderte Brückenaktionen in auf 32 KiB begrenzten Teilen übertragen.
- Es findet kein automatischer Vollimport und kein regelmäßiges Abfragen von Nachrichten statt. Brückenabfragen holen nur ausdrücklich beauftragte lokale Arbeit.
- Ein erfolgreich gebautes oder aktiviertes Safari-Plugin ist noch keine bestandene Live-Abnahme des Nachrichtenversands. Text und Bilder dürfen erst nach eindeutiger Prüfung beider Account- und Empfängerzuordnungen getestet werden.

Apple-Referenzen: [Native Messaging und Profil-ID](https://developer.apple.com/documentation/safariservices/messaging-between-the-app-and-javascript-in-a-safari-web-extension), [Erweiterung ausführen](https://developer.apple.com/documentation/safariservices/running-your-safari-web-extension), [Appgruppen im macOS-Sandboxmodell](https://developer.apple.com/documentation/security/accessing-files-from-the-macos-app-sandbox).
