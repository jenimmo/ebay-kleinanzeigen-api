/* Deliberately separate from popup.js: even a parser/startup failure is visible. */
(function () {
  "use strict";
  function fixedFailure(code) {
    var node = document.getElementById("status");
    if (!node) return;
    node.textContent = "Die Safari-Oberfläche konnte nicht gestartet werden. [" + code + "]";
    node.dataset.phase = "error";
  }
  window.addEventListener("error", function () { fixedFailure("popup_script_error"); });
  window.addEventListener("unhandledrejection", function () { fixedFailure("popup_promise_rejected"); });
  var node = document.getElementById("status");
  if (node) {
    node.textContent = "Safari-Erweiterung startet … [popup_bootstrap]";
    node.dataset.phase = "bootstrap";
  }
  window.setTimeout(function () {
    var current = document.getElementById("status");
    if (current && current.dataset.phase === "bootstrap") fixedFailure("popup_script_not_started");
  }, 4000);
})();
