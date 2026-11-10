"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const P = require("../protocol.js");
const SOURCE = fs.readFileSync(path.join(__dirname, "../messaging-read.js"), "utf8");
const ORIGIN = "https://www.kleinanzeigen.de";
const listingURL = ORIGIN + "/s-anzeige/gewerbliche-anzeige/1234567890-1-2";
const profileURL = ORIGIN + "/s-bestandsliste.html?userId=222";
const proURL = ORIGIN + "/pro/Firma-Mueller";
class Node {
  constructor(tag, text = "", attrs = {}) {
    this.tagName = tag.toUpperCase(); this.nodeType = 1; this.attrs = attrs; this.selectors = new Map();
    this.childNodes = text ? [{nodeType: 3, textContent: text}] : [];
  }
  getAttribute(name) { return this.attrs[name] ?? null; }
  querySelectorAll(selector) { return this.selectors.get(selector) || []; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  select(selector, nodes) { this.selectors.set(selector, nodes); return this; }
}
function documents({seller = "222", peer = "222", imprint = "Firma Müller\nHauptstraße 1\nVerantwortlich: Änne Müller", profileText = "Änne Müller\nGewerblicher Nutzer\nAktiv seit 2020", duplicate = false,
  providerNodes = true, profileLinkText = "Firma Müller", imprintTrigger = false, pro = false,
  proLinks = [proURL, proURL], proCanonical = proURL, proActionId = "222", proSellerId = 222,
  proActionBody = null, duplicateProof = false, missingProof = false, foreignComponent = false,
  proBrandName = undefined, proAbout = undefined, proContactPerson = undefined, expired = false} = {}) {
  const listing = new Node("html"), profile = new Node("html"), proPage = new Node("html");
  const root = new Node("div"), name = new Node("span", "Firma Müller"), details = new Node("span", "Gewerblicher Nutzer");
  root.select("a[href]", seller == null ? [] : [new Node("a", profileLinkText, {href: "/s-bestandsliste.html?userId=" + seller})]);
  root.select(".userprofile-vip, .userprofile-vip-details-text, .userprofile-vip-badges, #viewad-contact-phone", providerNodes ? [name, details] : []);
  listing.select("#viewad-contact", [root]);
  listing.select("#viewad-imprint #viewad-imprint-text", imprint == null ? [] : [new Node("p", imprint)]);
  listing.select("#viewad-imprint-link", imprintTrigger ? [new Node("button", "Rechtliche Angaben")] : []);
  listing.select("#srchrslt-adexpired", expired ? [new Node("section", "Anzeige nicht mehr verfügbar")] : []);
  const businessHeader = new Node("div").select("a[href]", proLinks.map(href => new Node("a", profileLinkText, {href})));
  listing.select("#viewad-profile-box #viewad-bizteaser .bizteaser--header", pro ? [businessHeader] : []);
  // The site footer and message-form data are deliberately outside the scopes.
  listing.childNodes.push(new Node("footer", "SITE OPERATOR IMPRESSUM"), new Node("form", "PRIVATE FORM DATA"));
  const profileRoot = new Node("section", profileText, {"data-followed-user": peer});
  profileRoot.childNodes.push(new Node("script", "PRIVATE CREDENTIALS"), new Node("button", "Nachricht senden"));
  profile.select(".userprofile[data-followed-user]", duplicate ? [profileRoot, profileRoot] : [profileRoot]);
  profile.select("#userprofile-info .userprofile--info-content", [new Node("div", "Weitere öffentlich angezeigte Informationen")]);
  proPage.select('link[rel="canonical"]', [new Node("link", "", {href: proCanonical})]);
  const action = new Node("astro-island", "", {"component-url": (foreignComponent ? "https://foreign.invalid" : "") + "/frontend-web/_pro-public-web/assets/ProfileActions.ObservedHash.js",
    "component-export": "default", props: proActionBody ?? JSON.stringify({userId: [0, proActionId], currentUser: [0, "PRIVATE"]})});
  const brand = new Node("astro-island", "", {"component-url": "/frontend-web/_pro-public-web/assets/BrandProfilePage.ObservedHash.js",
    "component-export": "default", props: JSON.stringify({sellerId: [0, proSellerId], initialAds: [1, "PRIVATE ADS"],
      brandName: proBrandName, about: proAbout, contactPerson: proContactPerson})});
  proPage.select("astro-island[component-url][props]", [action, ...(!missingProof ? [brand] : []), ...(duplicateProof ? [action] : [])]);
  return {listing, profile, pro: proPage};
}
function fixture(options = {}) {
  const docs = documents(options), requests = []; let elapsed = 0;
  class ClockDate extends Date { static now() { return Date.now() + elapsed; } }
  const window = {}; window.top = window;
  const context = vm.createContext({KleinanzeigenProtocol: P, URL, Date: ClockDate, TextDecoder, TextEncoder, Uint8Array, AbortController,
    window, location: new URL(ORIGIN + "/m-nachrichten.html"), setTimeout: () => 1, clearTimeout: () => {},
    document: {createElement(tag) { assert.equal(tag, "template"); return {set innerHTML(body) {this.content = docs[body] || options.htmlFragments?.[body];}};}},
    fetch: async (url, args) => {
      requests.push({url, args}); elapsed += options.elapsedPerRequest || 0;
      const listing = url.includes("/zur-anzeige/"), pro = url.includes("/pro/");
      const body = options.body || (listing ? "listing" : pro ? "pro" : "profile"), status = (pro ? options.proStatus : null) || options.status || 200;
      if (options.network) throw Error("PRIVATE NETWORK DETAILS");
      return {url: options.finalURL || (listing ? listingURL : pro ? options.proFinalURL || proURL : profileURL), status, ok: status === 200,
        headers: {get: () => options.mime || "text/html; charset=UTF-8"},
        body: new ReadableStream({start(c) {c.enqueue(new TextEncoder().encode(body));c.close();}})};
    }});
  vm.runInContext(SOURCE, context);
  const command = {id: "context-1", method: "get_conversation", account_id: "private", user_id: "111",
    expires_at: new Date(Date.now() + 45000).toISOString(), args: {tab_id: 1, conversation_id: "conversation-1"}};
  const snapshot = {account_user_id: "111", recipient_user_id: "222", listing_id: "1234567890", conversation_id: "conversation-1", _view: {role: "Buyer"}};
  return {requests, docs, command, snapshot, run: (changes = {}) => context.KleinanzeigenMessagingRead.providerContext(command, {...snapshot, ...changes})};
}
const json = value => JSON.parse(JSON.stringify(value));

test("verified listing provider, collapsed imprint and public profile are separately archived", async () => {
  const f = fixture(), result = json(await f.run());
  assert.deepEqual(result.sections.map(x => [x.kind, x.status]), [["listing_provider", "captured"], ["listing_imprint", "captured"], ["profile", "captured"]]);
  assert.equal(result.recipient_user_id, "222"); assert.equal(result.listing_id, "1234567890");
  assert.match(result.sections[1].text, /Änne Müller/);
  assert.match(result.sections[2].text, /Weitere öffentlich angezeigte/);
  for (const section of result.sections) { assert.equal(section.user_id, "222"); assert.ok(P.preciseTime(section.captured_at)); }
  assert.equal(result.sections[1].source_url, listingURL); assert.equal(result.sections[2].source_url, profileURL);
  assert.doesNotMatch(JSON.stringify(result), /SITE OPERATOR|PRIVATE|Nachricht senden/);
  assert.equal(f.requests.length, 2);
  for (const {args} of f.requests) { assert.equal(args.method, "GET"); assert.equal(args.credentials, "omit"); assert.equal(args.body, undefined); assert.equal(args.headers.Authorization, undefined); }
});

test("own listings never assign own seller details or imprint to the chat partner", async () => {
  const f = fixture({seller: "111"}), result = json(await f.run({_view: {role: "Seller"}}));
  assert.equal(f.requests.length, 1); assert.equal(f.requests[0].url, profileURL);
  assert.deepEqual(result.sections.slice(0, 2).map(x => x.status), ["not_counterpart", "not_counterpart"]);
  assert.ok(result.sections.slice(0, 2).every(x => x.text === undefined && x.source_url === null));
  assert.equal(result.sections[2].status, "captured");
});

test("listing or profile identity disagreement does not leak another party's text", async () => {
  for (const options of [{seller: "333"}, {peer: "333"}, {duplicate: true}]) {
    const result = json(await fixture(options).run());
    const failed = options.seller ? result.sections.slice(0, 2) : result.sections.slice(2);
    assert.ok(failed.every(x => x.status === "identity_mismatch" && x.text === undefined));
  }
  for (const finalURL of [ORIGIN + "/s-anzeige/falsche-anzeige/9999999999-1-2", "https://foreign.invalid/s-bestandsliste.html?userId=222", ORIGIN + "/impressum.html", profileURL + "&userId=333"]) {
    const result = json(await fixture({finalURL}).run());
    assert.ok(result.sections.every(x => x.status !== "captured"));
    assert.doesNotMatch(JSON.stringify(result), /Firma Müller/);
  }
});

test("provider failures expose only the exact fixed verification station", async () => {
  const cases = [
    {options: {finalURL: ORIGIN + "/s-anzeige/falsche-anzeige/9999999999-1-2"}, guard: "listing_url"},
    {options: {}, missingRoot: true, guard: "provider_root"},
    {options: {seller: "333"}, guard: "conflicting_numeric_id"},
    {options: {seller: null}, guard: "no_numeric_or_business_link"},
    {options: {seller: null, pro: true, proCanonical: ORIGIN + "/pro/Other"}, guard: "pro_url"},
    {options: {seller: null, pro: true, proSellerId: 333}, guard: "pro_identity_components"},
  ];
  for (const item of cases) {
    const f = fixture(item.options);
    if (item.missingRoot) f.docs.listing.select("#viewad-contact", []);
    const result = json(await f.run());
    for (const section of result.sections.slice(0, 2)) {
      assert.equal(section.status, "identity_mismatch"); assert.equal(section.provider_guard, item.guard);
      assert.equal(section.text, undefined); assert.equal(section.source_url, null);
    }
  }
  const success = await fixture({seller: null, pro: true}).run();
  assert.ok(success.sections.every(section => section.provider_guard === undefined));
});

test("an absent provider imprint never falls back to the platform footer", async () => {
  const result = await fixture({imprint: null}).run();
  assert.equal(result.sections[1].status, "not_present"); assert.equal(result.sections[1].text, "");
  assert.equal(result.sections[0].status, "captured"); assert.equal(result.sections[2].status, "captured");
});

test("the observed expired-ad marker reports unavailable without assigning stale provider content", async () => {
  const f = fixture({expired: true, pro: true}), result = json(await f.run());
  for (const item of result.sections.slice(0, 2)) {
    assert.equal(item.status, "unavailable"); assert.equal(item.error_code, "page_unavailable");
    assert.equal(item.source_url, null); assert.equal(item.text, undefined);
  }
  assert.equal(result.sections[2].status, "captured");
  assert.deepEqual(f.requests.map(x => x.url), [ORIGIN + "/zur-anzeige/1234567890", profileURL]);
});

test("provider name fallback uses only already verified counterpart profile anchors", async () => {
  const f = fixture({providerNodes: false}), result = json(await f.run());
  assert.equal(result.sections[0].status, "captured"); assert.equal(result.sections[0].text, "Firma Müller");
  assert.equal(result.sections[0].structure, undefined);
  const mismatched = await fixture({providerNodes: false, seller: "333"}).run();
  assert.equal(mismatched.sections[0].status, "identity_mismatch"); assert.equal(mismatched.sections[0].text, undefined);
});

test("empty verified provider or present-but-empty imprint is unavailable with counts only", async () => {
  for (const options of [{providerNodes: false, profileLinkText: "", imprint: ""}, {providerNodes: false, profileLinkText: "", imprint: null, imprintTrigger: true}]) {
    const f = fixture(options), result = json(await f.run());
    assert.equal(result.sections[0].status, "unavailable"); assert.equal(result.sections[1].status, "unavailable");
    for (const item of result.sections.slice(0, 2)) {
      assert.deepEqual(Object.keys(item.structure).sort(), ["body_bytes", "imprint_nodes", "imprint_text_chars", "imprint_triggers", "profile_links", "provider_nodes", "provider_text_chars"]);
      assert.ok(Object.values(item.structure).every(value => Number.isInteger(value) && value >= 0));
      assert.equal(item.structure.profile_links, 1); assert.equal(item.structure.provider_text_chars, 0);
      assert.doesNotMatch(JSON.stringify(item.structure), /Müller|222|https|PRIVATE/);
    }
  }
});

test("profile vanity redirects require an explicit matching numeric profile root", async () => {
  const result = await fixture({finalURL: ORIGIN + "/pro/firma-mueller"}).run({_view: {role: "Seller"}});
  assert.equal(result.sections[2].status, "captured");
  const bad = await fixture({finalURL: ORIGIN + "/pro/firma-mueller", peer: "333"}).run({_view: {role: "Seller"}});
  assert.equal(bad.sections[2].status, "identity_mismatch");
});

test("the observed business header requires both independent Astro seller IDs and exact canonical URL", async () => {
  const f = fixture({seller: null, pro: true}), result = json(await f.run());
  assert.deepEqual(result.sections.map(x => x.status), ["captured", "captured", "captured"]);
  assert.deepEqual(f.requests.map(x => x.url), [ORIGIN + "/zur-anzeige/1234567890", proURL, profileURL]);
  assert.ok(f.requests.every(x => x.args.credentials === "omit"));
  assert.equal(result.sections[1].source_url, listingURL);
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE|currentUser|initialAds/);
});

