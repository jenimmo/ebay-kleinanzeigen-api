(function () {
  "use strict";
  function initialize() {
    const accountSelect = document.getElementById("account");
    const connectButton = document.getElementById("connect");
    const statusNode = document.getElementById("status");
    if (!accountSelect || !connectButton || !statusNode) return;
    let refreshing = false;
    let pairingStarted = false;
    let pairingInFlight = false;
    let pairFailure = null;
    const errors = {
      invalid_bridge_reply: "Die Safari-App liefert keine gültige API-Antwort. Aktualisiere die Safari-Erweiterung und prüfe die lokale Verbindung.",
      config_missing: "Öffne zuerst die App „Kleinanzeigen Safari“ und importiere die lokale Verbindungsdatei.",
      safari_configuration_missing: "Öffne zuerst die App „Kleinanzeigen Safari“ und importiere die lokale Verbindungsdatei.",
      safari_configuration_invalid: "Die Verbindungsdatei ist ungültig. Exportiere sie erneut über die lokale API und importiere sie in der Safari-App.",
      safari_backend_unavailable: "Die lokale API ist nicht erreichbar. Starte sie und öffne dieses Fenster erneut.",
      safari_native_timeout: "Die native Safari-Verbindung antwortet nicht rechtzeitig. Prüfe, ob die aktuelle Erweiterung aktiv ist.",
      safari_tab_missing: "Der zugeordnete Safari-Tab ist nicht mehr verfügbar. Starte connect_account erneut und verbinde den geöffneten Kleinanzeigen-Tab in diesem Profil.",
      safari_backend_response_invalid: "Die lokale API hat die Verbindung nicht bestätigt. Prüfe die konfigurierte API-Adresse.",
      safari_message_invalid: "Safari konnte den lokalen Auftrag nicht zuordnen. Öffne dieses Fenster erneut.",
      safari_message_too_large: "Der lokale Auftrag überschreitet die zulässige Größe.",
      safari_response_too_large: "Die lokale Antwort überschreitet die zulässige Größe.",
      unsafe_tab: "Bitte öffne www.kleinanzeigen.de in diesem Safari-Profil. Die Verbindung öffnet anschließend „Meine Anzeigen“ zur Prüfung.",
      safari_active_tab_unavailable: "Safari konnte den aktiven Tab nicht bestimmen. Öffne einen Kleinanzeigen-Tab in diesem Profil und versuche es erneut.",
      pairing_not_pending: "Starte zuerst die Anmeldung für diesen Account über die lokale API.",
      bridge_unavailable: "Die lokale API ist nicht erreichbar. Starte sie und öffne dieses Fenster erneut.",
      popup_status_timeout: "Die Safari-Erweiterung antwortet noch nicht. Öffne dieses Fenster erneut."
    };
    function errorText(code) {
      const safeCode = typeof code === "string" && /^[a-z_]{1,64}$/.test(code) ? code : "safari_command_failed";
      return (errors[safeCode] || "Die lokale Verbindung ist noch nicht verfügbar. Prüfe die Safari-App und die API.") + " [" + safeCode + "]";
    }
    function show(text, phase) {
      statusNode.textContent = text;
      statusNode.dataset.phase = phase;
    }
    function messageWithTimeout(message) {
      let timer;
      const timeout = new Promise((_, reject) => {
        timer = window.setTimeout(() => {
          const error = new Error("popup_status_timeout");
          error.code = "popup_status_timeout";
          reject(error);
        }, 8000);
      });
      return Promise.race([browser.runtime.sendMessage(message), timeout]).finally(() => window.clearTimeout(timer));
    }
    async function refresh() {
      if (refreshing) return;
      refreshing = true;
      if (statusNode.dataset.phase === "bootstrap" || !statusNode.dataset.phase)
        show("Die lokale API wird abgefragt … [popup_requesting]", "requesting");
      try {
        const state = await messageWithTimeout({type: "status"});
        if (!state || !Array.isArray(state.pending_accounts)) {
          show(errorText("invalid_bridge_reply"), "error");
          accountSelect.disabled = connectButton.disabled = true;
          return;
        }
        if (state.error) {
          show(errorText(state.error), "error");
          accountSelect.disabled = connectButton.disabled = true;
          return;
        }
        const selected = accountSelect.value;
        accountSelect.replaceChildren();
        for (const item of state.pending_accounts) {
          const option = document.createElement("option");
          option.value = item.account_id;
          option.textContent = item.label || item.account_id;
          accountSelect.append(option);
        }
        if ([...accountSelect.options].some(option => option.value === selected)) accountSelect.value = selected;
        const enabled = accountSelect.options.length > 0 && !pairingStarted && !pairingInFlight;
        accountSelect.disabled = connectButton.disabled = !enabled;
        if (pairFailure || state.last_pair_error) {
          show(errorText(pairFailure || state.last_pair_error), "error");
          return;
        }
        if (pairingInFlight || state.pair_state === "checking_tab" || state.pair_state === "pairing") {
          show("Der aktive Kleinanzeigen-Tab wird verbunden …", "requesting");
          return;
        }
        show(pairingStarted ? "Tab ausgewählt. Den Abschluss der Nutzer-ID-Prüfung zeigt die API im Auftragsstatus." :
          state.running ? "Die API prüft gerade den ausgewählten Tab." :
          enabled ? "Wähle den Account, der in diesem Profil angemeldet ist." : "Kein Anmeldeauftrag offen. Starte connect_account über die lokale API.", "ready");
      } catch (error) {
        show(errorText(error?.code), "error");
        accountSelect.disabled = connectButton.disabled = true;
      } finally { refreshing = false; }
    }
    connectButton.addEventListener("click", async () => {
      if (pairingInFlight) return;
      pairingInFlight = true;
      pairFailure = null;
      connectButton.disabled = true;
      show("Der aktive Kleinanzeigen-Tab wird verbunden …", "requesting");
      try {
        const result = await messageWithTimeout({type: "pair", account_id: accountSelect.value});
        pairingStarted = result?.paired === true;
        pairFailure = result?.error?.code || (pairingStarted ? null : "invalid_bridge_reply");
        show(result?.error ? errorText(result.error.code) : pairingStarted ? "Tab ausgewählt. Die API prüft jetzt die angemeldete Nutzer-ID." : errorText("invalid_bridge_reply"), result?.error ? "error" : "ready");
      } catch (error) { pairFailure = error?.code || "safari_command_failed"; show(errorText(pairFailure), "error"); }
      finally { pairingInFlight = false; }
    });
    void refresh();
    // Local status only; no website access.
    window.setInterval(refresh, 1000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, {once: true});
  else initialize();
})();
