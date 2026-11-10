/* Fixed command vocabulary. No eval, scripts, selectors or arbitrary URLs cross IPC. */
(function (root) {
  "use strict";
  const METHODS = new Set(["identify", "recover_tab", "list_conversations", "get_conversation", "prepare_send", "send_step", "confirm_send", "disconnect", "inspect_ui"]);
  const ACCOUNT = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
  const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
  const USER = /^[0-9]{1,20}$/;
  const LISTING = /^[0-9]{6,20}(?:-[0-9]{1,8}){0,2}$/;
  function fail(code) { const error = new Error(code); error.code = code; throw error; }
  function originAllowed(value) {
    try { const u = new URL(value); return u.origin === "https://www.kleinanzeigen.de" && !u.username && !u.password; }
    catch (_) { return false; }
  }
  function assertFresh(command, now = Date.now()) {
    const expires = typeof command.expires_at === "string" ? Date.parse(command.expires_at) : NaN;
    if (!Number.isFinite(expires) || expires <= now) fail("command_expired");
  }
  function validate(command, now = Date.now()) {
    if (!command || typeof command !== "object" || !ID.test(command.id || "") ||
        !ACCOUNT.test(command.account_id || "") || !METHODS.has(command.method) ||
        !command.args || typeof command.args !== "object" || Array.isArray(command.args) ||
        !Number.isInteger(command.args.tab_id) || command.args.tab_id < 0) fail("invalid_command");
    if (command.method !== "identify" && command.method !== "disconnect" && !USER.test(command.user_id || "")) fail("account_unverified");
    assertFresh(command, now);
    return command;
  }
  function targetURL(command) {
    validate(command);
    if (command.method === "disconnect") return null;
    if (command.method === "identify") return "https://www.kleinanzeigen.de/m-meine-anzeigen.html";
    const inboxURL = command.identity_page_path === "/portal/anzeigen" ? "https://www.kleinanzeigen.de/portal/nachrichten" : "https://www.kleinanzeigen.de/m-nachrichten.html";
    if (command.method === "confirm_send") {
      validateConfirmation(command);
      return inboxURL + "?conversationId=" + encodeURIComponent(command.args.conversation_id);
    }
    if (command.method === "inspect_ui") {
      const view = command.args.view || "current";
      if (!["current", "inbox", "listing"].includes(view)) fail("invalid_target");
      if (view === "current") return null;
      if (view === "inbox") return inboxURL;
      if (view === "listing" && command.args.target_kind !== "listing") fail("invalid_target");
    }
    const target = command.method === "send_step" ? command.args.target : command.args;
    if (["prepare_send", "send_step", "inspect_ui"].includes(command.method) && target?.target_kind === "listing") {
      if (!LISTING.test(target.target_id || "")) fail("invalid_target");
      return "https://www.kleinanzeigen.de/s-anzeige/" + target.target_id;
    }
    const conversation = command.method === "get_conversation" ? command.args.conversation_id :
      ["prepare_send", "send_step"].includes(command.method) && target?.target_kind === "conversation" ? target.target_id : null;
    if (conversation !== null) {
      if (!ID.test(conversation || "")) fail("invalid_target");
      return inboxURL + "?conversationId=" + encodeURIComponent(conversation);
    }
    return inboxURL;
  }
  function errorCode(error) {
    const code = error?.code;
    return typeof code === "string" && /^[a-z_]{1,64}$/.test(code) ? code : "safari_command_failed";
  }
  function targetMatches(actual, expected) {
    if (!originAllowed(actual) || !originAllowed(expected)) return false;
    const a = new URL(actual), e = new URL(expected);
    // Observed account-specific business redirect; no other login redirect is
    // accepted as a verified own-account page.
    if (e.pathname === "/m-meine-anzeigen.html" && a.pathname === "/portal/anzeigen") return true;
    if (e.pathname.startsWith("/s-anzeige/")) {
      const listing = path => path.match(/^\/s-anzeige\/(?:[^/]+\/)?([0-9]{6,20})(?:-[0-9]+){0,2}\/?$/)?.[1];
      return !!listing(e.pathname) && listing(a.pathname) === listing(e.pathname);
    }
    return a.pathname === e.pathname && (!e.searchParams.has("conversationId") ||
      (a.searchParams.getAll("conversationId").length === 1 && a.searchParams.get("conversationId") === e.searchParams.get("conversationId")));
  }
  function followTarget(value) {
    if (!originAllowed(value)) return null;
    const url = new URL(value);
    if (!["/m-nachrichten.html", "/portal/nachrichten"].includes(url.pathname) ||
        url.searchParams.getAll("conversationId").length !== 1 || url.searchParams.getAll("messageId").length > 1) return null;
    const conversationId = url.searchParams.get("conversationId"), messageId = url.searchParams.get("messageId");
    if (!ID.test(conversationId || "") || (messageId !== null && !ID.test(messageId))) return null;
    return {conversation_id: conversationId, message_id: messageId};
  }
  function singleFollow(candidates) {
    const unique = new Map();
    for (const candidate of candidates) {
      if (!candidate || !ID.test(candidate.conversation_id || "") ||
          (candidate.message_id != null && !ID.test(candidate.message_id))) fail("outcome_unknown");
      const previous = unique.get(candidate.conversation_id);
      if (previous?.message_id && candidate.message_id && previous.message_id !== candidate.message_id) fail("outcome_unknown");
      unique.set(candidate.conversation_id, {conversation_id: candidate.conversation_id, message_id: candidate.message_id || previous?.message_id || null});
    }
    if (unique.size > 1) fail("outcome_unknown");
    return unique.size ? [...unique.values()][0] : null;
  }
  function preciseTime(value) {
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
  }
  function messageTextEqual(actual, expected) {
    // HTML form submission uses CRLF on the wire. Preserve every other byte of
    // text comparison: no trimming, whitespace folding or Unicode conversion.
    return typeof actual === "string" && typeof expected === "string" &&
      actual.replace(/\r\n/g, "\n") === expected.replace(/\r\n/g, "\n");
  }
  const MAX_PROOF_PIXELS = 4000000;
  function validImagePixels(value) {
    return !!value && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).length === 4 && Object.keys(value).every(key => ["algorithm", "width", "height", "sha256"].includes(key)) && value.algorithm === "srgb-rgba8-v1" &&
      Number.isInteger(value.width) && Number.isInteger(value.height) && value.width >= 1 && value.height >= 1 &&
      value.width <= MAX_PROOF_PIXELS && value.height <= MAX_PROOF_PIXELS && value.width * value.height <= MAX_PROOF_PIXELS &&
      typeof value.sha256 === "string" && /^[a-f0-9]{64}$/.test(value.sha256);
  }
  function imagePixelsEqual(actual, expected) {
    return validImagePixels(actual) && validImagePixels(expected) && actual.width === expected.width && actual.height === expected.height &&
      actual.algorithm === expected.algorithm && actual.sha256 === expected.sha256;
  }
  function validateConfirmation(command) {
    const a = command.args, t = a.target;
    const kind = a.kind || "text";
    if (!["text", "image"].includes(kind) || !t ||
        t.account_user_id !== command.user_id || !USER.test(t.recipient_user_id || "") || t.recipient_user_id === command.user_id ||
        !ID.test(a.conversation_id || "") || (a.expected_message_id != null && !ID.test(a.expected_message_id)) ||
        !preciseTime(a.not_before) ||
        !Array.isArray(a.baseline_message_ids) || a.baseline_message_ids.length > 2000 || a.baseline_message_ids.some(id => !ID.test(id))) fail("invalid_command");
    if (kind === "text") {
      if (t.target_kind !== "listing" || !LISTING.test(t.target_id || "") || t.listing_id !== t.target_id ||
          typeof a.text !== "string" || !a.text.trim() || a.text.length > 20000 || a.expected_image_sha256 != null || a.expected_image_pixels != null) fail("invalid_command");
    } else if (t.target_kind !== "conversation" || t.target_id !== a.conversation_id || t.conversation_id !== a.conversation_id ||
        !LISTING.test(t.listing_id || "") || !ID.test(a.expected_message_id || "") || a.text !== "" ||
        !/^[a-f0-9]{64}$/.test(a.expected_image_sha256 || "") ||
        (a.expected_image_pixels != null && !validImagePixels(a.expected_image_pixels))) fail("invalid_command");
  }
  function confirmationReceipt(snapshot, command) {
    validateConfirmation(command);
    const a = command.args;
    // An image candidate is never confirmed by metadata alone. Its separate
    // fresh-document byte proof is performed only by the content reader.
    if (a.kind === "image") fail("invalid_command");
    if (snapshot.conversation_id !== a.conversation_id || snapshot.recipient_user_id !== a.target.recipient_user_id) fail("outcome_unknown");
    const threshold = Date.parse(a.not_before);
    const matches = snapshot.messages.filter(m => {
      if (!ID.test(m.message_id || "") || a.baseline_message_ids.includes(m.message_id) || m.sender_user_id !== command.user_id ||
          m.direction !== "outbound" || !messageTextEqual(m.text, a.text) || m.images?.length || (a.expected_message_id && m.message_id !== a.expected_message_id)) return false;
      const precise = preciseTime(m.created_at);
      // Explicit new platform message IDs can confirm a receipt without a time;
      // an available timestamp must still agree with this particular dispatch.
      if (precise && (Date.parse(m.created_at) < threshold || Date.parse(m.created_at) > Date.now() + 5000)) return false;
      return !!a.expected_message_id || precise;
    });
    if (matches.length > 1) fail("outcome_unknown");
    if (!matches.length) return null;
    return {message_id: matches[0].message_id, conversation_id: snapshot.conversation_id,
      recipient_user_id: snapshot.recipient_user_id, created_at: matches[0].created_at || null, confirmed: true};
  }
  function pagination(selection, upstreamMore) {
    return {next_cursor: selection.next_cursor, has_more: selection.has_more || upstreamMore,
      coverage: "visible_page", complete: !upstreamMore, upstream_has_more: upstreamMore,
      pagination_limitation: upstreamMore ? "Weitere Inhalte sind in der Oberfläche vorhanden; der Abruf außerhalb der sichtbaren Seite ist noch nicht verifiziert." : null};
  }
  const api = Object.freeze({METHODS, ACCOUNT, ID, USER, LISTING, fail, originAllowed, assertFresh, validate, targetURL, targetMatches, pagination, errorCode,
    followTarget, singleFollow, preciseTime, messageTextEqual, MAX_PROOF_PIXELS, validImagePixels, imagePixelsEqual, validateConfirmation, confirmationReceipt});
  root.KleinanzeigenProtocol = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
