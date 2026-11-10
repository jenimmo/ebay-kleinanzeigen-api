"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.join(__dirname, "..");
const flush = () => new Promise(resolve => setImmediate(resolve));
function gate() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}

async function fixture({content, marker, nativeResult, nativePreview} = {}) {
  const start = Date.parse("2026-11-04T20:17:00.000Z");
  let now = start, sequence = 0, revision = 0, listener;
  let tabURL = "https://www.kleinanzeigen.de/m-nachrichten.html";
  const timers = new Map(), sent = [], nativeSent = [], storage = {};
  class Clock extends Date {
    constructor(...args) {super(...(args.length ? args : [now]));}
    static now() {return now;}
  }
  const context = vm.createContext({URL, TextEncoder, Date: Clock,
    setTimeout: (callback, ms) => {const id = ++sequence; timers.set(id, {callback, at: now + Math.max(0, ms)}); return id;},
    clearTimeout: id => timers.delete(id),
    browser: {
      runtime: {id: "extension", onMessage: {addListener(fn) {listener = fn;}}, sendNativeMessage: async (_app, message) => {
        nativeSent.push(message);
        if (message.action === "image_preview" && nativePreview) return nativePreview(message);
        if (message.action === "result" && nativeResult) return nativeResult(message);
        return message.action === "poll" ? {command: null, pending_accounts: []} : {accepted: true};
      }},
      storage: {local: {get: async key => ({[key]: storage[key]}), set: async value => Object.assign(storage, value)}},
      tabs: {
        get: async () => ({id: 7, url: tabURL, status: "complete", incognito: false}),
        update: async (_id, update) => {tabURL = update.url; revision++;},
        sendMessage: async (_id, message) => {
          if (message.type === "kleinanzeigen-document-marker") {
            if (marker) return marker();
            const url = new URL(tabURL);
            return {document_id: "doc-" + revision, url: url.origin + url.pathname, ready_state: "complete", navigation_started_at: now};
          }
          if (message.type === "kleinanzeigen-content-bootstrap") return null;
          sent.push(message.command);
          return content ? content(message.command, api) : {result: {account_user_id: "12345", inspected: true}};
        }
      }
    }
  });
  const api = {context, sent, nativeSent, storage, start,
    message: (message, sender = {id: "extension", tab: {id: 7, url: "https://www.kleinanzeigen.de/m-nachrichten.html"}}) => listener(message, sender),
    now: () => now,
    navigate: url => {tabURL = url; revision++;},
    command: (overrides = {}) => ({id: "bounded-command", account_id: "own_a", user_id: "12345", method: "inspect_ui",
      args: {tab_id: 7, view: "current"}, expires_at: new Date(start + 45000).toISOString(), ...overrides}),
    run: (command, method = "execute") => {context.testCommand = command; return vm.runInContext(method + "(testCommand)", context);},
    advance: async milliseconds => {
      const target = now + milliseconds;
      // Advance due timers in deadline order, including tasks scheduled by a wake.
      // No real sleeping, browser, account or network participates in these tests.
      while (true) {
        const next = [...timers].filter(([, t]) => t.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        now = Math.max(now, next[1].at); timers.delete(next[0]); next[1].callback(); await flush();
      }
      now = target; await flush();
    }
  };
  vm.runInContext(fs.readFileSync(path.join(root, "protocol.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(root, "background.js"), "utf8"), context);
  await flush();
  timers.clear(); // The startup idle local poll has completed; tests drive commands directly.
  return api;
}

test("hung content reads stop before bridge expiry and ignore a late successful result", async () => {
  const pending = gate();
  const f = await fixture({content: () => pending.promise});
  const cmd = f.command();
  const result = f.run(cmd).then(value => ({value}), error => ({error}));
  await flush();
  assert.equal(Date.parse(f.sent[0].expires_at), f.start + 40000);
  await f.advance(40000);
  assert.equal((await result).error.code, "safari_content_timeout");
  pending.resolve({result: {confirmed: true}}); await flush();
  assert.equal((await result).value, undefined);
  assert.equal(f.sent.length, 1);
});

test("send content receives the exact shorter deadline; delayed pre-click work cannot click", async () => {
  const pending = gate(); let clicks = 0, lateCode = null;
  const f = await fixture({content: async command => {
    await pending.promise;
    try {
      f.context.delayedCommand = command;
      vm.runInContext("KleinanzeigenProtocol.assertFresh(delayedCommand)", f.context);
      clicks++;
      return {result: {confirmed: true}};
    } catch (error) {lateCode = error.code; throw error;}
  }});
  const cmd = f.command({method: "send_step", args: {tab_id: 7, kind: "text", text: "Grüße\nWelt", target: {target_kind: "conversation", target_id: "c-own"}}});
  const result = f.run(cmd).then(value => ({value}), error => ({error}));
  await flush();
  assert.equal(Date.parse(f.sent[0].expires_at), f.start + 37000);
  assert.equal(cmd.expires_at, new Date(f.start + 45000).toISOString());
  assert.equal(f.storage["dispatch:bounded-command"].state, "attempted");
  await f.advance(37000);
  assert.equal((await result).error.code, "outcome_unknown");
  pending.resolve(); await flush();
  assert.equal(clicks, 0);
  assert.equal(lateCode, "command_expired");
  assert.equal(f.sent.length, 1);
  await assert.rejects(f.run({...cmd, expires_at: new Date(f.now() + 45000).toISOString()}), error => error.code === "outcome_unknown");
  assert.equal(f.sent.length, 1);
});

test("a lost post-click content reply yields only a fresh navigation hint within the reserved window", async () => {
  const pending = gate();
  const f = await fixture({content: (_command, api) => {
    api.navigate("https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=c-own&messageId=m-new");
    return pending.promise;
  }});
  const cmd = f.command({method: "send_step", args: {tab_id: 7, kind: "text", text: "Eigener Test", target: {target_kind: "listing", target_id: "1234500001"}}});
  const result = f.run(cmd);
  await flush(); await f.advance(37000);
  const hint = await result;
  assert.equal(hint._follow.conversation_id, "c-own");
  assert.equal(hint._follow.message_id, "m-new");
  assert.equal(hint._upstream_reached, false);
  assert.equal(hint.confirmed, undefined);
  assert.equal(f.sent.length, 1);
  assert.ok(f.now() <= f.start + 40000);
  pending.resolve({result: {confirmed: true}}); await flush();
  assert.equal(hint.confirmed, undefined);
});

test("document-marker IPC is bounded and a late marker never becomes evidence", async () => {
  const pending = gate();
  const f = await fixture({marker: () => pending.promise});
  f.context.deliveryState = {value: "unset"};
  const result = vm.runInContext("documentMarker(7, deliveryState)", f.context);
  await flush(); await f.advance(2000);
  assert.equal(await result, null);
  assert.equal(f.context.deliveryState.value, "no_reply");
  pending.resolve({document_id: "late-doc", url: "https://www.kleinanzeigen.de/m-nachrichten.html", ready_state: "complete"});
  await flush(); assert.equal(f.context.deliveryState.value, "no_reply");
  assert.equal(f.sent.length, 0);
});

test("native result delivery stops at the original deadline without retry or late acknowledgement", async () => {
  const pending = gate();
  const f = await fixture({nativeResult: () => pending.promise});
  const completion = f.run(f.command(), "deliver");
  await flush();
  assert.equal(f.nativeSent.filter(x => x.action === "result").length, 1);
  // The native RPC keeps its existing 40s cap, even if 45s remain overall.
  await f.advance(40000); await completion;
  assert.equal(vm.runInContext("statusSnapshot().error", f.context), "safari_native_timeout");
  assert.equal(vm.runInContext("statusSnapshot().running", f.context), false);
  pending.resolve({accepted: true}); await flush();
  assert.equal(vm.runInContext("statusSnapshot().error", f.context), "safari_native_timeout");
  assert.equal(f.nativeSent.filter(x => x.action === "result").length, 1);
});

test("late content completion leaves only five seconds for one native result attempt", async () => {
  const page = gate(), native = gate();
  const f = await fixture({content: () => page.promise, nativeResult: () => native.promise});
  const completion = f.run(f.command(), "deliver");
  await flush(); await f.advance(39999);
  page.resolve({result: {account_user_id: "12345", inspected: true}}); await flush();
  const delivered = f.nativeSent.filter(x => x.action === "result");
  assert.equal(delivered.length, 1); assert.equal(delivered[0].result.inspected, true);
  await f.advance(5001); await completion;
  assert.equal(f.now(), f.start + 45000);
  assert.equal(vm.runInContext("statusSnapshot().error", f.context), "safari_native_timeout");
  native.resolve({accepted: true}); await flush();
  assert.equal(f.nativeSent.filter(x => x.action === "result").length, 1);
});

test("a command without reserved time fails before any sending action or journal claim", async () => {
  const f = await fixture();
  const cmd = f.command({method: "send_step", args: {tab_id: 7, kind: "text", target: {target_kind: "conversation", target_id: "c-own"}}, expires_at: new Date(f.start + 8000).toISOString()});
  await assert.rejects(f.run(cmd), error => error.code === "command_expired");
  assert.equal(f.sent.length, 0);
  assert.equal(f.storage["dispatch:bounded-command"], undefined);
});

test("preview persistence is bound to the running image command and exact sender tab", async () => {
  const pending = gate();
  const f = await fixture({content: () => pending.promise});
  const pixels = {algorithm: "srgb-rgba8-v1", width: 16, height: 16, sha256: "a".repeat(64)};
  const preview = {type: "image_preview", command_id: "bounded-command", preview_sha256: "b".repeat(64), image_pixels: pixels};
  const cmd = f.command({method: "send_step", args: {tab_id: 7, kind: "image", target: {target_kind: "conversation", target_id: "c-own"}}});
  const delivery = f.run(cmd, "deliver"); await flush();
  for (const change of [{command_id: "another"}, {preview_sha256: "private"}, {image_pixels: null}, {image_pixels: {...pixels, width: true}}, {image_pixels: {...pixels, extra: "private"}}])
    assert.equal((await f.message({...preview, ...change})).error.code, "invalid_command");
  assert.equal((await f.message(preview, {id: "extension", tab: {id: 8, url: "https://www.kleinanzeigen.de/m-nachrichten.html"}})).error.code, "invalid_command");
  assert.equal(f.message(preview, {id: "foreign", tab: {id: 7, url: "https://www.kleinanzeigen.de/m-nachrichten.html"}}), undefined);
  assert.equal(f.nativeSent.filter(message => message.action === "image_preview").length, 0);
  assert.deepEqual(JSON.parse(JSON.stringify(await f.message(preview))), {result: {accepted: true}});
  const recorded = f.nativeSent.find(message => message.action === "image_preview");
  assert.deepEqual(JSON.parse(JSON.stringify(recorded)), {version: 1, action: "image_preview", command_id: "bounded-command", preview_sha256: "b".repeat(64), image_pixels: pixels});
  pending.resolve({error: {code: "outcome_unknown"}}); await delivery;
  assert.equal((await f.message(preview)).error.code, "invalid_command");
  assert.equal(f.sent.length, 1);
});

test("late durable preview acknowledgement expires at the same shortened content deadline without retry", async () => {
  const content = gate(), acknowledgement = gate();
  const f = await fixture({content: () => content.promise, nativePreview: () => acknowledgement.promise});
  const cmd = f.command({method: "send_step", args: {tab_id: 7, kind: "image", target: {target_kind: "conversation", target_id: "c-own"}}});
  const delivery = f.run(cmd, "deliver"); await flush();
  const response = f.message({type: "image_preview", command_id: cmd.id, preview_sha256: "b".repeat(64),
    image_pixels: {algorithm: "srgb-rgba8-v1", width: 1, height: 1, sha256: "a".repeat(64)}});
  await flush(); await f.advance(37000);
  assert.equal((await response).error.code, "safari_native_timeout");
  await delivery;
  acknowledgement.resolve({accepted: true}); content.resolve({result: {confirmed: true}}); await flush();
  assert.equal(f.nativeSent.filter(message => message.action === "image_preview").length, 1);
  assert.equal(f.nativeSent.filter(message => message.action === "result").length, 1);
  assert.equal(f.sent.length, 1);
});