test("business proof never overrides numeric conflicts or accepts loose vanity evidence", async () => {
  for (const options of [
    {proActionId: "333"}, {proSellerId: 333}, {missingProof: true}, {duplicateProof: true}, {foreignComponent: true},
    {proCanonical: ORIGIN + "/pro/Other"}, {proFinalURL: ORIGIN + "/pro/Other"},
    {proActionBody: '{"userId":[0,"333"],"userId":[0,"222"]}'},
    {proActionBody: '{"userId":[1,"222"],"initialAds":{"sellerId":"222"}}'},
  ]) {
    const f = fixture({seller: null, pro: true, ...options}), result = json(await f.run());
    assert.deepEqual(result.sections.slice(0, 2).map(x => x.status), ["identity_mismatch", "identity_mismatch"]);
    assert.equal(result.sections[2].status, "captured"); assert.equal(f.requests.length, 3);
    assert.ok(result.sections.slice(0, 2).every(x => x.text === undefined));
  }
  for (const options of [{seller: "333"}, {seller: "222&userId=333"}, {seller: null, proLinks: [proURL, ORIGIN + "/pro/Other"]},
    {seller: null, proLinks: [proURL + "?userId=222"]}, {seller: null, proLinks: [proURL, proURL + "?userId=333"]}]) {
    const f = fixture({pro: true, ...options}), result = await f.run();
    assert.equal(result.sections[0].status, "identity_mismatch"); assert.equal(f.requests.length, 2);
  }
});

