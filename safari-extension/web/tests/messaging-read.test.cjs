"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {webcrypto} = require("node:crypto");
const P = require("../protocol.js");
const SOURCE = fs.readFileSync(path.join(__dirname, "../messaging-read.js"), "utf8");
const ORIGIN = "https://www.kleinanzeigen.de";
const GATEWAY = "https://gateway.kleinanzeigen.de";
function command(overrides = {}) {
  return {id: "read-1", method: "get_conversation", account_id: "own_a", user_id: "12345",
    expires_at: new Date(Date.now() + 45000).toISOString(), args: {tab_id: 1}, ...overrides};
}
function message(index, overrides = {}) {
  return {messageId: "m-" + index, type: "MESSAGE", boundness: index % 2 ? "INBOUND" : "OUTBOUND",
    textShort: "Grüße " + index, receivedDate: new Date(Date.UTC(2026, 0, 1, 0, 0, index)).toISOString(), attachments: [], ...overrides};
}
function conversation(overrides = {}) {
  return {id: "c-1", adId: 1234500001, role: "Buyer", userIdBuyer: 12345, userIdSeller: "67890",
    adTitle: "Eigene Anzeige", unread: true, unreadMessagesCount: 2,
    receivedDate: "2026-01-01T00:00:03Z", attachmentsEnabled: true,
    messages: [message(1), message(2), message(3)], ...overrides};
}
function fixture({profile = {userId: "12345", csrfToken: "PRIVATE-CSRF", email: "PRIVATE-EMAIL"}, data = conversation(),
  list = null, override = null, token = "Bearer PRIVATE-TOKEN", expiration = Date.now() + 60000} = {}) {
  const requests = [];
  const window = {}; window.top = window;
  const context = vm.createContext({KleinanzeigenProtocol: P, URL, Date, TextDecoder, TextEncoder, Uint8Array,
    AbortController, crypto: webcrypto, window, location: new URL(ORIGIN + "/m-nachrichten.html"),
    setTimeout: () => 1, clearTimeout: () => {},
    console: {log: () => {throw Error("No logging");}, error: () => {throw Error("No logging");}},
    fetch: async (url, options) => {
      requests.push({url, options});
      const custom = override?.(url, options, requests.length);
      if (custom?.reject) throw Error("PRIVATE-NETWORK-ERROR");
      const value = url === ORIGIN + "/m-mein-profil.json" ? profile :
        url === ORIGIN + "/m-access-token.json" ? {expiration} :
        url.includes("?page=") ? (typeof list === "function" ? list(url) : list || {_meta: {numFound: 1}, conversations: [conversation()]}) :
        (typeof data === "function" ? data() : data);
      const body = custom?.body ?? JSON.stringify(value);
      return {url: custom?.url ?? url, status: custom?.status ?? 200,
        ok: (custom?.status ?? 200) === 200, redirected: custom?.redirected ?? false,
        headers: {get: name => name.toLowerCase() === "content-type" ? custom?.mime ?? "application/json; charset=UTF-8" :
          name.toLowerCase() === "authorization" ? token : null},
        body: new ReadableStream({start(controller) {controller.enqueue(new TextEncoder().encode(body)); controller.close();}})};
    }});
  vm.runInContext(SOURCE, context);
  return {api: context.KleinanzeigenMessagingRead, requests};
}
const json = value => JSON.parse(JSON.stringify(value));

