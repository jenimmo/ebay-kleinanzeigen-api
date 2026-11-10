"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const P = require("../protocol.js");
const ENDPOINT = "https://www.kleinanzeigen.de/m-mein-profil.json";
function fixture({body = '{"userId":"90001","email":"PRIVATE-EMAIL","csrfToken":"PRIVATE-TOKEN"}', status = 200, contentType = "application/json;charset=UTF-8", responseURL = ENDPOINT, redirected = false, throwFetch = false, directIds = [], password = false, page = "/m-meine-anzeigen.html", businessUser = null} = {}) {
  let listener;
  const requests = [];
  const window = {}; window.top = window;
  const document = {querySelector: selector => selector === 'input[type="password"]' && password ? {} : null,
    querySelectorAll: selector => selector.startsWith("[data-current-user-id]") ? directIds.map(id => ({getAttribute: name => name === "data-current-user-id" ? id : null})) : [],
    getElementById: name => name === "astroSharedData" && businessUser ? {textContent: JSON.stringify({user: {id: businessUser}})} : null};
  const context = vm.createContext({KleinanzeigenProtocol: P, URL, Date, crypto: webcrypto, TextEncoder, TextDecoder, AbortSignal, window, document,
    location: new URL("https://www.kleinanzeigen.de" + page),
    fetch: async (url, options) => {
      requests.push({url, options});
      if (throwFetch) throw new Error("PRIVATE-REDIRECT-COOKIE");
      return {url: responseURL, status, ok: status >= 200 && status < 300, redirected, headers: {get: () => contentType},
        body: new ReadableStream({start(controller) {controller.enqueue(new TextEncoder().encode(body)); controller.close();}})};
    }, browser: {runtime: {id: "extension", onMessage: {addListener: value => {listener = value;}}}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8"), context);
  return {requests, run: (overrides = {}) => listener({type: "kleinanzeigen-command", command: {id: "profile-read", method: "identify", account_id: "own_a", user_id: null,
    expires_at: new Date(Date.now() + 10000).toISOString(), args: {tab_id: 7}, ...overrides}}, {id: "extension"})};
}
test("private identity reads only the observed fixed fresh profile endpoint and returns only userId", async () => {
  const current = fixture();
  const reply = await current.run();
  assert.equal(reply.result.user_id, "90001");
  assert.equal(reply.result._upstream_reached, true);
  assert.equal(current.requests.length, 1);
  assert.equal(current.requests[0].url, ENDPOINT);
  assert.equal(current.requests[0].options.credentials, "same-origin");
  assert.equal(current.requests[0].options.cache, "no-store");
  assert.equal(current.requests[0].options.redirect, "error");
  assert.equal(current.requests[0].options.signal.aborted, false);
  assert.doesNotMatch(JSON.stringify(reply), /PRIVATE|email|csrf|TOKEN/);
});
test("direct own identities and existing account bindings cannot conflict with fresh profile data", async () => {
  assert.equal((await fixture({directIds: ["90001"]}).run()).result.user_id, "90001");
  assert.equal((await fixture({directIds: ["90002"]}).run()).error.code, "account_mismatch");
  const ambiguous = fixture({directIds: ["90001", "90002"]});
  assert.equal((await ambiguous.run()).error.code, "identity_unverified");
  assert.equal(ambiguous.requests.length, 0);
  const switched = fixture({page: "/m-nachrichten.html"});
  assert.equal((await switched.run({method: "send_step", user_id: "90002", args: {tab_id: 7, kind: "text"}})).error.code, "account_mismatch");
  assert.equal(switched.requests.length, 1);
});
test("the fixed profile endpoint cannot be used to bind from another seller or to alter business identity", async () => {
  const seller = fixture({page: "/s-anzeigen-des-nutzers/90001"});
  assert.equal((await seller.run()).error.code, "identity_page_required");
  assert.equal(seller.requests.length, 0);
  const business = fixture({page: "/portal/anzeigen", businessUser: "80001"});
  assert.equal((await business.run()).result.user_id, "80001");
  assert.equal(business.requests.length, 0);
});
test("login, blocking, rate limits and redirects retain fixed non-sensitive failures", async () => {
  for (const [options, code] of [
    [{status: 401}, "reauth_required"], [{status: 403}, "upstream_blocked"], [{status: 429}, "upstream_rate_limited"],
    [{redirected: true}, "reauth_required"], [{throwFetch: true}, "reauth_required"],
    [{responseURL: "https://www.kleinanzeigen.de/m-einloggen.html"}, "reauth_required"],
    [{responseURL: ENDPOINT + "?userId=90001"}, "reauth_required"], [{responseURL: "https://evil.test/"}, "reauth_required"],
    [{contentType: "text/html", body: "<input type=password>PRIVATE-HTML"}, "identity_unverified"]
  ]) {
    const reply = await fixture(options).run();
    assert.equal(reply.error.code, code);
    assert.doesNotMatch(JSON.stringify(reply), /PRIVATE|evil\.test|userId=90001/);
  }
  const login = fixture({password: true});
  assert.equal((await login.run()).error.code, "reauth_required");
  assert.equal(login.requests.length, 0);
});
test("malformed, oversized, ambiguous or non-numeric profile JSON never binds an account", async () => {
  for (const body of ["not-json PRIVATE", "null", "[]", '{"email":"PRIVATE"}', '{"userId":90001}', '{"userId":"90001x"}',
    '{"userId":"90001","userId":"90002"}', '{"userId":"90001","\\u0075serId":"90001"}', '{"userId":"90001","padding":"' + "x".repeat(65536) + '"}']) {
    const reply = await fixture({body}).run();
    assert.equal(reply.error.code, "identity_unverified");
    assert.doesNotMatch(JSON.stringify(reply), /PRIVATE|90001|90002/);
  }
  const nested = await fixture({body: '{"userId":"90001","nested":{"userId":"90002"},"note":"\\\"userId\\\":private"}'}).run();
  assert.equal(nested.result.user_id, "90001");
});
test("expired commands cannot initiate an authenticated profile request", async () => {
  const current = fixture();
  const reply = await current.run({expires_at: new Date(0).toISOString()});
  assert.equal(reply.error.code, "command_expired");
  assert.equal(current.requests.length, 0);
});
