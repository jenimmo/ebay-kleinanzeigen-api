"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const P = require("../protocol.js");
const root = path.join(__dirname, "..");
function command(overrides = {}) {
  return {id: "command-1", account_id: "own_a", user_id: "12345", method: "get_conversation", args: {tab_id: 7, conversation_id: "conversation-1"}, expires_at: new Date(Date.now() + 30000).toISOString(), ...overrides};
}

test("only exact HTTPS site origin can be controlled", () => {
  assert.equal(P.originAllowed("https://www.kleinanzeigen.de/m-nachrichten.html"), true);
  for (const url of ["http://www.kleinanzeigen.de/", "https://www.kleinanzeigen.de.evil.test/", "https://user:pass@www.kleinanzeigen.de/", "https://login.kleinanzeigen.de/", "file:///tmp/test", "https://www.kleinanzeigen.de:444/"])
    assert.equal(P.originAllowed(url), false);
});
test("unknown commands, missing account and expired commands fail closed", () => {
  for (const entry of [command({method: "evaluate"}), command({account_id: "../wrong"}), command({user_id: null}), command({expires_at: "no"}), command({expires_at: new Date(0).toISOString()}), command({args: {tab_id: "7"}})])
    assert.throws(() => P.validate(entry));
});
test("navigation accepts only fixed inbox or numeric listing target", () => {
  assert.equal(P.targetURL(command()), "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=conversation-1");
  assert.equal(P.targetURL(command({method: "identify", user_id: null})), "https://www.kleinanzeigen.de/m-meine-anzeigen.html");
  assert.equal(P.targetURL(command({method: "prepare_send", args: {tab_id: 7, target_kind: "listing", target_id: "1234500001-84-3771"}})), "https://www.kleinanzeigen.de/s-anzeige/1234500001-84-3771");
  assert.throws(() => P.targetURL(command({method: "prepare_send", args: {tab_id: 7, target_kind: "listing", target_id: "https://evil.test"}})));
});

function background(storage = {}, {staleDocument = false, tabStatus = "complete", readyState = "complete", markerMissing = false, markerFailure = null, missingTab = false, navigationAfterSend = null, nativeHandler = async () => ({command: null, pending_accounts: []}), tabQuery = async () => [{id: 7, url: "https://www.kleinanzeigen.de/m-nachrichten.html"}]} = {}) {
  let dispatched = 0;
  const sentCommands = [];
  const timers = new Map();
  let timerSequence = 0;
  let revision = 0;
  let tabURL = "https://www.kleinanzeigen.de/m-nachrichten.html";
  let listener;
  const context = vm.createContext({KleinanzeigenProtocol: P, TextEncoder, Date, URL, setTimeout: (callback, ms) => {
    const id = ++timerSequence;
    timers.set(id, {callback, ms});
    if (ms === 100) setImmediate(callback);
    return id;
  }, clearTimeout: id => timers.delete(id), browser: {
    runtime: {id: "extension", sendNativeMessage: nativeHandler, onMessage: {addListener: value => { listener = value; }}},
    storage: {local: {get: async key => ({[key]: storage[key]}), set: async value => Object.assign(storage, value)}},
    tabs: {query: tabQuery, get: async () => {if (missingTab) throw new Error("private Safari details"); return {url: tabURL, status: tabStatus};}, update: async (_id, update) => {tabURL = update.url; if (!staleDocument) revision++;}, sendMessage: async (_id, message) => {
      if (message.type === "kleinanzeigen-content-bootstrap") return {stage: "handler_ready", protocol_ready: true, secure_random_ready: true, handler_ready: true};
      if (message.type === "kleinanzeigen-document-marker") {
        if (markerFailure === "send_rejected") throw new Error("private-runtime-details");
        if (markerFailure === "sender_rejected") return {error: {code: "marker_sender_mismatch"}};
        if (markerFailure === "handler_error") return {error: {code: "safari_command_failed"}};
        const markerURL = new URL(tabURL);
        return markerMissing ? undefined : {document_id: "doc-" + revision, ready_state: readyState, url: markerURL.origin + markerURL.pathname, navigation_started_at: Date.now()};
      }
      dispatched++; sentCommands.push(message.command);
      if (navigationAfterSend && message.command.method === "send_step") {
        tabURL = navigationAfterSend; revision++;
        throw new Error("The old content world was destroyed");
      }
      return {result: {message_id: "m1", navigation_verified: message.command.fresh_navigation?.document_id === "doc-" + revision}};
    }}
  }});
  vm.runInContext(fs.readFileSync(path.join(root, "background.js"), "utf8"), context);
  return {context, dispatched: () => dispatched, listener, sentCommands, timers};
}
test("duplicate send command is durably blocked across background restart", async () => {
  const storage = {};
  const first = background(storage);
  const send = command({method: "send_step", args: {tab_id: 7, kind: "text", text: "Grüße", target: {target_kind: "conversation", target_id: "c1"}}});
  first.context.commandUnderTest = send;
  await vm.runInContext("execute(commandUnderTest)", first.context);
  assert.equal(first.dispatched(), 1);
  assert.equal(storage["dispatch:command-1"].state, "attempted");
  const restarted = background(storage);
  restarted.context.commandUnderTest = send;
  await assert.rejects(vm.runInContext("execute(commandUnderTest)", restarted.context), error => error.code === "outcome_unknown");
  assert.equal(restarted.dispatched(), 0);
});
test("website content cannot pair a profile or trigger arbitrary commands", () => {
  const bg = background();
  assert.equal(bg.listener({type: "pair", account_id: "own_a"}, {id: "extension", tab: {id: 7, url: "https://www.kleinanzeigen.de/"}}), undefined);
  assert.equal(bg.listener({type: "pair", account_id: "own_a"}, {id: "other-extension"}), undefined);
});
test("image chunks are bound to the one running image command and its exact Safari tab", async () => {
  const requests = [];
  const bg = background({}, {nativeHandler: async (_app, request) => {
    if (request.action === "image_chunk") {requests.push(request); return {data_base64: "AA==", offset: 0};}
    return {command: null, pending_accounts: []};
  }});
  bg.context.bound = command({method: "send_step", args: {tab_id: 7, kind: "image"}});
  vm.runInContext("runningCommand = bound", bg.context);
  const message = {type: "image_chunk", command_id: "command-1", offset: 0};
  const sender = {id: "extension", tab: {id: 7, url: "https://www.kleinanzeigen.de/m-nachrichten.html"}};
  assert.equal((await bg.listener(message, sender)).result.data_base64, "AA==");
  assert.equal((await bg.listener({...message, command_id: "other"}, sender)).error.code, "invalid_command");
  assert.equal((await bg.listener(message, {...sender, tab: {...sender.tab, id: 8}})).error.code, "invalid_command");
  assert.equal(bg.listener(message, {...sender, id: "foreign-extension"}), undefined);
  vm.runInContext("runningCommand.args.kind = 'text'", bg.context);
  assert.equal((await bg.listener(message, sender)).error.code, "invalid_command");
  assert.equal(requests.length, 1);
});
test("an unavailable bound tab requires re-pairing and never chooses another tab", async () => {
  let queries = 0;
  const fixture = background({}, {missingTab: true, tabQuery: async () => {queries++; return [];}});
  fixture.context.commandUnderTest = command();
  await assert.rejects(vm.runInContext("execute(commandUnderTest)", fixture.context), error => error.code === "safari_tab_missing");
  assert.equal(fixture.dispatched(), 0);
  assert.equal(queries, 0);
});
test("content revision and bounded proof phase survive background forwarding on non-identity errors", async () => {
  const results = [];
  const fixture = background({}, {nativeHandler: async (_app, message) => {
    if (message.action === "result") results.push(message);
    return {command: null, pending_accounts: []};
  }});
  const original = fixture.context.browser.tabs.sendMessage;
  fixture.context.browser.tabs.sendMessage = async (tab, message) => message.type === "kleinanzeigen-command" ?
    {error: {code: "reauth_required", content_revision: "1.0.6", identity_structure: {legacy_proof_stage: "fresh_own"}}} : original(tab, message);
  fixture.context.commandUnderTest = command();
  await vm.runInContext("deliver(commandUnderTest)", fixture.context);
  assert.equal(results.length, 1);
  assert.equal(results[0].error.code, "reauth_required");
  assert.equal(results[0].error.content_revision, "1.0.6");
  assert.equal(results[0].error.identity_structure.legacy_proof_stage, "fresh_own");
});