test("system events share message pagination without invented senders or arbitrary payloads", async () => {
  const messages = [message(1), message(2, {type: "INITIAL_MESSAGE", textShort: "Sicherheitshinweis", extraPayload: "PRIVATE PAYMENT DATA"}), message(3),
    message(4, {type: "AdStatusChanged", textShort: "Anzeige gelöscht", extraPayload: "PRIVATE PROFILE DATA"}), message(5)];
  const f = fixture({data: conversation({messages})}), observed = [], events = [];
  let cursor = null, rounds = 0;
  do {
    const result = await f.api.get(command(), {conversation_id: "c-1", limit: 2, cursor});
    assert.ok(result.messages.length + result.system_events.length <= 2);
    assert.equal(result.complete, false); assert.equal(result.unsupported_message_count, 2);
    observed.push(...result.messages.map(x => x.message_id), ...result.system_events.map(x => x.event_id));
    events.push(...result.system_events); cursor = result.next_cursor; rounds++;
  } while (cursor);
  assert.equal(rounds, 3); assert.equal(new Set(observed).size, 5);
  assert.deepEqual(observed.sort(), messages.map(x => x.messageId).sort());
  assert.ok(events.every(x => !Object.hasOwn(x, "sender_user_id")));
  assert.doesNotMatch(JSON.stringify(events), /PRIVATE|extraPayload|boundness|attachments/);
  assert.deepEqual(events.map(x => x.event_type).sort(), ["AdStatusChanged", "INITIAL_MESSAGE"]);
});

test("event-only pages, missing event timestamps and UTF-8 budgets retain every event", async () => {
  const messages = Array.from({length: 5}, (_, index) => message(index + 1, {
    type: "SYSTEM", textShort: "Ö".repeat(15000), ...(index === 0 ? {receivedDate: null} : {}), payload: {secret: "PRIVATE"}}));
  const f = fixture({data: conversation({messages})}), ids = [];
  let cursor = null;
  do {
    const page = await f.api.get(command(), {conversation_id: "c-1", limit: 200, cursor});
    assert.equal(page.messages.length, 0); assert.equal(page.system_events.length, 1);
    assert.ok(new TextEncoder().encode(JSON.stringify(page.system_events)).byteLength < 45000);
    assert.equal(page.complete, false); ids.push(...page.system_events.map(x => x.event_id)); cursor = page.next_cursor;
    if (page.system_events[0].event_id === "m-1") assert.equal(page.system_events[0].created_at, null);
  } while (cursor);
  assert.equal(ids.length, 5); assert.equal(new Set(ids).size, 5);
});

test("list and detail normalize observed compact ISO offsets to the same UTC instants", async () => {
  for (const [receivedDate, expected] of [
    ["2026-11-04T12:34:56.789+0100", "2026-11-04T11:34:56.789Z"],
    ["2026-11-04T12:34:56.125+0130", "2026-11-04T11:04:56.125Z"],
    ["2026-11-04T12:34:56.125-0330", "2026-11-04T16:04:56.125Z"],
    ["2026-11-04T00:10:00+1400", "2026-11-03T10:10:00.000Z"],
    ["2026-11-04T12:34:56+0000", "2026-11-04T12:34:56.000Z"],
    ["2026-11-04T12:34:56-0000", "2026-11-04T12:34:56.000Z"],
    ["2026-11-04T12:34:56+01:00", "2026-11-04T11:34:56.000Z"],
    ["2026-11-04T12:34:56Z", "2026-11-04T12:34:56.000Z"],
  ]) {
    const list = fixture({list: {_meta: {numFound: 1}, conversations: [conversation({receivedDate})]}});
    const listed = await list.api.list(command(), {limit: 10});
    assert.equal(listed.conversations[0].updated_at, expected);
    assert.equal(list.requests.length, 3);
    const detail = fixture({data: conversation({messages: [message(1, {receivedDate})]})});
    const read = await detail.api.get(command(), {conversation_id: "c-1"});
    assert.equal(read.messages[0].created_at, expected);
    assert.equal(P.preciseTime(read.messages[0].created_at), true);
    assert.equal(detail.requests.length, 3);
  }
});

