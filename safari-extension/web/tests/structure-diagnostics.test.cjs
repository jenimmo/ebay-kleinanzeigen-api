"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const P = require("../protocol.js");
class Node {
  constructor(tag, attrs = {}, text = "") {this.tagName = tag.toUpperCase(); this.values = attrs; this.attributes = Object.keys(attrs).map(name => ({name})); this.textContent = text; this.children = []; this.childNodes = []; this.parentElement = null;}
  append(node) {node.parentElement = this; this.children.push(node); return node;}
  descendants() {return this.children.flatMap(node => [node, ...node.descendants()]);}
  getAttribute(name) {return this.values[name] ?? null;}
  hasAttribute(name) {return name in this.values;}
  get href() {return new URL(this.values.href, "https://www.kleinanzeigen.de").href;}
  checkVisibility() {return true;}
  matches(selector) {return selector.split(", ").some(part => {
    if (part === this.tagName.toLowerCase()) return true;
    if (part.startsWith("#")) return this.values.id === part.slice(1);
    const attribute = part.match(/^([a-z]+)?\[([a-z-]+)(?:="([^"]*)")?\]$/);
    return !!attribute && (!attribute[1] || attribute[1] === this.tagName.toLowerCase()) && this.hasAttribute(attribute[2]) &&
      (attribute[3] == null || this.values[attribute[2]] === attribute[3]);
  });}
  closest(selector) {let node = this; while (node) {if (node.matches(selector)) return node; node = node.parentElement;} return null;}
  querySelectorAll(selector) {
    if (selector === "*") return this.descendants();
    if (selector === "[aria-label], [alt]") return this.descendants().filter(node => node.hasAttribute("aria-label") || node.hasAttribute("alt"));
    return this.descendants().filter(node => node.matches(selector));
  }
  querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
}
test("fixed diagnostics find headings and frames outside main without exposing text or frame queries", async () => {
  let listener;
  const body = new Node("body", {}, "Hier geht bald die Post ab Jetzt inserieren und Anfragen erhalten oder nach spannenden Anzeigen suchen und andere Nutzer kontaktieren. PRIVATE-NACHRICHT");
  const main = body.append(new Node("main"));
  body.append(new Node("h1", {}, "Meine Nachrichten"));
  body.append(new Node("iframe", {src: "/portal/nachrichten?token=SECRET", id: "known-frame"}));
  body.append(new Node("iframe", {src: "https://evil.test/private?token=SECRET", id: "foreign-frame"}));
  const shadow = body.append(new Node("div", {id: "message-host"}));
  shadow.shadowRoot = {children: [new Node("div"), new Node("p", {}, "PRIVATE-SHADOW-TEXT")]};
  const own = {querySelector: () => null, querySelectorAll: () => [], getElementById: name => name === "astroSharedData" ? {textContent: '{"user":{"id":"12345"}}'} : null};
  const document = {body, documentElement: body, querySelector: selector => selector === "main" ? main : null, querySelectorAll: selector => body.querySelectorAll(selector)};
  const window = {}; window.top = window;
  const context = vm.createContext({KleinanzeigenProtocol: P, URL, Date, crypto: webcrypto, TextEncoder, TextDecoder, AbortSignal, window, document, location: new URL("https://www.kleinanzeigen.de/portal/nachrichten"),
    DOMParser: class {parseFromString() {return own;}}, fetch: async url => ({ok: true, status: 200, url, body: new ReadableStream({start(controller) {controller.enqueue(new TextEncoder().encode("fixture")); controller.close();}})}),
    browser: {runtime: {id: "extension", onMessage: {addListener: value => {listener = value;}}}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8"), context);
  const inspect = () => listener({type: "kleinanzeigen-command", command: {id: "inspect-fixed", account_id: "own_a", user_id: "12345", method: "inspect_ui", identity_page_path: "/portal/anzeigen",
    args: {tab_id: 7, view: "current"}, expires_at: new Date(Date.now() + 10000).toISOString()}}, {id: "extension"});
  const reply = await inspect();
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"));
  assert.equal(reply.result.content_revision, manifest.version);
  assert.equal(reply.result.empty_heading_in_main, false);
  assert.equal(reply.result.empty_text_in_main, false);
  assert.equal(reply.result.empty_heading_in_document, true);
  assert.equal(reply.result.empty_text_in_document, true);
  assert.equal(reply.result.open_shadow_host_count, 1);
  assert.equal(reply.result.nodes.find(node => node.id === "message-host").shadow_child_count, 2);
  assert.equal(reply.result.nodes.find(node => node.id === "known-frame").frame_path, "/portal/nachrichten");
  assert.equal(reply.result.nodes.find(node => node.id === "foreign-frame").frame_path, undefined);
  assert.doesNotMatch(JSON.stringify(reply), /SECRET|PRIVATE-NACHRICHT|PRIVATE-SHADOW-TEXT|evil\.test|token=/);
  assert.ok(reply.result.nodes.some(node => node.tag === "h1"));
  const intro = "Hier geht bald die Post ab";
  const detail = "Jetzt inserieren und Anfragen erhalten oder nach spannenden Anzeigen suchen und andere Nutzer kontaktieren.";
  body.textContent = intro + " " + detail.replace(/\s/g, "");
  body.innerText = intro + " " + detail;
  body.append(new Node("img", {alt: detail}));
  const stitched = (await inspect()).result.empty_variants;
  assert.equal(stitched.intro_text_content, true);
  assert.equal(stitched.detail_text_content, false);
  assert.equal(stitched.detail_inner_text, true);
  assert.equal(stitched.detail_without_whitespace, true);
  assert.equal(stitched.detail_without_terminal_dot, false);
  assert.equal(stitched.detail_aria_alt, true);
  assert.equal(Object.values(stitched).every(value => typeof value === "boolean"), true);
  body.textContent = intro + " " + detail.slice(0, -1);
  body.innerText = undefined;
  const punctuation = (await inspect()).result.empty_variants;
  assert.equal(punctuation.detail_text_content, false);
  assert.equal(punctuation.detail_without_terminal_dot, true);
  assert.equal(punctuation.detail_without_whitespace, false);
});

test("chat diagnostics prioritize only the existing HEADER listing proof before long message histories", async () => {
  let listener;
  const body = new Node("body"), main = body.append(new Node("main"));
  const wrapper = main.append(new Node("div", {"data-testid": "conversation-wrapper"}));
  const header = wrapper.append(new Node("div")).append(new Node("header", {}, "PRIVATE HEADER TITLE"));
  header.append(new Node("a", {href: "/zur-anzeige/1234500001"}, "PRIVATE LISTING TITLE"));
  header.append(new Node("a", {href: "https://foreign.example/zur-anzeige/9999999999?token=SECRET"}, "PRIVATE FOREIGN"));
  const messages = wrapper.append(new Node("ul"));
  for (let index = 0; index < 120; index++) messages.append(new Node("li", {"data-testid": "message-" + index}));
  const fakeHeader = messages.children[0].append(new Node("header", {id: "message-body-header"}));
  fakeHeader.append(new Node("a", {href: "/zur-anzeige/9999999999"}, "PRIVATE MESSAGE BODY"));
  const own = {querySelector: () => null, querySelectorAll: () => [], getElementById: name => name === "astroSharedData" ? {textContent: '{"user":{"id":"12345"}}'} : null};
  const document = {body, documentElement: body, querySelector: selector => selector === "main" ? main : null, querySelectorAll: selector => body.querySelectorAll(selector)};
  const window = {}; window.top = window;
  const context = vm.createContext({KleinanzeigenProtocol: P, URL, Date, crypto: webcrypto, TextEncoder, TextDecoder, AbortSignal, window, document,
    location: new URL("https://www.kleinanzeigen.de/portal/nachrichten?conversationId=own"),
    DOMParser: class {parseFromString() {return own;}}, fetch: async url => ({ok: true, status: 200, url,
      body: new ReadableStream({start(controller) {controller.enqueue(new TextEncoder().encode("fixture")); controller.close();}})}),
    browser: {runtime: {id: "extension", onMessage: {addListener: value => {listener = value;}}}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8"), context);
  const reply = await listener({type: "kleinanzeigen-command", command: {id: "inspect-header", account_id: "own_a", user_id: "12345", method: "inspect_ui",
    identity_page_path: "/portal/anzeigen", args: {tab_id: 7, view: "current"}, expires_at: new Date(Date.now() + 10000).toISOString()}}, {id: "extension"});
  assert.equal(reply.result.nodes[1].tag, "header");
  assert.equal(reply.result.nodes[2].link_path, "/zur-anzeige/1234500001");
  assert.equal(reply.result.nodes[2].parent_index, 1);
  assert.ok(reply.result.nodes.length <= 100);
  assert.doesNotMatch(JSON.stringify(reply.result.nodes.slice(0, 3)), /9999999999|message-body-header/);
  assert.doesNotMatch(JSON.stringify(reply), /PRIVATE|SECRET|foreign\.example|token=/);
});