test("background transports authored reader diagnostics without arbitrary error fields", async () => {
  const sent = [], diagnostic = {operation: "list", stage: "list_meta", reason: "page_number", meta_values: {pageNum: 1}};
  const fixture = background({}, {nativeHandler: async (_app, message) => {
    if (message.action === "result") sent.push(message);
    return {command: null, pending_accounts: []};
  }});
  const original = fixture.context.browser.tabs.sendMessage;
  fixture.context.browser.tabs.sendMessage = async (tab, message) => message.type === "kleinanzeigen-command" ?
    {error: {code: "ui_unsupported", reader_structure: diagnostic, message: "PRIVATE", body: "PRIVATE"}} : original(tab, message);
  fixture.context.commandUnderTest = command({method: "list_conversations", args: {tab_id: 7, limit: 10}});
  await vm.runInContext("deliver(commandUnderTest)", fixture.context);
  assert.equal(sent.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0].error.reader_structure)), diagnostic);
  assert.doesNotMatch(JSON.stringify(sent), /PRIVATE/);
});
test("background preserves fixed image-guard diagnostics without arbitrary error fields", async () => {
  const results = [], diagnostic = {stage: "preview_digest", data_mime: "webp", preview_digest_matches: false};
  const fixture = background({}, {nativeHandler: async (_app, message) => {
    if (message.action === "result") results.push(message);
    return {command: null, pending_accounts: []};
  }});
  const original = fixture.context.browser.tabs.sendMessage;
  fixture.context.browser.tabs.sendMessage = async (tab, message) => message.type === "kleinanzeigen-command" ?
    {error: {code: "outcome_unknown", image_structure: diagnostic, body: "PRIVATE", message: "PRIVATE"}} : original(tab, message);
  fixture.context.commandUnderTest = command();
  await vm.runInContext("deliver(commandUnderTest)", fixture.context);
  assert.equal(results.length, 1);
  assert.equal(results[0].error.code, "outcome_unknown");
  assert.deepEqual(JSON.parse(JSON.stringify(results[0].error.image_structure)), diagnostic);
  assert.doesNotMatch(JSON.stringify(results), /PRIVATE/);
});
test("a first-contact navigation recovers only a read hint and never repeats dispatch", async () => {
  const storage = {};
  const fixture = background(storage, {navigationAfterSend: "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=c-new&messageId=m-new"});
  const send = command({method: "send_step", args: {tab_id: 7, kind: "text", text: "Eigener Test", target: {target_kind: "listing", target_id: "1234500001"}}});
  fixture.context.commandUnderTest = send;
  const result = await vm.runInContext("execute(commandUnderTest)", fixture.context);
  assert.equal(result._follow.conversation_id, "c-new");
  assert.equal(result._follow.message_id, "m-new");
  assert.equal(result.message_id, undefined);
  assert.equal(result._upstream_reached, false);
  assert.equal(fixture.dispatched(), 1);
  await assert.rejects(vm.runInContext("execute(commandUnderTest)", fixture.context), error => error.code === "outcome_unknown");
  assert.equal(fixture.dispatched(), 1);
});
test("broken sending content on an unrelated navigation remains unknown without a read hint", async () => {
  const fixture = background({}, {navigationAfterSend: "https://evil.test/m-nachrichten.html?conversationId=c-new"});
  fixture.context.commandUnderTest = command({method: "send_step", args: {tab_id: 7, kind: "text", target: {target_kind: "listing", target_id: "1234500001"}}});
  await assert.rejects(vm.runInContext("execute(commandUnderTest)", fixture.context), error => error.code === "outcome_unknown");
  assert.equal(fixture.dispatched(), 1);
});
test("read-only confirmation navigates with an exact conversation ID without claiming another dispatch", async () => {
  const storage = {};
  const fixture = background(storage);
  fixture.context.commandUnderTest = command({method: "confirm_send", args: {tab_id: 7,
    target: {target_kind: "listing", target_id: "1234500001", listing_id: "1234500001", account_user_id: "12345", recipient_user_id: "67890"},
    conversation_id: "c-new", expected_message_id: "m-new", text: "Grüße", not_before: new Date().toISOString(), baseline_message_ids: []}});
  const result = await vm.runInContext("execute(commandUnderTest)", fixture.context);
  assert.equal(result.navigation_verified, true);
  assert.equal(fixture.sentCommands[0].method, "confirm_send");
  assert.equal(Object.keys(storage).some(key => key.startsWith("dispatch:")), false);
});