test("list and detail still reject invalid offsets and unverified timestamp forms", async () => {
  for (const receivedDate of [
    "2026-11-04T12:34:56+2400", "2026-11-04T12:34:56-2400",
    "2026-11-04T12:34:56+0060", "2026-11-04T12:34:56-1260",
    "2026-11-04T12:34:56+9999", "2026-11-04T12:34:56+010",
    "2026-11-04T12:34:56+01000", "2026-11-04T12:34:56+01",
    "2026-11-04T12:34:56+24:00", "2026-11-04T12:34:56+00:60",
    "2026-11-04T12:34:56", "2026-11-04 12:34:56+0100", "2026-11-04",
    "1793795696000", 1793795696000,
  ]) {
    const list = fixture({list: {_meta: {numFound: 1}, conversations: [conversation({receivedDate})]}});
    await assert.rejects(list.api.list(command(), {limit: 10}), error => error.code === "ui_unsupported" && error.reader_structure.reason === "item_timestamp");
    const detail = fixture({data: conversation({messages: [message(1, {receivedDate})]})});
    await assert.rejects(detail.api.get(command(), {conversation_id: "c-1"}), error => error.code === "ui_unsupported");
    assert.equal(list.requests.length, 3);
    assert.equal(detail.requests.length, 3);
  }
});

test("list guard diagnostics expose only fixed shapes and bounded metadata", async () => {
  const cases = [
    {row: {unread: null, unreadMessagesCount: "PRIVATE-COUNT"}, stage: "list_unread", reason: "item_unread"},
    {row: {receivedDate: "2026-11-04T12:34:56.789+2500"}, stage: "list_timestamp", reason: "item_timestamp", format: "iso_offset_compact"},
    {row: {receivedDate: "2026-11-04T12:34:56"}, stage: "list_timestamp", reason: "item_timestamp", format: "iso_unzoned"},
    {row: {receivedDate: 1793795696000}, stage: "list_timestamp", reason: "item_timestamp", format: "numeric_epoch_ms"},
    {row: {receivedDate: "1793795696"}, stage: "list_timestamp", reason: "item_timestamp", format: "numeric_epoch_s"},
    {row: {receivedDate: "2026-11-04"}, stage: "list_timestamp", reason: "item_timestamp", format: "date_only"},
    {row: {receivedDate: "2026-11-04 12:34:56Z"}, stage: "list_timestamp", reason: "item_timestamp", format: "iso_space_zoned"},
    {row: {receivedDate: "PRIVATE-DATE"}, stage: "list_timestamp", reason: "item_timestamp", format: "other"},
    {row: {adTitle: null}, stage: "list_item", reason: "item_normalization", code: "message_text_unverified"},
    {meta: {pageNum: 1}, stage: "list_meta", reason: "page_number"},
    {meta: {pageSize: 20}, stage: "list_meta", reason: "page_size"},
    {meta: {numFound: "PRIVATE-COUNT"}, stage: "list_shape", reason: "num_found"},
  ];
  for (const entry of cases) {
    const f = fixture({list: {_meta: {numFound: 1, pageNum: 0, pageSize: 10, ...entry.meta},
      conversations: [conversation({adTitle: "PRIVATE-TITLE", sellerName: "PRIVATE-NAME", messages: [message(1, {textShort: "PRIVATE-MESSAGE"})], ...entry.row})],
      lastModified: "PRIVATE-DATE", "PRIVATE-KEY": "PRIVATE-VALUE", _links: {next: {href: "https://private.invalid/secret"}}}});
    await assert.rejects(f.api.list(command({method: "list_conversations"}), {limit: 10}), error => {
      assert.equal(error.code, entry.code || "ui_unsupported");
      const diagnostic = json(error.reader_structure);
      assert.equal(diagnostic.operation, "list");
      assert.equal(diagnostic.stage, entry.stage);
      assert.equal(diagnostic.reason, entry.reason);
      assert.equal(diagnostic.requested_page, 0);
      assert.equal(diagnostic.requested_size, 10);
      assert.equal(diagnostic.root_types.lastModified, "string");
      assert.equal(diagnostic.conversation_count, 1);
      if (entry.row) assert.equal(diagnostic.item_index, 0);
      if (entry.format) assert.equal(diagnostic.timestamp_format, entry.format);
      assert.doesNotMatch(JSON.stringify(diagnostic), /PRIVATE|12345|67890|1234500001|2026-|1793795696|Bearer|csrf|private\.invalid/);
      return true;
    });
    assert.equal(f.requests.length, 3);
  }
});