test("the extra public business proof shares the deadline and stops on a blocking response", async () => {
  const blocked = fixture({seller: null, pro: true, proStatus: 429}), result = await blocked.run();
  assert.equal(result.upstream_error, "upstream_rate_limited"); assert.equal(blocked.requests.length, 2);
  assert.ok(result.sections.every(x => x.status === "unavailable"));
  const slow = fixture({seller: null, pro: true, elapsedPerRequest: 5000}), limited = await slow.run();
  assert.equal(slow.requests.length, 2); assert.ok(limited.sections.every(x => x.status === "unavailable"));
});

test("verified PRO about data and the numeric profile retain their exact respective sources", async () => {
  const f = fixture({seller: null, pro: true, proBrandName: [0, "Firma Müller"],
    proAbout: [0, {description: [0, "Dienstleistungen in Jena\nGrüße von Änne"]}], proContactPerson: [0]}), result = json(await f.run());
  const profile = result.sections[2];
  assert.equal(profile.status, "captured"); assert.equal(profile.source_url, proURL); assert.equal(profile.truncated, false);
  assert.ok(profile.text.includes("Quelle: " + proURL)); assert.ok(profile.text.includes("Quelle: " + profileURL));
  assert.match(profile.text, /Über uns\nDienstleistungen in Jena\nGrüße von Änne/);
  assert.match(profile.text, /Aktiv seit 2020/); assert.doesNotMatch(profile.text, /PRIVATE|sellerId|initialAds/);
  assert.equal(f.requests.length, 3);
});