function content({url = "https://www.kleinanzeigen.de/m-nachrichten.html", currentUser = "12345", profileUser = currentUser === null ? "12345" : currentUser, httpStatus = 200, challenges = [], selectorNodes = {}, queryNodes = {}, nextData = null} = {}) {
  let listener;
  let fetched = 0;
  let intervalCount = 0;
  const fetchedURLs = [];
  const ownDocument = {querySelectorAll: selector => selectorNodes[selector] || [], querySelector: () => null,
    getElementById: name => name === "astroSharedData" && currentUser !== null ? {textContent: JSON.stringify({user: {id: currentUser}})} : name === "__NEXT_DATA__" && nextData !== null ? {textContent: JSON.stringify(nextData)} : null};
  const document = {...ownDocument, readyState: "complete", querySelector: selector => queryNodes[selector] || null,
    querySelectorAll: selector => selector === "[data-captcha]" ? challenges.map(shown => ({hidden: !shown, checkVisibility: () => shown})) : ownDocument.querySelectorAll(selector)};
  const window = {}; window.top = window;
  const location = new URL(url);
  const context = vm.createContext({KleinanzeigenProtocol: P, URL, TextEncoder, TextDecoder, Date, window, location, document, crypto: webcrypto, performance: {timeOrigin: Date.now()}, setInterval: () => ++intervalCount, setTimeout: callback => setImmediate(callback),
    AbortSignal, DOMParser: class {parseFromString() { return ownDocument; }},
    fetch: async fetchURL => { fetched++; fetchedURLs.push(fetchURL); return {status: httpStatus, ok: httpStatus >= 200 && httpStatus < 300, url: fetchURL, headers: {get: () => "application/json"}, body: new ReadableStream({start(controller) { controller.enqueue(new TextEncoder().encode(fetchURL.endsWith("/m-mein-profil.json") ? JSON.stringify({userId: profileUser}) : "fixture")); controller.close(); }})}; },
    browser: {runtime: {id: "extension", sendMessage: async () => {}, onMessage: {addListener: value => { listener = value; }}}}
  });
  vm.runInContext(fs.readFileSync(path.join(root, "content.js"), "utf8"), context);
  return {run: (cmd, sender = {id: "extension"}) => listener({type: "kleinanzeigen-command", command: cmd}, sender), marker: (sender = {id: "extension"}) => listener({type: "kleinanzeigen-document-marker"}, sender), fetched: () => fetched, intervalCount: () => intervalCount, fetchedURLs};
}
test("fresh identity mismatch blocks access before touching recipient/composer", async () => {
  const fixture = content({currentUser: "67890"});
  const response = await fixture.run(command({method: "send_step", args: {tab_id: 7, kind: "text"}}));
  assert.equal(response.error.code, "account_mismatch");
  assert.equal(fixture.fetched(), 1);
});
test("foreign page cannot fetch identity or execute a message command", async () => {
  const fixture = content({url: "https://evil.test/m-nachrichten.html"});
  const response = await fixture.run(command());
  assert.equal(response.error.code, "unsafe_tab");
  assert.equal(fixture.fetched(), 0);
});
test("unknown image semantics fail explicitly before any upload", async () => {
  const fixture = content();
  const response = await fixture.run(command({method: "send_step", args: {tab_id: 7, kind: "image"}}));
  assert.equal(response.error.code, "images_unsupported");
});
test("initial binding requires user's own account page and never uses a seller page", async () => {
  const fixture = content({url: "https://www.kleinanzeigen.de/s-bestandsliste.html?userId=12345"});
  const response = await fixture.run(command({method: "identify", user_id: null}));
  assert.equal(response.error.code, "identity_page_required");
  assert.equal(fixture.fetched(), 0);
});
test("identity-only DOM check never claims a fresh network result", async () => {
  const fixture = content({url: "https://www.kleinanzeigen.de/portal/anzeigen"});
  const response = await fixture.run(command({method: "identify", user_id: null}));
  assert.equal(response.result.user_id, "12345");
  assert.equal(response.result._upstream_reached, false);
  assert.equal(fixture.fetched(), 0);
});
test("private own-page markers agree with a fresh profile response", async () => {
  const element = attributes => ({getAttribute: name => attributes[name] ?? null});
  const cases = [
    {selectorNodes: {'[data-current-user-id], [data-logged-in-user-id], meta[name="current-user-id"], meta[name="user-id"]': [element({content: "12345"})]}},
    {selectorNodes: {'#user-menu [data-user-id], #my-menu [data-user-id], [data-testid="my-profile"][data-user-id]': [element({"data-user-id": "12345"})]}},
    {selectorNodes: {'#user-menu a[href], #my-menu a[href], a[data-testid="my-profile"]': [element({href: "/s-bestandsliste.html?userId=12345"})]}},
    {selectorNodes: {'#user-menu a[href], #my-menu a[href], a[data-testid="my-profile"]': [element({href: "https://www.kleinanzeigen.de/s-anzeigen-des-nutzers/12345"})]}},
    {nextData: {props: {pageProps: {currentUser: {id: "12345"}, seller: {id: "99999"}}}}}
  ];
  for (const source of cases) {
    const fixture = content({url: "https://www.kleinanzeigen.de/m-meine-anzeigen.html", currentUser: null, ...source});
    const reply = await fixture.run(command({method: "identify", user_id: null}));
    assert.equal(reply.result.user_id, "12345");
    assert.equal(fixture.fetched(), 1);
  }
});
test("unscoped seller links and foreign header links never bind a private account", async () => {
  const element = href => ({getAttribute: name => name === "href" ? href : null});
  for (const selectorNodes of [
    {'a[href*="s-bestandsliste.html"], a[href*="s-anzeigen-des-nutzers/"]': [element("/s-bestandsliste.html?userId=12345")]},
    {'#user-menu a[href], #my-menu a[href], a[data-testid="my-profile"]': [element("https://evil.test/s-anzeigen-des-nutzers/12345")]}
  ]) {
    const fixture = content({url: "https://www.kleinanzeigen.de/m-meine-anzeigen.html", currentUser: null, profileUser: null, selectorNodes});
    const reply = await fixture.run(command({method: "identify", user_id: null}));
    assert.equal(reply.error.code, "identity_unverified");
  }
  const seller = content({url: "https://www.kleinanzeigen.de/s-anzeige/eine-anzeige/1234500001", nextData: {props: {pageProps: {currentUser: {id: "12345"}}}}});
  assert.equal((await seller.run(command({method: "identify", user_id: null}))).error.code, "identity_page_required");
});
test("conflicting current-account sources fail closed before account binding", async () => {
  const fixture = content({url: "https://www.kleinanzeigen.de/m-meine-anzeigen.html", nextData: {props: {pageProps: {currentUser: {id: "67890"}}}}});
  const reply = await fixture.run(command({method: "identify", user_id: null}));
  assert.equal(reply.error.code, "identity_unverified");
  assert.equal(reply.result, undefined);
});
test("real upstream blocking and rate limiting retain their distinct circuit signals", async () => {
  for (const [httpStatus, expected] of [[403, "upstream_blocked"], [429, "upstream_rate_limited"]]) {
    const fixture = content({httpStatus});
    const response = await fixture.run(command({method: "send_step", args: {tab_id: 7, kind: "text"}}));
    assert.equal(response.error.code, expected);
    assert.equal(fixture.fetched(), 1);
  }
});
test("diagnostic listing inspection still rejects arbitrary navigation URLs", () => {
  assert.equal(P.targetURL(command({method: "inspect_ui", args: {tab_id: 7, view: "listing", target_kind: "listing", target_id: "1234500001"}})), "https://www.kleinanzeigen.de/s-anzeige/1234500001");
  assert.throws(() => P.targetURL(command({method: "inspect_ui", args: {tab_id: 7, view: "listing", target_kind: "listing", target_id: "1234500001?token=secret"}})));
});
test("fresh navigation is confirmed by a new content document before any command executes", async () => {
  const fixture = background();
  fixture.context.commandUnderTest = command({method: "identify", user_id: null});
  const result = await vm.runInContext("execute(commandUnderTest)", fixture.context);
  assert.equal(result.navigation_verified, true);
  assert.equal(fixture.dispatched(), 1);
});
test("a stale completed Safari document cannot masquerade as successful navigation", async () => {
  const fixture = background({}, {staleDocument: true});
  fixture.context.commandUnderTest = command({method: "identify", user_id: null, expires_at: new Date(Date.now() + 5025).toISOString()});
  await assert.rejects(vm.runInContext("execute(commandUnderTest)", fixture.context), error => {
    assert.equal(error.code, "safari_navigation_timeout");
    assert.equal(error.navigation_state.tab_complete, true);
    assert.equal(error.navigation_state.target_matched, true);
    assert.equal(error.navigation_state.marker_received, true);
    assert.equal(error.navigation_state.marker_ready, true);
    assert.equal(error.navigation_state.fresh_document, false);
    return true;
  });
  assert.equal(fixture.dispatched(), 0);
});
test("identify claims upstream reachability only for its exact newly verified document", async () => {
  const fixture = content({url: "https://www.kleinanzeigen.de/portal/anzeigen"});
  const marker = await fixture.marker();
  const verified = await fixture.run(command({method: "identify", user_id: null, fresh_navigation: {document_id: marker.document_id, path: "/portal/anzeigen"}}));
  assert.equal(verified.result._upstream_reached, true);
  const stale = await fixture.run(command({method: "identify", user_id: null, fresh_navigation: {document_id: "old-document", path: "/portal/anzeigen"}}));
  assert.equal(stale.result._upstream_reached, false);
});
test("unknown upstream pagination remains explicitly incomplete with has_more true", () => {
  const paging = P.pagination({next_cursor: null, has_more: false}, true);
  assert.equal(paging.has_more, true);
  assert.equal(paging.complete, false);
  assert.equal(paging.next_cursor, null);
  assert.equal(paging.upstream_has_more, true);
  assert.match(paging.pagination_limitation, /noch nicht verifiziert/);
});
test("visible CAPTCHA blocks while hidden login widgets do not", async () => {
  const hidden = content({url: "https://www.kleinanzeigen.de/m-meine-anzeigen.html", challenges: [false, false]});
  assert.equal((await hidden.run(command({method: "identify", user_id: null}))).result.user_id, "12345");
  const visible = content({url: "https://www.kleinanzeigen.de/m-meine-anzeigen.html", challenges: [false, true]});
  assert.equal((await visible.run(command({method: "identify", user_id: null}))).error.code, "upstream_blocked");
  assert.equal(visible.fetched(), 0);
});
test("popup status waits for the startup poll instead of exposing an empty cache", async () => {
  let release;
  let calls = 0;
  const reply = new Promise(resolve => {release = resolve;});
  const bg = background({}, {nativeHandler: async () => {calls++; return reply;}});
  let finished = false;
  const status = bg.listener({type: "status"}, {id: "extension"}).then(value => {finished = true; return value;});
  await Promise.resolve();
  assert.equal(finished, false);
  assert.equal(calls, 1);
  release({command: null, pending_accounts: [{account_id: "own_a", label: "Eigenes Konto"}]});
  const state = await status;
  assert.equal(state.pending_accounts[0].account_id, "own_a");
  assert.equal(state.error, null);
  assert.equal(calls, 1);
});
test("native echo dictionaries and malformed poll envelopes are explicit errors", async () => {
  for (const reply of [{echo: {action: "poll"}}, {}, {command: null}, {command: null, pending_accounts: null}, {command: [], pending_accounts: []}]) {
    const bg = background({}, {nativeHandler: async () => reply});
    const state = await bg.listener({type: "status"}, {id: "extension"});
    assert.equal(state.error, "invalid_bridge_reply");
    assert.equal(state.pending_accounts.length, 0);
  }
});
test("subsequent popup status fetches fresh pending accounts", async () => {
  let calls = 0;
  const bg = background({}, {nativeHandler: async () => ({command: null, pending_accounts: ++calls === 1 ? [] : [{account_id: "own_b"}]})});
  const first = await bg.listener({type: "status"}, {id: "extension"});
  assert.equal(first.pending_accounts.length, 0);
  const second = await bg.listener({type: "status"}, {id: "extension"});
  assert.equal(second.pending_accounts[0].account_id, "own_b");
  assert.equal(calls, 2);
});
test("current inspection preserves a manually opened conversation without navigation", () => {
  assert.equal(P.targetURL(command({method: "inspect_ui"})), null);
  assert.equal(P.targetURL(command({method: "inspect_ui", args: {tab_id: 7, view: "current"}})), null);
  assert.equal(P.targetURL(command({method: "inspect_ui", args: {tab_id: 7, view: "inbox"}})), "https://www.kleinanzeigen.de/m-nachrichten.html");
});
test("pairing failures before native dispatch remain visible across later polls", async () => {
  let pairCalls = 0;
  const bg = background({}, {
    nativeHandler: async (_app, request) => {if (request.action === "pair") pairCalls++; return {command: null, pending_accounts: [{account_id: "own_a"}]};},
    tabQuery: async () => [{id: 7, url: "https://example.org/"}]
  });
  await bg.listener({type: "status"}, {id: "extension"});
  const pair = await bg.listener({type: "pair", account_id: "own_a"}, {id: "extension"});
  assert.equal(pair.error.code, "unsafe_tab");
  const status = await bg.listener({type: "status"}, {id: "extension"});
  assert.equal(status.last_pair_error, "unsafe_tab");
  assert.equal(status.pair_state, "failed");
  assert.equal(pairCalls, 0);
});
test("only the observed business own-page redirect is accepted", () => {
  const own = "https://www.kleinanzeigen.de/m-meine-anzeigen.html";
  assert.equal(P.targetMatches("https://www.kleinanzeigen.de/portal/anzeigen", own), true);
  assert.equal(P.targetMatches("https://www.kleinanzeigen.de/portal/einstellungen", own), false);
  assert.equal(P.targetMatches("https://evil.test/portal/anzeigen", own), false);
  assert.equal(P.targetMatches("https://www.kleinanzeigen.de/portal/anzeigen", "https://www.kleinanzeigen.de/m-nachrichten.html"), false);
});
test("business account identity is read only on its observed own page", async () => {
  const fixture = content({url: "https://www.kleinanzeigen.de/portal/anzeigen"});
  const result = await fixture.run(command({method: "identify", user_id: null}));
  assert.equal(result.result.user_id, "12345");
  assert.equal(result.result.identity_page_path, "/portal/anzeigen");
  assert.equal(result.result.capabilities.text_status, "unverified");
  assert.equal(result.result.capabilities.images_status, "unverified");
});
test("stored business routes are used only for the same verified account identity", async () => {
  const state = {"identity-route:own_a": {user_id: "12345", path: "/portal/anzeigen"}};
  const fixture = background(state);
  fixture.context.commandUnderTest = command();
  await vm.runInContext("execute(commandUnderTest)", fixture.context);
  assert.equal(fixture.sentCommands[0].identity_page_path, "/portal/anzeigen");
  state["identity-route:own_a"].user_id = "67890";
  await vm.runInContext("execute(commandUnderTest)", fixture.context);
  assert.equal(fixture.sentCommands[1].identity_page_path, "/m-meine-anzeigen.html");
});
test("business identity checks fetch only the exact stored own route", async () => {
  const fixture = content({url: "https://www.kleinanzeigen.de/portal/nachrichten"});
  await fixture.run(command({method: "send_step", identity_page_path: "/portal/anzeigen", args: {tab_id: 7, kind: "image"}}));
  assert.deepEqual(fixture.fetchedURLs, ["https://www.kleinanzeigen.de/portal/anzeigen"]);
  const unsafe = await fixture.run(command({identity_page_path: "/s-bestandsliste.html?userId=12345"}));
  assert.equal(unsafe.error.code, "invalid_command");
  assert.equal(fixture.fetchedURLs.length, 1);
});
test("unknown identity diagnostics contain keys and types without their private values", async () => {
  const fixture = content({url: "https://www.kleinanzeigen.de/portal/anzeigen", currentUser: "private@example.invalid"});
  const result = await fixture.run(command({method: "identify", user_id: null}));
  assert.equal(result.error.code, "identity_unverified");
  assert.equal(result.error.identity_structure.page_path, "/portal/anzeigen");
  assert.equal(result.error.identity_structure.astro_present, true);
  assert.equal(result.error.identity_structure.sources[0].type, "object");
  assert.deepEqual(Array.from(result.error.identity_structure.sources[0].keys), ["id"]);
  assert.doesNotMatch(JSON.stringify(result), /private@example/);
});
test("legacy own-page identity diagnostics expose bounded header counts and known JSON shape only", async () => {
  const privateValue = "do-not-expose@example.invalid";
  const fixture = content({url: "https://www.kleinanzeigen.de/m-meine-anzeigen.html", currentUser: null, profileUser: null,
    nextData: {props: {pageProps: {currentUser: {id: privateValue, userId: "not-a-number", email: privateValue, accessToken: "secret-token"}}}},
    selectorNodes: {
      '#user-menu [data-user-id], #my-menu [data-user-id], [data-testid="my-profile"][data-user-id]': Array.from({length: 125}, () => ({getAttribute: () => privateValue})),
      '#user-menu a[href], #my-menu a[href], a[data-testid="my-profile"]': [{getAttribute: () => "https://evil.test/private?token=secret-token"}]
    }});
  const reply = await fixture.run(command({method: "identify", user_id: null}));
  const diagnostic = reply.error.identity_structure;
  assert.equal(diagnostic.own_header_data_count, 100);
  assert.equal(diagnostic.own_header_link_count, 1);
  assert.equal(diagnostic.candidate_id_count, 0);
  assert.equal(diagnostic.next_present, true);
  assert.equal(diagnostic.next_valid_json, true);
  assert.equal(diagnostic.next_current_user.type, "object");
  assert.deepEqual(Array.from(diagnostic.next_current_user.keys), ["id", "userId"]);
  assert.deepEqual(Array.from(diagnostic.json_scripts, item => item.name), ["astroSharedData", "__NEXT_DATA__"]);
  assert.equal(diagnostic.json_scripts[0].type, "missing");
  assert.doesNotMatch(JSON.stringify(reply), /do-not-expose|secret-token|evil\.test|accessToken/);
});
test("ambiguous own identity reports only a capped candidate count without account IDs", async () => {
  const fixture = content({url: "https://www.kleinanzeigen.de/m-meine-anzeigen.html", currentUser: "12345", nextData: {props: {pageProps: {currentUser: {id: "67890", userId: "99999"}}}}});
  const reply = await fixture.run(command({method: "identify", user_id: null}));
  assert.equal(reply.error.identity_structure.candidate_id_count, 2);
  assert.doesNotMatch(JSON.stringify(reply), /12345|67890|99999/);
});
test("business inbox selection never changes private account routing", () => {
  assert.equal(P.targetURL(command({identity_page_path: "/portal/anzeigen"})), "https://www.kleinanzeigen.de/portal/nachrichten?conversationId=conversation-1");
  assert.equal(P.targetURL(command()), "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=conversation-1");
  assert.equal(P.targetURL(command({method: "inspect_ui", identity_page_path: "/portal/anzeigen", args: {tab_id: 7, view: "current"}})), null);
});
test("the observed business empty state is accepted only with fresh matching identity", async () => {
  const main = {checkVisibility: () => true,
    textContent: "Meine Nachrichten Hier geht bald die Post ab Jetzt inserieren und Anfragen erhalten oder nach spannenden Anzeigen suchen und andere Nutzer kontaktieren.",
    querySelectorAll: selector => selector === "h1" ? [{textContent: "Meine Nachrichten", checkVisibility: () => true}] : []};
  main.innerText = main.textContent;
  const fixture = content({url: "https://www.kleinanzeigen.de/portal/nachrichten", queryNodes: {main}});
  const response = await fixture.run(command({method: "list_conversations", identity_page_path: "/portal/anzeigen"}));
  assert.equal(response.result.conversations.length, 0);
  assert.equal(response.result.has_more, false);
  assert.equal(response.result.complete, true);
  assert.equal(response.result.account_user_id, "12345");
  assert.equal(fixture.fetched(), 1);
  const changed = content({url: "https://www.kleinanzeigen.de/portal/nachrichten", currentUser: "67890", queryNodes: {main}});
  assert.equal((await changed.run(command({method: "list_conversations", identity_page_path: "/portal/anzeigen"}))).error.code, "account_mismatch");
});
test("missing markup, a wrong page or a visible conversation cannot become a business empty inbox", async () => {
  const text = "Hier geht bald die Post ab Jetzt inserieren und Anfragen erhalten oder nach spannenden Anzeigen suchen und andere Nutzer kontaktieren.";
  for (const scenario of [
    {path: "/m-nachrichten.html", text},
    {path: "/portal/nachrichten", text: "Meine Nachrichten"},
    {path: "/portal/nachrichten", text, visibleConversation: true},
    {path: "/portal/nachrichten", text, hidden: true}
  ]) {
    const main = {hidden: scenario.hidden, checkVisibility: () => !scenario.hidden, textContent: scenario.text, innerText: scenario.text,
      querySelectorAll: selector => selector === "h1" ? [{textContent: "Meine Nachrichten", checkVisibility: () => true}] : scenario.visibleConversation ? [{checkVisibility: () => true}] : []};
    const fixture = content({url: "https://www.kleinanzeigen.de" + scenario.path, queryNodes: {main}});
    const response = await fixture.run(command({method: "list_conversations", identity_page_path: "/portal/anzeigen"}));
    assert.equal(response.error.code, "ui_unsupported");
  }
});
test("hydrated business empty inbox uses exact visible text and refreshes identity after waiting", async () => {
  const intro = "Hier geht bald die Post ab";
  const detail = "Jetzt inserieren und Anfragen erhalten oder nach spannenden Anzeigen suchen und andere Nutzer kontaktieren.";
  const main = {checkVisibility: () => true, textContent: intro + detail.replace(/\s/g, ""), innerText: intro + "\n" + detail,
    querySelectorAll: selector => selector === "h1" ? [{textContent: "Meine Nachrichten", checkVisibility: () => true}] : []};
  let checks = 0;
  const fixture = content({url: "https://www.kleinanzeigen.de/portal/nachrichten", queryNodes: {get main() {return ++checks >= 3 ? main : null;}}});
  const response = await fixture.run(command({method: "list_conversations", identity_page_path: "/portal/anzeigen"}));
  assert.equal(response.result.conversations.length, 0);
  assert.equal(response.result.complete, true);
  assert.ok(checks >= 3);
  assert.equal(fixture.fetched(), 2);
});
test("inbox readiness is bounded and never treats absent or hidden text as an empty inbox", async () => {
  let checks = 0;
  const fixture = content({url: "https://www.kleinanzeigen.de/portal/nachrichten", queryNodes: {get main() {checks++; return null;}}});
  const response = await fixture.run(command({method: "list_conversations", identity_page_path: "/portal/anzeigen"}));
  assert.equal(response.error.code, "ui_unsupported");
  assert.equal(checks, 50);
  assert.equal(fixture.fetched(), 1);
  const main = {checkVisibility: () => true,
    textContent: "Hier geht bald die Post ab Jetzt inserieren und Anfragen erhalten oder nach spannenden Anzeigen suchen und andere Nutzer kontaktieren.", innerText: "Meine Nachrichten",
    querySelectorAll: selector => selector === "h1" ? [{textContent: "Meine Nachrichten", checkVisibility: () => true}] : []};
  const hidden = content({url: "https://www.kleinanzeigen.de/portal/nachrichten", queryNodes: {main}});
  assert.equal((await hidden.run(command({method: "list_conversations", identity_page_path: "/portal/anzeigen"}))).error.code, "ui_unsupported");
});
test("a fresh parsed document does not wait for unrelated advertisement loading", async () => {
  const fixture = background({}, {tabStatus: "loading", readyState: "interactive"});
  fixture.context.commandUnderTest = command({method: "identify", user_id: null});
  const result = await vm.runInContext("execute(commandUnderTest)", fixture.context);
  assert.equal(result.navigation_verified, true);
  assert.equal(fixture.dispatched(), 1);
});
test("missing content injection produces bounded boolean-only navigation diagnostics", async () => {
  const fixture = background({}, {markerMissing: true});
  fixture.context.commandUnderTest = command({method: "identify", user_id: null, expires_at: new Date(Date.now() + 5025).toISOString()});
  await assert.rejects(vm.runInContext("execute(commandUnderTest)", fixture.context), error => {
    assert.equal(error.code, "safari_navigation_timeout");
    assert.equal(error.navigation_state.target_matched, true);
    assert.equal(error.navigation_state.marker_received, false);
    assert.equal(error.navigation_state.marker_ready, false);
    assert.equal(error.navigation_state.fresh_document, false);
    assert.equal(Object.values(error.navigation_state).every(value => typeof value === "boolean"), true);
    assert.equal(error.marker_delivery, "no_reply");
    return true;
  });
  assert.equal(fixture.dispatched(), 0);
});
test("content injection starts after HTML parsing without waiting for resource idle", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"));
  assert.equal(manifest.content_scripts[0].run_at, "document_end");
});
test("the macOS local queue uses persistent MV2 background without broader permissions", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"));
  assert.equal(manifest.manifest_version, 2);
  assert.equal(manifest.background.persistent, true);
  assert.deepEqual(manifest.permissions, ["nativeMessaging", "storage", "https://www.kleinanzeigen.de/*"]);
  const page = content();
  assert.equal(page.intervalCount(), 0);
  assert.equal(page.fetched(), 0);
  const bg = background();
  assert.equal(bg.listener({type: "wake"}, {id: "extension", tab: {id: 7, url: "https://www.kleinanzeigen.de/"}}), undefined);
});
test("idle local queue polling continues every five seconds without popup or website actions", async () => {
  let polls = 0;
  const fixture = background({}, {nativeHandler: async () => {polls++; return {command: null, pending_accounts: []};}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(polls, 1);
  const timer = [...fixture.timers.values()].find(value => value.ms === 5000);
  assert.ok(timer);
  await timer.callback();
  assert.equal(polls, 2);
  assert.equal(fixture.dispatched(), 0);
  assert.equal(fixture.sentCommands.length, 0);
});
test("pending pairing and a delivered command use a one-second local follow-up poll", async () => {
  for (const reply of [
    {command: null, pending_accounts: [{account_id: "own_a"}]},
    {command: command({method: "inspect_ui", args: {tab_id: 7, view: "current"}}), pending_accounts: []}
  ]) {
    const fixture = background({}, {nativeHandler: async (_app, message) => message.action === "poll" ? reply : {accepted: true}});
    await new Promise(resolve => setImmediate(resolve));
    assert.ok([...fixture.timers.values()].find(value => value.ms === 1000));
  }
});
test("marker diagnostics distinguish send rejection, missing reply and sender/handler refusal", async () => {
  for (const markerFailure of ["send_rejected", "sender_rejected", "handler_error"]) {
    const fixture = background({}, {markerFailure});
    fixture.context.commandUnderTest = command({method: "identify", user_id: null, expires_at: new Date(Date.now() + 5015).toISOString()});
    await assert.rejects(vm.runInContext("execute(commandUnderTest)", fixture.context), error => {
      assert.equal(error.code, "safari_navigation_timeout");
      assert.equal(error.marker_delivery, markerFailure);
      assert.doesNotMatch(JSON.stringify(error), /private-runtime-details/);
      return true;
    });
    assert.equal(fixture.dispatched(), 0);
  }
});
test("a refused sender receives only a fixed marker error and cannot execute a command", async () => {
  const fixture = content();
  const reply = await fixture.marker({id: "other-extension"});
  assert.equal(reply.error.code, "marker_sender_mismatch");
  assert.equal(reply.document_id, undefined);
  assert.equal(reply.url, undefined);
  assert.equal(fixture.run(command(), {id: "other-extension"}), undefined);
  assert.equal(fixture.fetched(), 0);
});
test("hung native messaging releases the shared poll state with a fixed timeout", async () => {
  const fixture = background({}, {nativeHandler: () => new Promise(() => {})});
  const status = fixture.listener({type: "status"}, {id: "extension"});
  const timeout = [...fixture.timers.values()].find(timer => timer.ms === 40000);
  assert.ok(timeout);
  timeout.callback();
  const result = await status;
  assert.equal(result.error, "safari_native_timeout");
  assert.equal(result.pending_accounts.length, 0);
  assert.equal(vm.runInContext("polling", fixture.context), false);
  assert.equal(vm.runInContext("pollStatePromise", fixture.context), null);
});
test("a command in a late native reply is discarded and never executed", async () => {
  let release;
  const reply = new Promise(resolve => {release = resolve;});
  const fixture = background({}, {nativeHandler: () => reply});
  const status = fixture.listener({type: "status"}, {id: "extension"});
  [...fixture.timers.values()].find(timer => timer.ms === 40000).callback();
  assert.equal((await status).error, "safari_native_timeout");
  release({pending_accounts: [], command: command({method: "send_step", args: {tab_id: 7, kind: "text", text: "Darf nie versendet werden", target: {target_kind: "conversation", target_id: "c1"}}})});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fixture.dispatched(), 0);
  assert.equal(fixture.sentCommands.length, 0);
});
test("local polling can recover after a timeout without replaying the expired reply", async () => {
  let calls = 0;
  const fixture = background({}, {nativeHandler: () => ++calls === 1 ? new Promise(() => {}) : Promise.resolve({command: null, pending_accounts: [{account_id: "own_a"}]})});
  const first = fixture.listener({type: "status"}, {id: "extension"});
  [...fixture.timers.values()].find(timer => timer.ms === 40000).callback();
  assert.equal((await first).error, "safari_native_timeout");
  const recovered = await fixture.listener({type: "status"}, {id: "extension"});
  assert.equal(recovered.error, null);
  assert.equal(recovered.pending_accounts[0].account_id, "own_a");
  assert.equal(calls, 2);
  assert.equal(fixture.dispatched(), 0);
});
