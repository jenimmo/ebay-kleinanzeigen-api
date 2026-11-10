/* Minimal synchronous read-only probe, independent of protocol.js and crypto. */
(function () {
  "use strict";
  var root = typeof globalThis !== "undefined" ? globalThis : window;
  var state = {stage: "bootstrap", handlerReady: false};
  root.KleinanzeigenContentBootstrapState = state;
  browser.runtime.onMessage.addListener(function (message, sender, sendResponse) {
    if (!message || message.type !== "kleinanzeigen-content-bootstrap") return undefined;
    if (!sender || sender.id !== browser.runtime.id) {
      sendResponse({error: {code: "marker_sender_mismatch"}});
      return false;
    }
    if (window !== window.top || location.protocol !== "https:" || location.hostname !== "www.kleinanzeigen.de" || (location.port && location.port !== "443")) {
      sendResponse({error: {code: "unsafe_tab"}});
      return false;
    }
    sendResponse({stage: state.stage,
      protocol_ready: !!root.KleinanzeigenProtocol,
      secure_random_ready: !!root.crypto && typeof root.crypto.randomUUID === "function",
      handler_ready: state.handlerReady === true});
    return false;
  });
})();
