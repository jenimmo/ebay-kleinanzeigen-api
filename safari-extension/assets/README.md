# Icons für Kleinanzeigen Safari

Eigenständiger Entwurf für die lokale Nachrichten- und Archiv-App: eine helle
Sprechblase über einer Archivablage auf einer grünen Kachel. Das Motiv verwendet
keine übernommenen Markenlogos, externen Schriftarten oder Bildvorlagen.

- `AppIcon.svg`: editierbare Vektorvorlage für die App und Erweiterungsübersicht.
- `ToolbarIcon.svg`: monochrome Variante mit transparenten Aussparungen.
- `KleinanzeigenSafari.iconset/`: alle zehn regulären macOS-Icongrößen von
  16 bis 1024 Pixeln.
- `KleinanzeigenSafari.icns`: App-Icon für `Contents/Resources/`.
- `web-icons/`: fünf farbige PNG-Größen sowie Toolbar-PNGs mit 16 und 32 Pixeln.

Die Vorlagen lassen sich mit `python script/build_safari_icons.py` erneut
rasterisieren. Dafür werden `rsvg-convert` und das macOS-Werkzeug `iconutil`
verwendet. Der Aufruf erzeugt ausschließlich die lokalen Bilddateien; er baut,
signiert oder installiert keine App.

Das Xcode-Projekt kopiert die ICNS-Datei in die Host-App und die sieben PNGs in
die Ressourcen der Safari-Erweiterung. Die Manifestpfade beziehen sich auf die
gepackten Erweiterungsressourcen. `CFBundleIconFile` benennt das Host-Icon.

`script/update_safari_icons.py --version VERSION --build BUILD` ist der gesonderte
Updateweg für bereits installierte Apps. Er akzeptiert nur die festgelegten
Icons, deren Manifestreferenzen und höhere Versionsnummern. Alle JavaScript-
und nativen Programmbytes, vorhandene Profile, Signierrechte und Sitzungen
bleiben erhalten. Der überprüfte Kandidat wird innen nach außen signiert und
atomar installiert. Der Updater startet oder beendet weder Safari noch die
Host-App und registriert keine temporäre zweite App.

Versionsnummern müssen vor diesem Update ausdrücklich festgelegt werden. Normale
Web- und native Folgeupdates erhalten die installierten Icons; neue Iconbytes
werden ausschließlich über den gesonderten Iconupdateweg übernommen.