test("request diagnostics identify the failing phase without exposing body or headers", async () => {
  const cases = [
    {path: "/m-mein-profil.json", body: '{"userId":"12345","userId":"12345","csrfToken":"PRIVATE"}', stage: "profile_request", reason: "duplicate_root_key", count: 1},
    {path: "/m-access-token.json", body: "PRIVATE-INVALID-JSON", stage: "token_request", reason: "json_parse", count: 2},
    {path: "/conversations?", mime: "text/PRIVATE", stage: "list_request", reason: "content_type", count: 3},
    {path: "/conversations?", status: 503, body: "PRIVATE", stage: "list_request", reason: "http_status", count: 3},
    {path: "/conversations?", body: "[]", stage: "list_request", reason: "root_type", count: 3},
  ];
  for (const entry of cases) {
    const f = fixture({override: url => url.includes(entry.path) ? entry : null});
    await assert.rejects(f.api.list(command(), {limit: 10}), error => {
      assert.equal(error.code, "ui_unsupported");
      assert.equal(error.reader_structure.stage, entry.stage);
      assert.equal(error.reader_structure.reason, entry.reason);
      assert.equal(error.reader_structure.http_status, entry.status || 200);
      assert.doesNotMatch(JSON.stringify(error.reader_structure), /PRIVATE|12345|Authorization|csrf|userId|https?:/);
      return true;
    });
    assert.equal(f.requests.length, entry.count);
  }
});

