"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.join(__dirname, "..");

function fixture(sendMessage = async () => ({pending_accounts: [{account_id: "own_a", label: "Eigenes Konto"}], error: null, running: false})) {
  const docEvents = new Map();
  const windowEvents = new Map();
  const timers = new Map();
  const intervals = [];
  let nextTimer = 0;
  const nodes = {
    status: {textContent: "Verbindung wird geprüft …", dataset: {}},
    account: {value: "", disabled: true, options: [], replaceChildren() {this.options = []; this.value = "";}, append(option) {this.options.push(option); if (!this.value) this.value = option.value;}},
    connect: {disabled: true, addEventListener(name, callback) {this[name] = callback;}}
  };
  const document = {readyState: "loading", getElementById: id => nodes[id], createElement: () => ({}), addEventListener: (name, callback) => docEvents.set(name, callback)};
  const context = vm.createContext({document, browser: {runtime: {sendMessage}},
    addEventListener: (name, callback) => windowEvents.set(name, callback),
    setTimeout: (callback, milliseconds) => {const id = ++nextTimer; timers.set(id, {callback, milliseconds}); return id;},
    clearTimeout: id => timers.delete(id), setInterval: callback => {intervals.push(callback); return intervals.length;}
  });
  // Safari exposes legacy Window names. The popup must not declare conflicting
  // global lexical bindings such as `const status`.
  vm.runInContext("globalThis.window = globalThis; Object.defineProperty(globalThis, 'status', {value: 'legacy', configurable: false});", context);
  const scripts = [...fs.readFileSync(path.join(root, "popup.html"), "utf8").matchAll(/<script src="([^"]+)"/g)].map(match => match[1]);
  for (const script of scripts) vm.runInContext(fs.readFileSync(path.join(root, script), "utf8"), context, {filename: script});
  return {nodes, scripts, events: windowEvents, timers, intervals, loaded() {document.readyState = "interactive"; docEvents.get("DOMContentLoaded")?.();}};
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test("popup HTML boots before its async status request, even with legacy Window.status", async () => {
  const ui = fixture();
  assert.deepEqual(ui.scripts, ["popup-bootstrap.js", "popup.js"]);
  assert.match(ui.nodes.status.textContent, /popup_bootstrap/);
  ui.loaded();
  assert.match(ui.nodes.status.textContent, /popup_requesting/);
  await flush();
  assert.equal(ui.nodes.status.dataset.phase, "ready");
  assert.equal(ui.nodes.account.options.length, 1);
  assert.equal(ui.nodes.connect.disabled, false);
});
test("an unanswered runtime request becomes a fixed visible timeout", async () => {
  const ui = fixture(() => new Promise(() => {}));
  ui.loaded();
  [...ui.timers.values()].find(timer => timer.milliseconds === 8000).callback();
  await flush();
  assert.match(ui.nodes.status.textContent, /\[popup_status_timeout\]/);
  assert.equal(ui.nodes.connect.disabled, true);
});
test("bootstrap exposes script and rejection failures without private details", () => {
  const ui = fixture();
  ui.events.get("error")({message: "private-token-or-message"});
  assert.match(ui.nodes.status.textContent, /popup_script_error/);
  assert.doesNotMatch(ui.nodes.status.textContent, /private-token/);
  ui.events.get("unhandledrejection")({reason: "private-token-or-message"});
  assert.match(ui.nodes.status.textContent, /popup_promise_rejected/);
  assert.doesNotMatch(ui.nodes.status.textContent, /private-token/);
});
test("repeated local refresh does not duplicate account options", async () => {
  const ui = fixture(); ui.loaded(); await flush();
  await ui.intervals[0](); await flush();
  assert.equal(ui.nodes.account.options.length, 1);
  assert.equal(ui.nodes.account.value, "own_a");
});
test("native failures render only a fixed explanatory message and safe code", async () => {
  const ui = fixture(async () => ({pending_accounts: [], error: "invalid_bridge_reply"}));
  ui.loaded(); await flush();
  assert.match(ui.nodes.status.textContent, /\[invalid_bridge_reply\]/);
  assert.equal(ui.nodes.connect.disabled, true);
});
test("a pairing error is not overwritten by the one-second status refresh", async () => {
  const ui = fixture(async message => message.type === "pair" ? {error: {code: "unsafe_tab"}} : {pending_accounts: [{account_id: "own_a"}], error: null});
  ui.loaded(); await flush();
  await ui.nodes.connect.click(); await flush();
  assert.match(ui.nodes.status.textContent, /\[unsafe_tab\]/);
  await ui.intervals[0](); await flush();
  assert.match(ui.nodes.status.textContent, /\[unsafe_tab\]/);
  assert.equal(ui.nodes.connect.disabled, false);
});
