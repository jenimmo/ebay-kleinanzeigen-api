"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const P = require("../protocol.js");
const SOURCE = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const ORIGIN = "https://www.kleinanzeigen.de";
function tab(id, route, incognito = false) { return {id, url: ORIGIN + route, incognito, status: "complete"}; }
function command(overrides = {}) {
  return {id: "recover-1", account_id: "own_a", user_id: "111", method: "recover_tab", args: {tab_id: 1},
    expires_at: new Date(Date.now() + 30000).toISOString(), ...overrides};
}
function background(tabs, {userId = "111", queryError = false, duringLookup = null, duringProof = null, business = false} = {}) {
  const storage = {}, calls = {query: [], update: [], content: [], native: []};
  let revision = 0;
  const actual = new Map(tabs.map(value => [value.id, {...value}]));
  const context = vm.createContext({KleinanzeigenProtocol: P, URL, Date, TextEncoder,
    setTimeout: () => 1, clearTimeout: () => {}, browser: {
      runtime: {id: "extension", onMessage: {addListener: () => {}}, sendNativeMessage: async (_app, message) => {
        calls.native.push(message); return {command: null, pending_accounts: []};
      }},
      storage: {local: {get: async key => ({[key]: storage[key]}), set: async value => Object.assign(storage, value)}},
      tabs: {
        query: async args => { calls.query.push(args); if (queryError) throw new Error("private details"); return tabs; },
        get: async id => {
          if (duringLookup && !revision) duringLookup(actual, id);
          if (!actual.has(id)) throw new Error("not in this profile");
          return {...actual.get(id)};
        },
        update: async (id, update) => {
          calls.update.push({id, ...update}); revision++;
          actual.set(id, {...actual.get(id), url: business ? ORIGIN + "/portal/anzeigen" : update.url});
        },
        sendMessage: async (id, message) => {
          if (message.type === "kleinanzeigen-document-marker") {
            const url = new URL(actual.get(id).url);
            return {document_id: "doc-" + revision, url: url.origin + url.pathname, ready_state: "complete", navigation_started_at: Date.now()};
          }
          calls.content.push(message.command);
          if (duringProof) duringProof(actual, id);
          return {result: {user_id: userId, identity_page_path: business ? "/portal/anzeigen" : "/m-meine-anzeigen.html",
            capabilities: {text_status: "unverified"}, _upstream_reached: true}};
        }
      }
    }});
  vm.runInContext(SOURCE, context);
  return {calls, storage, async run(value = command()) { context.commandUnderTest = value; return vm.runInContext("execute(commandUnderTest)", context); }};
}

test("explicit recovery ignores public listings and proves the single own tab afresh", async () => {
  const fixture = background([tab(70, "/portal/nachrichten"), tab(71, "/s-anzeige/eigene-anzeige/1234500001-84-123")], {business: true});
  const result = await fixture.run();
  assert.equal(result.user_id, "111");
  assert.equal(result.account_user_id, "111");
  assert.equal(result.recovered_tab_id, 70);
  assert.equal(result.identity_page_path, "/portal/anzeigen");
  assert.equal(fixture.calls.query.length, 1);
  assert.equal(fixture.calls.query[0].url, ORIGIN + "/*");
  assert.equal(fixture.calls.update.length, 1);
  assert.equal(fixture.calls.update[0].id, 70);
  assert.equal(fixture.calls.update[0].url, ORIGIN + "/m-meine-anzeigen.html");
  assert.equal(fixture.calls.update[0].active, undefined);
  assert.equal(fixture.calls.content.length, 1);
  assert.equal(fixture.calls.content[0].method, "identify");
  assert.equal(fixture.calls.content[0].user_id, "111");
  assert.equal(fixture.calls.content[0].fresh_navigation.document_id, "doc-1");
  assert.equal(Object.keys(fixture.storage).some(key => key.startsWith("dispatch:")), false);
});

test("unknown, public, foreign and private tabs never become recovery candidates", async () => {
  const fixture = background([
    tab(10, "/s-anzeige/1234500001"), tab(11, "/s-bestandsliste.html?userId=111"),
    tab(12, "/m-nachrichten.html", true), {...tab(13, "/m-nachrichten.html"), incognito: undefined},
    {...tab(14, "/m-meine-anzeigen.html"), url: "https://other.example/m-meine-anzeigen.html"}
  ]);
  await assert.rejects(fixture.run(), error => error.code === "safari_tab_missing");
  assert.equal(fixture.calls.update.length, 0);
  assert.equal(fixture.calls.content.length, 0);
});

test("multiple own tabs fail closed without selecting the active or first tab", async () => {
  const fixture = background([tab(70, "/m-meine-anzeigen.html"), {...tab(71, "/m-nachrichten.html"), active: true}]);
  await assert.rejects(fixture.run(), error => error.code === "safari_tab_ambiguous");
  assert.equal(fixture.calls.update.length, 0);
  assert.equal(fixture.calls.content.length, 0);
});

test("candidate navigation or closure before use is rejected without a replacement scan", async () => {
  for (const mutate of [(tabs, id) => tabs.delete(id), (tabs, id) => tabs.set(id, {...tabs.get(id), url: "https://other.example/"}),
    (tabs, id) => tabs.set(id, {...tabs.get(id), incognito: true})]) {
    const fixture = background([tab(70, "/m-nachrichten.html")], {duringLookup: mutate});
    await assert.rejects(fixture.run(), error => error.code === "safari_tab_missing");
    assert.equal(fixture.calls.query.length, 1);
    assert.equal(fixture.calls.update.length, 0);
  }
});

test("fresh identity mismatch cannot be cached or exported as recovered", async () => {
  const fixture = background([tab(70, "/m-nachrichten.html")], {userId: "222"});
  await assert.rejects(fixture.run(), error => error.code === "account_mismatch");
  assert.equal(fixture.calls.content.length, 1);
  assert.equal(fixture.storage["identity-route:own_a"], undefined);
});

test("a candidate closed or made private during proof is not recovered", async () => {
  for (const mutate of [(tabs, id) => tabs.delete(id), (tabs, id) => tabs.set(id, {...tabs.get(id), incognito: true})]) {
    const fixture = background([tab(70, "/m-nachrichten.html")], {duringProof: mutate});
    await assert.rejects(fixture.run(), error => error.code === "safari_tab_missing");
    assert.equal(fixture.calls.content.length, 1);
  }
});

test("permission errors remain fixed diagnostics without navigation", async () => {
  const fixture = background([], {queryError: true});
  await assert.rejects(fixture.run(), error => error.code === "safari_recovery_unavailable" && !error.message.includes("private"));
  assert.equal(fixture.calls.update.length, 0);
});

test("fresh enrollment, expired commands, idle polls and send errors cannot recover", async () => {
  const fixture = background([tab(70, "/m-nachrichten.html")]);
  await assert.rejects(fixture.run(command({user_id: null})), error => error.code === "account_unverified");
  await assert.rejects(fixture.run(command({expires_at: new Date(0).toISOString()})), error => error.code === "command_expired");
  await assert.rejects(fixture.run(command({method: "send_step", args: {tab_id: 1, kind: "text", text: "Nicht senden", target: {target_kind: "conversation", target_id: "c1"}}})), error => error.code === "safari_tab_missing");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fixture.calls.query.length, 0);
  assert.equal(fixture.calls.update.length, 0);
  assert.equal(fixture.calls.content.length, 0);
  assert.equal(Object.keys(fixture.storage).some(key => key.startsWith("dispatch:")), false);
});
