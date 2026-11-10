"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const P = require("../protocol.js");

function confirmation(overrides = {}) {
  return {id: "read-confirmation", method: "confirm_send", account_id: "own_a", user_id: "12345",
    expires_at: new Date(Date.now() + 10000).toISOString(), args: {tab_id: 7,
      target: {target_kind: "listing", target_id: "1234500001", listing_id: "1234500001", account_user_id: "12345", recipient_user_id: "67890"},
      conversation_id: "c-new", expected_message_id: null, text: "Grüße aus dem eigenen Test",
      not_before: new Date(Date.now() - 2000).toISOString(), baseline_message_ids: [], ...overrides}};
}
function snapshot(messages) {
  return {conversation_id: "c-new", recipient_user_id: "67890", messages: messages || [{message_id: "m-new", sender_user_id: "12345", direction: "outbound", text: "Grüße aus dem eigenen Test", created_at: new Date().toISOString()}]};
}
test("follow hints accept only exact observed inbox routes and unambiguous IDs", () => {
  assert.deepEqual(P.followTarget("https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=c-new&messageId=m-new"), {conversation_id: "c-new", message_id: "m-new"});
  assert.deepEqual(P.followTarget("https://www.kleinanzeigen.de/portal/nachrichten?conversationId=c-new"), {conversation_id: "c-new", message_id: null});
  for (const value of ["https://evil.test/m-nachrichten.html?conversationId=c-new", "http://www.kleinanzeigen.de/m-nachrichten.html?conversationId=c-new", "https://user:secret@www.kleinanzeigen.de/m-nachrichten.html?conversationId=c-new", "https://www.kleinanzeigen.de/m-meine-anzeigen.html?conversationId=c-new", "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=a&conversationId=b", "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=c&messageId=a&messageId=b"])
    assert.equal(P.followTarget(value), null);
});
test("conflicting success links cannot select a conversation or message", () => {
  assert.equal(P.singleFollow([]), null);
  assert.deepEqual(P.singleFollow([{conversation_id: "c", message_id: null}, {conversation_id: "c", message_id: "m"}]), {conversation_id: "c", message_id: "m"});
  assert.throws(() => P.singleFollow([{conversation_id: "c1"}, {conversation_id: "c2"}]), error => error.code === "outcome_unknown");
  assert.throws(() => P.singleFollow([{conversation_id: "c", message_id: "m1"}, {conversation_id: "c", message_id: "m2"}]), error => error.code === "outcome_unknown");
});
test("confirmation navigation is canonical and remains bound to the account's inbox", () => {
  const command = confirmation();
  assert.equal(P.targetURL(command), "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=c-new");
  assert.equal(P.targetURL({...command, identity_page_path: "/portal/anzeigen"}), "https://www.kleinanzeigen.de/portal/nachrichten?conversationId=c-new");
  assert.equal(P.targetMatches("https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=wrong", P.targetURL(command)), false);
  assert.throws(() => P.targetURL(confirmation({conversation_id: "https://evil.test"})), error => error.code === "invalid_command");
});
test("a first-contact receipt requires a precise new matching outbound message", () => {
  const command = confirmation();
  assert.equal(P.confirmationReceipt(snapshot(), command).message_id, "m-new");
  for (const change of [{created_at: null}, {created_at: "2026-11-04"}, {created_at: "2026-11-04T12:30Z"}, {created_at: "2000-01-01T00:00:00Z"}, {text: "Anderer Inhalt"}, {sender_user_id: "67890"}, {direction: "inbound"}]) {
    const original = snapshot();
    original.messages[0] = {...original.messages[0], ...change};
    assert.equal(P.confirmationReceipt(original, command), null);
  }
  assert.equal(P.confirmationReceipt(snapshot(), confirmation({baseline_message_ids: ["m-new"]})), null);
  const withImage = snapshot(); withImage.messages[0].images = [{remote_id: "unexpected-image"}];
  assert.equal(P.confirmationReceipt(withImage, command), null);
});
test("explicit success message IDs still require exact sender content and recipient", () => {
  const current = snapshot(); current.messages[0].created_at = null;
  const command = confirmation({expected_message_id: "m-new"});
  assert.equal(P.confirmationReceipt(current, command).confirmed, true);
  assert.equal(P.confirmationReceipt(current, confirmation({expected_message_id: "other"})), null);
  assert.throws(() => P.confirmationReceipt({...current, recipient_user_id: "99999"}, command), error => error.code === "outcome_unknown");
  assert.throws(() => P.confirmationReceipt({...current, conversation_id: "wrong"}, command), error => error.code === "outcome_unknown");
  const twice = snapshot(); twice.messages.push({...twice.messages[0], message_id: "m-other"});
  assert.throws(() => P.confirmationReceipt(twice, confirmation()), error => error.code === "outcome_unknown");
});