test("PRO contact objects remain unsupported instead of leaking unknown nested fields", async () => {
  const result = await fixture({seller: null, pro: true, proBrandName: [0, "Firma Müller"],
    proContactPerson: [0, {fullName: [0, "PRIVATE NAME"], token: [0, "PRIVATE TOKEN"]}]}).run();
  assert.equal(result.sections[2].truncated, true); assert.match(result.sections[2].text, /noch nicht unterstützte Struktur/);
  assert.doesNotMatch(result.sections[2].text, /PRIVATE|fullName|token/);
  const textual = await fixture({seller: null, pro: true, proContactPerson: [0, "Öffentliche Ansprechpartnerin"]}).run();
  assert.match(textual.sections[2].text, /Kontaktperson\nÖffentliche Ansprechpartnerin/);
});

test("PRO description markup is parsed in an inert template with scripts excluded", async () => {
  const markup = "<p>Grüße aus Jena</p><script>PRIVATE SCRIPT</script>";
  const fragment = new Node("fragment"); fragment.childNodes = [new Node("p", "Grüße aus Jena"), new Node("script", "PRIVATE SCRIPT"), new Node("iframe", "PRIVATE FRAME")];
  const result = await fixture({seller: null, pro: true, proAbout: [0, {description: [0, markup]}], htmlFragments: {[markup]: fragment}}).run();
  assert.match(result.sections[2].text, /Grüße aus Jena/); assert.doesNotMatch(result.sections[2].text, /PRIVATE|<p>|<script>/);
});