test("read chain uses only three fixed fresh GETs and never returns credentials", async () => {
  const f = fixture(), cmd = command();
  const result = await f.api.get(cmd, {conversation_id: "c-1", limit: 2});
  assert.equal(f.requests.length, 3);
  assert.deepEqual(f.requests.map(r => r.url), [ORIGIN + "/m-mein-profil.json", ORIGIN + "/m-access-token.json",
    GATEWAY + "/messagebox/api/users/12345/conversations/c-1?contentWarnings=true"]);
  for (const {options} of f.requests) {
    assert.equal(options.method, "GET"); assert.equal(options.credentials, "include");
    assert.equal(options.redirect, "error"); assert.equal(options.cache, "no-store"); assert.equal(options.body, undefined);
  }
  assert.equal(f.requests[1].options.headers["X-CSRF-TOKEN"], "PRIVATE-CSRF");
  assert.equal(f.requests[2].options.headers.Authorization, "Bearer PRIVATE-TOKEN");
  assert.equal(f.requests[2].options.headers["X-ECG-USER-AGENT"], "messagebox-1");
  assert.equal(result.account_user_id, "12345"); assert.equal(result.recipient_user_id, "67890");
  assert.deepEqual(json(result.messages.map(m => m.message_id)), ["m-2", "m-3"]);
  assert.equal(result.messages[0].sender_user_id, "12345"); assert.equal(result.messages[1].sender_user_id, "67890");
  assert.equal(result._view.attachments_enabled, true);
  assert.equal(result._view.messages.length, 3);
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE|csrf|Authorization|email/);
});
test("fresh profile mismatches and ambiguous root keys prevent token and gateway access", async () => {
  for (const profile of [{userId: "99999", csrfToken: "PRIVATE"}, {userId: 12345, csrfToken: "PRIVATE"}, {userId: "12345", csrfToken: "bad\nsecret"}]) {
    const f = fixture({profile});
    await assert.rejects(f.api.get(command(), {conversation_id: "c-1"}));
    assert.equal(f.requests.length, 1);
  }
  const f = fixture({override: url => url.endsWith("m-mein-profil.json") ? {body: '{"userId":"12345","\\u0075serId":"12345","csrfToken":"PRIVATE"}'} : null});
  await assert.rejects(f.api.get(command(), {conversation_id: "c-1"}), e => e.code === "ui_unsupported");
  assert.equal(f.requests.length, 1);
});
test("conversation account, role, target and duplicate message IDs are independently checked", async () => {
  for (const data of [conversation({userIdBuyer: "99999"}), conversation({role: "Seller"}), conversation({id: "different"}),
    conversation({userIdSeller: "12345"}), conversation({role: "admin"}), conversation({messages: [message(1), message(1)]})]) {
    const f = fixture({data});
    await assert.rejects(f.api.get(command(), {conversation_id: "c-1"}));
    assert.equal(f.requests.length, 3);
  }
});
test("list pagination scopes cursor and filters one server page without background scans", async () => {
  const f = fixture({list: url => {
    const page = Number(new URL(url).searchParams.get("page"));
    return {_meta: {numFound: 3, pageNum: page, pageSize: 2}, conversations: page === 0 ?
      [conversation({id: "c-1"}), conversation({id: "c-2", unread: false, unreadMessagesCount: 0})] : [conversation({id: "c-3"})]};
  }});
  const cmd = command({method: "list_conversations"});
  const first = await f.api.list(cmd, {limit: 2, unread_only: true});
  assert.deepEqual(json(first.conversations.map(c => c.conversation_id)), ["c-1"]);
  assert.equal(first.has_more, true); assert.equal(f.requests.length, 3);
  const second = await f.api.list(cmd, {limit: 2, unread_only: true, cursor: first.next_cursor});
  assert.deepEqual(json(second.conversations.map(c => c.conversation_id)), ["c-3"]);
  assert.equal(second.next_cursor, null); assert.equal(f.requests.length, 6);
  await assert.rejects(f.api.list(command({account_id: "other"}), {limit: 2, unread_only: true, cursor: first.next_cursor}), e => e.code === "cursor_expired");
  assert.equal(f.requests.length, 6);
});
test("inconsistent server windows cannot become a complete or empty inbox", async () => {
  for (const list of [
    {_meta: {numFound: 3}, conversations: []},
    {_meta: {numFound: 1, pageNum: 2}, conversations: [conversation()]},
    {_meta: {numFound: 1, pageSize: 25}, conversations: [conversation()]},
    {_meta: {numFound: 0}, conversations: [conversation()]}
  ]) await assert.rejects(fixture({list}).api.list(command(), {limit: 50}), e => e.code === "ui_unsupported");
});
test("latest-first pages are stably time sorted and older cursors do not overlap new arrivals", async () => {
  let messages = [message(3), message(1), message(2)];
  const f = fixture({data: () => conversation({messages})});
  const first = await f.api.get(command(), {conversation_id: "c-1", limit: 2});
  assert.deepEqual(json(first.messages.map(m => m.message_id)), ["m-2", "m-3"]);
  messages = [message(4), ...messages];
  const second = await f.api.get(command(), {conversation_id: "c-1", limit: 2, cursor: first.next_cursor});
  assert.deepEqual(json(second.messages.map(m => m.message_id)), ["m-1"]);
  assert.equal(second.has_more, false);
  messages = [message(1), message(3)];
  await assert.rejects(f.api.get(command(), {conversation_id: "c-1", cursor: first.next_cursor}), e => e.code === "cursor_expired");
});
test("message pagination respects UTF-8 bytes and keeps a lossless older cursor", async () => {
  const f = fixture({data: conversation({messages: [1, 2, 3, 4].map(i => message(i, {textShort: "ä".repeat(10000)}))})});
  const first = await f.api.get(command(), {conversation_id: "c-1", limit: 200});
  assert.deepEqual(json(first.messages.map(m => m.message_id)), ["m-3", "m-4"]);
  assert.ok(new TextEncoder().encode(JSON.stringify(first.messages)).length < 45000);
  const second = await f.api.get(command(), {conversation_id: "c-1", limit: 200, cursor: first.next_cursor});
  assert.deepEqual(json(second.messages.map(m => m.message_id)), ["m-1", "m-2"]);
  await assert.rejects(fixture({data: conversation({messages: [message(1, {textShort: "漢".repeat(16000)})]})}).api.get(command(), {conversation_id: "c-1"}), e => e.code === "message_too_large");
});
test("list byte limits resume within the same server page rather than dropping summaries", async () => {
  const items = Array.from({length: 10}, (_, i) => conversation({id: "c-" + i, adTitle: "ä".repeat(4000)}));
  const f = fixture({list: {_meta: {numFound: 10}, conversations: items}}), cmd = command();
  const first = await f.api.list(cmd, {limit: 10});
  assert.ok(first.conversations.length > 0 && first.conversations.length < 10);
  const second = await f.api.list(cmd, {limit: 10, cursor: first.next_cursor});
  assert.equal(first.conversations.length + second.conversations.length, 10);
  assert.equal(new Set([...first.conversations, ...second.conversations].map(c => c.conversation_id)).size, 10);
});
test("unsupported automatic messages and unknown attachment routes are explicit and never fetched", async () => {
  const f = fixture({data: conversation({messages: [message(1, {type: "PAYMENT_AND_SHIPPING_MESSAGE", secret: "PRIVATE"}),
    message(2, {attachments: [{gatewayUrl: GATEWAY + "/messagebox/api/attachments?messageId=m-2&filename=a.jpg", format: "image/jpeg"},
      {gatewayUrl: "https://evil.test/PRIVATE", format: "image/jpeg"}]})]})});
  const result = await f.api.get(command(), {conversation_id: "c-1"});
  assert.equal(result.unsupported_message_count, 1); assert.equal(result.complete, false);
  assert.deepEqual(json(result._view.messages), [{message_id: "m-1", direction: null, type: "unsupported"}, {message_id: "m-2", direction: "outbound", type: "message"}]);
  assert.equal(result.messages[0].images.length, 2);
  assert.equal(result.messages[0].images[0].mime_type, "image/jpeg");
  assert.ok(result.messages[0].images.every(i => i.unavailable && !i.reference));
  assert.equal(f.requests.length, 3); assert.doesNotMatch(JSON.stringify(result), /PRIVATE|evil|gatewayUrl/);
  await assert.rejects(f.api.image(command(), "arbitrary"), e => e.code === "image_unavailable");
  assert.equal(f.requests.length, 3);
});
test("DOM attachment eligibility requires an actual descriptor without guessing server format labels", async () => {
  const attachments = [null, "image", {}, {format: "image/jpeg"}, {gatewayUrl: GATEWAY + "/opaque", format: "JPEG"}];
  const f = fixture({data: conversation({messages: [message(1, {attachments})]})});
  const result = await f.api.get(command(), {conversation_id: "c-1"});
  const images = result.messages[0].images;
  assert.ok(images.slice(0, 4).every(image => image._dom_attachment === undefined));
  assert.equal(images[4]._dom_attachment, true);
  assert.equal(images[4].mime_type, undefined);
  assert.doesNotMatch(JSON.stringify(result), /gatewayUrl|opaque|JPEG/);
  assert.equal(f.requests.length, 3);
});
test("redirects, wrong MIME, expired auth, oversize and network failure never retry or leak errors", async () => {
  for (const options of [
    {override: (u, o, n) => n === 3 ? {url: "https://evil.test/PRIVATE"} : null},
    {override: (u, o, n) => n === 3 ? {mime: "text/html"} : null},
    {override: (u, o, n) => n === 3 ? {reject: true} : null},
    {override: (u, o, n) => n === 3 ? {status: 503} : null},
    {override: (u, o, n) => n === 1 ? {body: '{"padding":"' + "x".repeat(65536) + '"}'} : null},
    {expiration: Math.floor(Date.now() / 1000)}, {token: "PRIVATE\nTOKEN"}
  ]) {
    const f = fixture(options);
    await assert.rejects(f.api.get(command(), {conversation_id: "c-1"}), e => !!e.code && !/PRIVATE/.test(e.message));
    assert.ok(f.requests.length <= 3);
  }
  const f = fixture();
  await assert.rejects(f.api.get(command({expires_at: new Date(0).toISOString()}), {conversation_id: "c-1"}), e => e.code === "command_expired");
  assert.equal(f.requests.length, 0);
});
