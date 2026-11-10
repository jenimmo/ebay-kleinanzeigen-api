"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const P = require("../protocol.js");
const ORIGIN = "https://www.kleinanzeigen.de";
class Node {
  constructor(tag, attrs = {}, hidden = false) {
    this.tagName = tag.toUpperCase(); this.values = attrs; this.hidden = hidden;
    this.attributes = Object.keys(attrs).map(name => ({name})); this.children = []; this.childNodes = [];
    this.textContent = "PRIVATE NAME AND LISTING TEXT"; this.parentElement = null;
  }
  get href() { return new URL(this.values.href, ORIGIN).href; }
  append(node) {node.parentElement = this; this.children.push(node); return node;}
  descendants() {return this.children.flatMap(node => [node, ...node.descendants()]);}
  checkVisibility() {return !this.hidden && (!this.parentElement || this.parentElement.checkVisibility());}
  getAttribute(name) {return this.values[name] ?? null;}
  hasAttribute(name) {return name in this.values;}
  matches(selectors) {
    return selectors.split(/,\s*/).some(selector => {
      if (selector === "*") return true;
      if (selector.startsWith(".")) return (this.values.class || "").split(/\s+/).includes(selector.slice(1));
      if (selector.startsWith("#")) return this.values.id === selector.slice(1);
      if (selector === "a[href]") return this.tagName === "A" && this.hasAttribute("href");
      if (selector === "iframe[src]") return this.tagName === "IFRAME" && this.hasAttribute("src");
      if (/^\[[a-z-]+\]$/.test(selector)) return this.hasAttribute(selector.slice(1, -1));
      return selector === this.tagName.toLowerCase();
    });
  }
  querySelectorAll(selector) {return this.descendants().filter(node => node.matches(selector));}
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
}
function anchor(href, attrs = {}, hidden = false) {return new Node("a", {href, ...attrs}, hidden);}
function container(links, cls = "userprofile-vip", hidden = false) {
  const root = new Node("div", {class: cls + " PRIVATE-CLASS"}, hidden);
  for (const link of links) root.append(link);
  return root;
}
function fixture(scopes, {global = [], listing = "1234500001", hydrate = null, hydrateAfter = 250, blockedAfter = null} = {}) {
  let listener, requests = 0, elapsed = 0, waits = 0, hydrated = false;
  class ClockDate extends Date {static now() {return Date.now() + elapsed;}}
  const body = new Node("body"), main = body.append(new Node("main"));
  // This existing contact root previously hid the separate seller area from
  // inspection. The helper's seller nodes must still be included first.
  main.append(new Node("form", {id: "viewad-contact-form"}));
  for (const node of [...global, ...scopes]) main.append(node);
  const document = {body, documentElement: body, readyState: "complete", getElementById: () => null,
    querySelectorAll: selector => blockedAfter != null && elapsed >= blockedAfter && selector === "[data-captcha]" ? [new Node("div")] : body.querySelectorAll(selector),
    querySelector: selector => body.querySelector(selector)};
  const window = {}; window.top = window;
  const context = vm.createContext({KleinanzeigenProtocol: P, URL, Date: ClockDate, TextEncoder, TextDecoder, AbortSignal,
    crypto: webcrypto, window, document, location: new URL(ORIGIN + "/s-anzeige/eigene-anzeige/" + listing), performance: {timeOrigin: Date.now()},
    setTimeout: (callback, milliseconds) => {
      elapsed += milliseconds; waits++;
      if (hydrate && !hydrated && elapsed >= hydrateAfter) {hydrated = true; for (const node of hydrate) main.append(node);}
      return setImmediate(callback);
    },
    fetch: async url => {requests++; return {status: 200, ok: true, url, headers: {get: () => "application/json"},
      body: new ReadableStream({start(controller) {controller.enqueue(new TextEncoder().encode('{"userId":"111"}')); controller.close();}})};},
    browser: {runtime: {id: "extension", onMessage: {addListener: value => {listener = value;}}}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "protocol.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8"), context);
  return {requests: () => requests, waiting: () => ({elapsed, waits}), run: (method, target = listing, ttl = 10000) => listener({type: "kleinanzeigen-command", command: {
    id: "listing-check", account_id: "own", user_id: "111", method,
    args: {tab_id: 1, target_kind: "listing", target_id: target}, expires_at: new Date(ClockDate.now() + ttl).toISOString(),
  }}, {id: "extension"})};
}

test("known anchor and known container shapes yield the same uniquely scoped seller", async () => {
  for (const scope of [
    anchor("/s-bestandsliste.html?userId=222", {class: "userprofile-vip PRIVATE-CLASS"}),
    container([anchor("/s-bestandsliste.html?userId=222"), anchor("/s-bestandsliste.html?userId=222")]),
    container([anchor("/s-anzeigen-des-nutzers/222")], "userprofile"),
  ]) {
    const f = fixture([scope], {global: [anchor("/s-bestandsliste.html?userId=999")]});
    const prepared = await f.run("prepare_send");
    assert.equal(prepared.result.recipient_user_id, "222");
    const inspected = await f.run("inspect_ui");
    assert.equal(inspected.result.listing_recipient_verified, true);
    const proof = inspected.result.nodes.filter(node => node.profile_scope && node.userId);
    assert.ok(proof.length >= 1);
    assert.ok(proof.every(node => node.userId === "222"));
    assert.ok(proof.every(node => ["/s-bestandsliste.html", "/s-anzeigen-des-nutzers/222"].includes(node.link_path)));
    assert.doesNotMatch(JSON.stringify(inspected), /PRIVATE|userId=|999/);
    assert.equal(f.requests(), 2); // Fresh identity once per explicitly requested read.
  }
});

test("unscoped, foreign, misleading and ambiguous links never prove a seller", async () => {
  for (const scopes of [
    [], [container([anchor("/s-bestandsliste.html?userId=222")], "unknown-profile")],
    [container([anchor("https://foreign.example/s-bestandsliste.html?userId=222")])],
    [container([anchor("https://name@www.kleinanzeigen.de/s-bestandsliste.html?userId=222")])],
    [container([anchor("/?userId=222")])],
    [container([anchor("/s-bestandsliste.html?userId=222&userId=222")])],
    [container([anchor("/s-bestandsliste.html?userId=222&userId=333"), anchor("/s-bestandsliste.html?userId=222")])],
    [container([anchor("/s-bestandsliste.html?userId=PRIVATE")])],
    [container([anchor("/s-anzeigen-des-nutzers/222?userId=333")])],
    [container([anchor("/s-bestandsliste.html?userId=222"), anchor("/s-bestandsliste.html?userId=333")])],
    [container([anchor("/s-bestandsliste.html?userId=222")]), container([anchor("/s-bestandsliste.html?userId=333")], "userprofile", true)],
    [container([anchor("/s-bestandsliste.html?userId=222")], "userprofile-vip", true)],
    [container([anchor("/s-bestandsliste.html?userId=222", {}, true)])],
  ]) {
    const f = fixture(scopes, {global: [anchor("/s-bestandsliste.html?userId=222")]});
    assert.equal((await f.run("prepare_send")).error.code, "recipient_unverified");
    assert.equal((await f.run("inspect_ui")).result.listing_recipient_verified, false);
  }
});

test("listing mismatch and own-account listings remain forbidden", async () => {
  const f = fixture([container([anchor("/s-bestandsliste.html?userId=111")])]);
  assert.equal((await f.run("prepare_send", "1234567890")).error.code, "listing_mismatch");
  assert.equal((await f.run("prepare_send")).error.code, "own_listing");
});

test("bounded profile scans never promote an incomplete seller scan", async () => {
  for (const scopes of [
    Array.from({length: 21}, () => container([anchor("/s-bestandsliste.html?userId=222")])),
    [container([...Array.from({length: 100}, () => anchor("/hilfe")), anchor("/s-bestandsliste.html?userId=222")])],
  ]) {
    const f = fixture(scopes);
    assert.equal((await f.run("prepare_send")).error.code, "recipient_unverified");
    const inspected = (await f.run("inspect_ui")).result;
    assert.equal(inspected.listing_recipient_verified, false);
    assert.equal(inspected.truncated, true);
    assert.ok(inspected.nodes.length <= 100);
  }
});

test("prepare waits only for bounded local hydration of the known seller scope", async () => {
  const f = fixture([], {hydrate: [container([anchor("/s-bestandsliste.html?userId=222")])]});
  const prepared = await f.run("prepare_send");
  assert.equal(prepared.result.recipient_user_id, "222");
  assert.equal(f.requests(), 1);
  assert.equal(f.waiting().elapsed, 250);
  const unavailable = fixture([]);
  assert.equal((await unavailable.run("prepare_send")).error.code, "recipient_unverified");
  assert.ok(unavailable.waiting().elapsed <= 5000);
  assert.ok(unavailable.waiting().waits <= 40);
  assert.equal(unavailable.requests(), 1);
});

test("conflicts are rejected immediately while expiry and block signals stop hydration waits", async () => {
  for (const links of [
    [anchor("/s-bestandsliste.html?userId=222"), anchor("/s-bestandsliste.html?userId=333")],
    [anchor("/s-bestandsliste.html?userId=222&userId=222")],
  ]) {
    const f = fixture([container(links)]);
    assert.equal((await f.run("prepare_send")).error.code, "recipient_unverified");
    assert.equal(f.waiting().waits, 0);
  }
  const expiring = fixture([]);
  assert.equal((await expiring.run("prepare_send", "1234500001", 250)).error.code, "command_expired");
  assert.ok(expiring.waiting().elapsed <= 250);
  const blocked = fixture([], {blockedAfter: 250});
  assert.equal((await blocked.run("prepare_send")).error.code, "upstream_blocked");
  assert.equal(blocked.waiting().elapsed, 250);
  assert.equal(blocked.requests(), 1);
});
