"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const root = path.join(__dirname, "..");

function fixture({loadProtocol = true, loadContent = true, secureRandom = true, url = "https://www.kleinanzeigen.de/portal/anzeigen"} = {}) {
  const listeners = [];
  let networkCalls = 0;
  const window = {}; window.top = window;
  const context = vm.createContext({window, location: new URL(url), crypto: secureRandom ? webcrypto : {},
    document: {querySelector: () => null, querySelectorAll: () => [], getElementById: () => null},
    browser: {runtime: {id: "extension", onMessage: {addListener: listener => listeners.push(listener)}, sendMessage: async () => ({})}},
    setInterval: () => 1, setTimeout, fetch: () => {networkCalls++; throw new Error("not allowed");}
  });
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"));
  assert.equal(manifest.content_scripts[0].js[0], "content-bootstrap.js");
  vm.runInContext(fs.readFileSync(path.join(root, "content-bootstrap.js"), "utf8"), context);
  if (loadProtocol) vm.runInContext(fs.readFileSync(path.join(root, "protocol.js"), "utf8"), context);
  let startupError = false;
  if (loadContent) {
    try { vm.runInContext(fs.readFileSync(path.join(root, "content.js"), "utf8"), context); }
    catch (_) { startupError = true; }
  }
  return {startupError, listeners, networkCalls: () => networkCalls,
    probe(sender = {id: "extension"}) {
      let reply;
      let replies = 0;
      for (const listener of listeners) listener({type: "kleinanzeigen-content-bootstrap"}, sender, value => {reply = value; replies++;});
      return {reply, replies};
    }};
}

test("bootstrap replies synchronously before protocol or command code exists", () => {
  const extension = fixture({loadProtocol: false, loadContent: false});
  const {reply, replies} = extension.probe();
  assert.equal(replies, 1);
  assert.equal(reply.stage, "bootstrap");
  assert.equal(reply.protocol_ready, false);
  assert.equal(reply.secure_random_ready, true);
  assert.equal(reply.handler_ready, false);
  assert.equal(extension.networkCalls(), 0);
});
test("bootstrap distinguishes protocol startup failure from an uninjected script", () => {
  const extension = fixture({loadProtocol: false});
  assert.equal(extension.startupError, true);
  const {reply} = extension.probe();
  assert.equal(reply.stage, "content_started");
  assert.equal(reply.protocol_ready, false);
  assert.equal(reply.handler_ready, false);
});
test("bootstrap isolates missing secure-random API without exposing raw errors", () => {
  const extension = fixture({secureRandom: false});
  assert.equal(extension.startupError, true);
  const {reply} = extension.probe();
  assert.equal(reply.stage, "content_started");
  assert.equal(reply.protocol_ready, true);
  assert.equal(reply.secure_random_ready, false);
  assert.equal(reply.handler_ready, false);
  assert.deepEqual(Object.keys(reply).sort(), ["handler_ready", "protocol_ready", "secure_random_ready", "stage"]);
});
test("a fully initialized content script leaves exactly one bootstrap reply", () => {
  const extension = fixture();
  assert.equal(extension.startupError, false);
  const {reply, replies} = extension.probe();
  assert.equal(replies, 1);
  assert.equal(reply.stage, "handler_ready");
  assert.equal(reply.handler_ready, true);
  assert.equal(extension.networkCalls(), 0);
});
test("bootstrap retains sender and exact-origin guards", () => {
  const extension = fixture();
  const foreign = extension.probe({id: "other-extension"});
  assert.equal(foreign.reply.error.code, "marker_sender_mismatch");
  assert.equal(foreign.reply.stage, undefined);
  const wrongPage = fixture({url: "https://example.org/"}).probe();
  assert.equal(wrongPage.reply.error.code, "unsafe_tab");
  assert.equal(wrongPage.reply.stage, undefined);
});