test("long PRO descriptions keep the existing profile budget and an explicit truncation marker", async () => {
  const result = await fixture({seller: null, pro: true, proAbout: [0, {description: [0, "Grüße 😀 ".repeat(10000)]}]}).run();
  assert.equal(result.sections[2].truncated, true);
  assert.ok(new TextEncoder().encode(JSON.stringify(result.sections[2].text)).byteLength <= 3500);
  assert.ok(new TextEncoder().encode(JSON.stringify(result)).byteLength < 12000);
});

test("unavailable HTML is explicit and bounded, without retrying or disclosing request errors", async () => {
  for (const options of [{network: true}, {status: 404}, {mime: "application/json"}, {body: "x".repeat(2000001)}]) {
    const f = fixture(options), result = json(await f.run());
    assert.ok(result.sections.every(x => x.status === "unavailable"));
    assert.equal(f.requests.length, 2); assert.doesNotMatch(JSON.stringify(result), /PRIVATE/);
  }
  const slow = fixture({elapsedPerRequest: 11000}), result = await slow.run();
  assert.equal(slow.requests.length, 1); assert.ok(result.sections.every(x => x.status === "unavailable"));
});

test("context serialization fits the reserved native budget even with Unicode and escapes", async () => {
  const f = fixture({imprint: 'Änne 😀 "'.repeat(10000), profileText: 'Straße 😀 "'.repeat(10000)});
  const result = json(await f.run());
  assert.ok(new TextEncoder().encode(JSON.stringify(result)).byteLength < 12000);
  assert.equal(result.sections[1].truncated, true); assert.equal(result.sections[2].truncated, true);
  assert.ok(!/[\uD800-\uDBFF]$/.test(result.sections[1].text));
});

test("invalid conversation binding prevents all public fetches", async () => {
  for (const changes of [{recipient_user_id: "111"}, {account_user_id: "999"}, {listing_id: "bad"}, {conversation_id: "wrong"}, {_view: {role: "unknown"}}]) {
    const f = fixture(); await assert.rejects(f.run(changes), error => error.code === "conversation_unverified"); assert.equal(f.requests.length, 0);
  }
});

test("block responses stop all additional fetches and preserve a circuit-breaker signal", async () => {
  for (const [status, code] of [[403, "upstream_blocked"], [429, "upstream_rate_limited"]]) {
    const f = fixture({status}), result = json(await f.run());
    assert.equal(result.upstream_error, code); assert.equal(f.requests.length, 1);
    assert.ok(result.sections.every(x => x.status === "unavailable"));
  }
});