function success(conversationId, messageId = "m-new", {hidden = false, inMessage = false} = {}) {
  return {hidden, checkVisibility: () => !hidden, matches: () => true, textContent: "Nachricht gesendet",
    closest: () => inMessage ? {} : null, parentElement: null,
    getAttribute: name => name === "data-message-id" ? messageId : null,
    querySelectorAll: () => [{getAttribute: name => name === "href" ? "/m-nachrichten.html?conversationId=" + conversationId : null}]};
}
function contentFixture({before = [], after = [], confirm = false, profileUsers = ["12345"], timerMinimum = 0} = {}) {
  let listener, clicks = 0, fetched = 0, composerQueries = 0;
  let elapsed = 0, waits = 0;
  const startedAt = Date.now();
  class Clock extends Date {
    constructor(...args) {super(...(args.length ? args : [startedAt + elapsed]));}
    static now() {return startedAt + elapsed;}
  }
  const text = "Grüße aus dem eigenen Test";
  const window = {}; window.top = window;
  class Editor {
    get value() { return this.text || ""; }
    set value(value) { this.text = value; }
    getAttribute(name) { return name === "maxlength" ? "20000" : null; }
    checkVisibility() { return true; }
    closest() { return form; }
    dispatchEvent() {}
  }
  const editor = new Editor();
  const button = {disabled: false, textContent: "Senden", checkVisibility: () => true, getAttribute: () => null, click: () => {clicks++;}};
  const form = {querySelector: () => null, querySelectorAll: selector => selector.startsWith("button") ? [button] : []};
  const header = {getAttribute: name => name === "data-recipient-user-id" ? "67890" : null, querySelectorAll: () => []};
  const message = {getAttribute: name => ({"data-message-id": "m-new", "data-direction": "outbound", "data-sender-user-id": "12345"}[name] || null),
    querySelector: selector => selector.startsWith('[data-testid="message-text"]') ? {textContent: text} : selector === "time[datetime]" ? {getAttribute: () => new Date().toISOString()} : null,
    querySelectorAll: () => []};
  const scope = {getAttribute: name => name === "data-conversation-id" ? "c-new" : null,
    querySelector: selector => selector.startsWith('[data-testid="conversation-header"]') ? header : null,
    querySelectorAll: selector => selector === '[data-message-id], li[data-testid]' ? [message] : []};
  const ownDocument = {querySelectorAll: () => [], querySelector: () => null,
    getElementById: name => name === "astroSharedData" ? {textContent: '{"user":{"id":"12345"}}'} : null};
  const document = {...ownDocument,
    querySelector: selector => confirm && selector.startsWith('[data-testid="conversation-wrapper"]') ? scope : null,
    querySelectorAll: selector => {
      if (selector.startsWith("#nachricht")) {composerQueries++; return [editor];}
      if (selector === ".userprofile-vip, .userprofile") return [{href: "https://www.kleinanzeigen.de/s-anzeigen-des-nutzers/67890",
        matches: value => [".userprofile-vip", "a[href]"].includes(value), querySelectorAll: () => [], checkVisibility: () => true}];
      if (selector.startsWith('[data-testid="message-send-success"]')) return clicks ? after : before;
      return [];
    }};
  const context = vm.createContext({KleinanzeigenProtocol: timerMinimum ? {...P, assertFresh: command => P.assertFresh(command, Clock.now())} : P,
    URL, Date: timerMinimum ? Clock : Date, TextEncoder, TextDecoder, crypto: webcrypto, window, document,
    location: new URL(confirm ? "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=c-new" : "https://www.kleinanzeigen.de/s-anzeige/1234500001"),
    HTMLTextAreaElement: Editor, Event: class {}, AbortSignal,
    setTimeout: (callback, ms = 0) => {waits++; elapsed += Math.max(ms, timerMinimum); return setImmediate(callback);}, DOMParser: class {parseFromString() {return ownDocument;}},
    fetch: async url => {const userId = profileUsers[Math.min(fetched, profileUsers.length - 1)]; fetched++; return {status: 200, ok: true, url, headers: {get: () => "application/json"}, body: new ReadableStream({start(controller) {controller.enqueue(new TextEncoder().encode(url.endsWith("/m-mein-profil.json") ? JSON.stringify({userId}) : "fixture")); controller.close();}})};},
    browser: {runtime: {id: "extension", onMessage: {addListener: value => {listener = value;}}}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8"), context);
  return {run: command => listener({type: "kleinanzeigen-command", command}, {id: "extension"}), clicks: () => clicks,
    fetched: () => fetched, composerQueries: () => composerQueries, waiting: () => ({elapsed, waits})};
}
function firstSend() {
  const command = confirmation();
  return {...command, method: "send_step", args: {tab_id: 7, kind: "text", target: command.args.target, text: command.args.text}};
}
test("a new success box after one send produces only an unconfirmed follow hint", async () => {
  const fixture = contentFixture({after: [success("c-new")]});
  const reply = await fixture.run(firstSend());
  assert.equal(fixture.clicks(), 1);
  assert.equal(reply.result._follow.conversation_id, "c-new");
  assert.equal(reply.result._follow.message_id, "m-new");
  assert.equal(reply.result.confirmed, undefined);
  assert.equal(reply.result.message_id, undefined);
});
test("stale, hidden, message-body or ambiguous success boxes never confirm or repeat a send", async () => {
  const stale = success("c-old");
  for (const setup of [{before: [stale], after: [stale]}, {after: [success("c-new", "m-new", {hidden: true})]},
    {after: [success("c-new", "m-new", {inMessage: true})]}, {after: [success("c-one"), success("c-two")]}]) {
    const fixture = contentFixture(setup);
    const reply = await fixture.run(firstSend());
    assert.equal(reply.error.code, "outcome_unknown");
    assert.equal(fixture.clicks(), 1);
  }
});
test("the confirmation command checks fresh identity and never accesses a composer", async () => {
  const fixture = contentFixture({confirm: true});
  const reply = await fixture.run(confirmation({expected_message_id: "m-new"}));
  assert.equal(reply.result.confirmed, true);
  assert.equal(reply.result.message_id, "m-new");
  assert.equal(fixture.fetched(), 1);
  assert.equal(fixture.clicks(), 0);
  assert.equal(fixture.composerQueries(), 0);
});
test("read-only DOM confirmation stops at its real deadline with throttled timers", async () => {
  const fixture = contentFixture({confirm: true, timerMinimum: 2000});
  const cmd = confirmation({text: "Different text, no receipt"});
  cmd.expires_at = new Date(Date.now() + 30000).toISOString();
  const reply = await fixture.run(cmd);
  assert.equal(reply.error.code, "outcome_unknown");
  assert.deepEqual(fixture.waiting(), {elapsed: 8000, waits: 4});
  assert.equal(fixture.fetched(), 1);
  assert.equal(fixture.clicks(), 0);
  assert.equal(fixture.composerQueries(), 0);
});
test("a session switch after form preparation prevents the sole send click", async () => {
  const fixture = contentFixture({after: [success("c-new")], profileUsers: ["12345", "99999"]});
  const reply = await fixture.run(firstSend());
  assert.equal(reply.error.code, "account_mismatch");
  assert.equal(fixture.fetched(), 2);
  assert.equal(fixture.clicks(), 0);
});
