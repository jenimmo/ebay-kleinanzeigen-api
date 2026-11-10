/* Fixed read-only website calls. Credentials remain in the isolated Safari world. */
(function (root) {
  "use strict";
  const P = root.KleinanzeigenProtocol;
  const ORIGIN = "https://www.kleinanzeigen.de";
  const GATEWAY = "https://gateway.kleinanzeigen.de";
  const references = new Map();
  const JSON_MAX = 2000000;
  const IMAGE_MAX = 20 * 1024 * 1024;
  const PAGE_BYTES = 45000;
  const MAX_MESSAGES = 5000;
  function object(value) { return !!value && typeof value === "object" && !Array.isArray(value); }
  function shapeType(value) {
    return value === undefined ? "missing" : value === null ? "null" : Array.isArray(value) ? "array" :
      ["object", "string", "number", "boolean"].includes(typeof value) ? typeof value : "other";
  }
  function fieldTypes(value, names) {
    return Object.fromEntries(names.map(name => [name, shapeType(object(value) ? value[name] : undefined)]));
  }
  function diagnosticFail(code, diagnostic, reason, details = {}) {
    try { P.fail(code); }
    catch (error) {
      if (diagnostic) error.reader_structure = {...diagnostic, reason, ...details};
      throw error;
    }
  }
  function listShape(raw, page, size) {
    const names = ["numFound", "pageNum", "pageSize"];
    const meta = object(raw._meta) ? raw._meta : {};
    return {operation: "list", requested_page: page, requested_size: size,
      root_types: fieldTypes(raw, ["conversations", "_meta", "_links", "lastModified"]),
      meta_types: fieldTypes(meta, names),
      meta_values: Object.fromEntries(names.filter(name => Number.isSafeInteger(meta[name]) && meta[name] >= 0 && meta[name] <= 1000000).map(name => [name, meta[name]])),
      ...(Array.isArray(raw.conversations) ? {conversation_count: Math.min(raw.conversations.length, 1000), conversation_count_limited: raw.conversations.length > 1000} : {})};
  }
  function itemShape(diagnostic, item, index, stage) {
    return {...diagnostic, stage, item_index: index,
      item_types: fieldTypes(item, ["id", "adId", "role", "userIdBuyer", "userIdSeller", "unread", "unreadMessagesCount", "adTitle", "receivedDate"])};
  }
  function timestampShape(value) {
    if (value == null) return "missing";
    if (typeof value !== "string" && typeof value !== "number") return "other";
    const shape = String(value);
    if (/^[1-9][0-9]{12}$/.test(shape)) return "numeric_epoch_ms";
    if (/^[1-9][0-9]{9}$/.test(shape)) return "numeric_epoch_s";
    if (/^\d{4}-\d{2}-\d{2}$/.test(shape)) return "date_only";
    const start = "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d+)?";
    if (new RegExp(start + "(?:Z|[+-]\\d{2}:\\d{2})$").test(shape)) return "iso_zoned";
    if (new RegExp(start + "$").test(shape)) return "iso_unzoned";
    if (new RegExp(start + "[+-]\\d{4}$").test(shape)) return "iso_offset_compact";
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$/.test(shape)) return "iso_space_zoned";
    return "other";
  }
  function id(value, code = "conversation_unverified") {
    if (typeof value !== "string" || !P.ID.test(value)) P.fail(code);
    return value;
  }
  function user(value) {
    const normalized = typeof value === "number" && Number.isSafeInteger(value) ? String(value) : value;
    if (typeof normalized !== "string" || !P.USER.test(normalized)) P.fail("account_unverified");
    return normalized;
  }
  function text(value, maximum = 20000) {
    if (typeof value !== "string" || value.length > maximum) P.fail("message_text_unverified");
    return value;
  }
  function timestamp(value, diagnostic = null) {
    const zoned = typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$/.test(value);
    const parseable = typeof value === "string" && Number.isFinite(Date.parse(value));
    if (!zoned || !parseable) diagnosticFail("ui_unsupported", diagnostic, "item_timestamp", {timestamp_parseable: parseable, timestamp_iso_zoned: zoned, timestamp_format: timestampShape(value)});
    return new Date(value).toISOString();
  }
  function checkCommand(command) {
    P.validate(command);
    if (window.top !== window || !P.originAllowed(location.href) || user(command.user_id) !== command.user_id) P.fail("account_unverified");
    P.assertFresh(command);
  }
  function secret(value) {
    if (typeof value !== "string" || !value || value.length > 8192 || /[^\x21-\x7e ]/.test(value) || value.trim() !== value) P.fail("reauth_required");
    return value;
  }
  function uniqueRootKeys(body, diagnostic = null) {
    const keys = new Set();
    let depth = 0;
    for (let index = 0; index < body.length; index++) {
      if (body[index] === '"') {
        const start = index;
        while (++index < body.length) {
          if (body[index] === "\\") index++;
          else if (body[index] === '"') break;
        }
        let next = index + 1;
        while (next < body.length && /\s/.test(body[next])) next++;
        if (depth === 1 && body[next] === ":") {
          const key = JSON.parse(body.slice(start, index + 1));
          if (keys.has(key)) diagnosticFail("ui_unsupported", diagnostic, "duplicate_root_key", {duplicate_root_key: true});
          keys.add(key);
        }
      } else if (body[index] === "{" || body[index] === "[") depth++;
      else if (body[index] === "}" || body[index] === "]") depth--;
    }
  }
  async function request(command, url, headers, maximum, image = false, diagnostic = null) {
    checkCommand(command);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), Math.min(15000, Math.max(1, Date.parse(command.expires_at) - Date.now())));
    try {
      let response;
      try { response = await fetch(url, {method: "GET", credentials: "include", cache: "no-store", redirect: "error", headers, signal: controller.signal}); }
      catch (_) { P.assertFresh(command); diagnosticFail("reauth_required", diagnostic, "network"); }
      if (diagnostic && Number.isInteger(response.status) && response.status >= 100 && response.status <= 599) diagnostic = {...diagnostic, http_status: response.status};
      if (response.status === 429) P.fail("upstream_rate_limited");
      if (response.status === 403) P.fail("upstream_blocked");
      if (response.status === 401 || response.redirected || response.url !== url) P.fail("reauth_required");
      if (!response.ok || !response.body) diagnosticFail(image ? "image_unavailable" : "ui_unsupported", diagnostic, !response.ok ? "http_status" : "body_unavailable");
      const mime = (response.headers.get("content-type") || "").split(";", 1)[0].trim().toLowerCase();
      if (diagnostic) diagnostic = {...diagnostic, json_mime: mime === "application/json"};
      if (image ? !["image/png", "image/jpeg", "image/webp", "image/gif"].includes(mime) : mime !== "application/json") diagnosticFail(image ? "image_unavailable" : "ui_unsupported", diagnostic, "content_type");
      const reader = response.body.getReader(), chunks = [];
      let length = 0;
      while (true) {
        P.assertFresh(command);
        let item;
        try { item = await reader.read(); } catch (_) { P.assertFresh(command); diagnosticFail(image ? "image_unavailable" : "ui_unsupported", diagnostic, "body_read"); }
        if (item.done) break;
        length += item.value.byteLength;
        if (length > maximum) { controller.abort(); await reader.cancel().catch(() => {}); diagnosticFail(image ? "image_too_large" : "response_too_large", diagnostic, "response_size"); }
        chunks.push(item.value);
      }
      P.assertFresh(command);
      const bytes = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      if (image) return {bytes, mime_type: mime};
      let data;
      try {
        const body = new TextDecoder("utf-8", {fatal: true}).decode(bytes);
        data = JSON.parse(body); uniqueRootKeys(body, diagnostic);
      }
      catch (error) { if (error.reader_structure) throw error; diagnosticFail("ui_unsupported", diagnostic, "json_parse"); }
      if (!object(data)) diagnosticFail("ui_unsupported", diagnostic, "root_type", {body_type: shapeType(data)});
      return {data, headers: response.headers};
    } finally { clearTimeout(timeout); }
  }
  async function session(command, diagnostic = null) {
    const profile = (await request(command, ORIGIN + "/m-mein-profil.json", {Accept: "application/json"}, 65536, false, diagnostic ? {...diagnostic, stage: "profile_request"} : null)).data;
    if (typeof profile.userId !== "string" || user(profile.userId) !== command.user_id) P.fail("account_mismatch");
    const csrf = secret(profile.csrfToken);
    const token = await request(command, ORIGIN + "/m-access-token.json", {Accept: "application/json", "X-CSRF-TOKEN": csrf}, 16384, false, diagnostic ? {...diagnostic, stage: "token_request"} : null);
    const authorization = secret(token.headers.get("authorization"));
    if (typeof token.data.expiration !== "number" || !Number.isFinite(token.data.expiration) || token.data.expiration <= Date.now()) P.fail("reauth_required");
    return {authorization};
  }
  function accountConversation(raw, command) {
    if (!object(raw)) P.fail("conversation_unverified");
    const buyer = user(raw.userIdBuyer), seller = user(raw.userIdSeller);
    if (buyer === seller || !["Buyer", "Seller"].includes(raw.role) || (raw.role === "Buyer" ? buyer : seller) !== command.user_id) P.fail("account_mismatch");
    const listing = typeof raw.adId === "number" && Number.isSafeInteger(raw.adId) ? String(raw.adId) : raw.adId;
    if (typeof listing !== "string" || !P.LISTING.test(listing)) P.fail("conversation_unverified");
    return {conversation_id: id(raw.id), recipient_user_id: raw.role === "Buyer" ? seller : buyer, listing_id: listing};
  }
  function limit(args) {
    const value = args.limit ?? 50;
    if (!Number.isInteger(value) || value < 1 || value > 200) P.fail("invalid_limit");
    return value;
  }
  function headers(auth) { return {Accept: "application/json", Authorization: auth.authorization, "X-ECG-USER-AGENT": "messagebox-1"}; }
  async function list(command, args = {}) {
    checkCommand(command);
    const size = limit(args), unread = args.unread_only ?? false;
    if (typeof unread !== "boolean") P.fail("invalid_command");
    const prefix = `ka-list-v1:${command.account_id}:${command.user_id}:${size}:${unread ? 1 : 0}:`;
    let page = 0, offset = 0, anchor = "-";
    if (args.cursor != null) {
      if (typeof args.cursor !== "string" || !args.cursor.startsWith(prefix)) P.fail("cursor_expired");
      const parts = args.cursor.slice(prefix.length).split(":");
      if (parts.length !== 3 || !/^(?:0|[1-9][0-9]{0,5})$/.test(parts[0]) || !/^(?:0|[1-9][0-9]{0,2})$/.test(parts[1])) P.fail("cursor_expired");
      page = Number(parts[0]); offset = Number(parts[1]);
      try { anchor = decodeURIComponent(parts[2]); } catch (_) { P.fail("cursor_expired"); }
      if (offset >= size || (offset === 0 ? anchor !== "-" : !P.ID.test(anchor))) P.fail("cursor_expired");
    }
    const auth = await session(command, {operation: "list"});
    const url = `${GATEWAY}/messagebox/api/users/${command.user_id}/conversations?page=${page}&size=${size}`;
    const raw = (await request(command, url, headers(auth), JSON_MAX, false, {operation: "list", stage: "list_request"})).data;
    const diagnostic = listShape(raw, page, size);
    if (!Array.isArray(raw.conversations) || raw.conversations.length > size || !object(raw._meta) || !Number.isSafeInteger(raw._meta.numFound) || raw._meta.numFound < 0)
      diagnosticFail("ui_unsupported", {...diagnostic, stage: "list_shape"}, !Array.isArray(raw.conversations) ? "conversations_type" : raw.conversations.length > size ? "conversation_count" : !object(raw._meta) ? "metadata_type" : "num_found");
    if ((raw._meta.pageNum != null && raw._meta.pageNum !== page) ||
        (raw._meta.pageSize != null && raw._meta.pageSize !== size) ||
        raw._meta.numFound < page * size + raw.conversations.length ||
        (!raw.conversations.length && raw._meta.numFound > page * size))
      diagnosticFail("ui_unsupported", {...diagnostic, stage: "list_meta"}, raw._meta.pageNum != null && raw._meta.pageNum !== page ? "page_number" : raw._meta.pageSize != null && raw._meta.pageSize !== size ? "page_size" : raw._meta.numFound < page * size + raw.conversations.length ? "found_count" : "empty_page");
    const seen = new Set();
    const all = raw.conversations.map((item, index) => {
      try {
        const value = accountConversation(item, command);
        if (seen.has(value.conversation_id)) P.fail("conversation_unverified");
        seen.add(value.conversation_id);
        if (typeof item.unread !== "boolean" && (!Number.isSafeInteger(item.unreadMessagesCount) || item.unreadMessagesCount < 0))
          diagnosticFail("ui_unsupported", itemShape(diagnostic, item, index, "list_unread"), "item_unread");
        return {...value, title: text(item.adTitle, 4096), unread: item.unread === true || item.unreadMessagesCount > 0,
          updated_at: item.receivedDate == null ? null : timestamp(item.receivedDate, itemShape(diagnostic, item, index, "list_timestamp"))};
      } catch (error) {
        if (!error.reader_structure) error.reader_structure = {...itemShape(diagnostic, item, index, "list_item"), reason: "item_normalization"};
        throw error;
      }
    });
    if (offset > all.length || (offset && all[offset - 1].conversation_id !== anchor)) P.fail("cursor_expired");
    const conversations = [];
    let consumed = offset, bytes = 0;
    for (; consumed < all.length; consumed++) {
      const value = all[consumed];
      if (unread && !value.unread) continue;
      const needed = new TextEncoder().encode(JSON.stringify(value)).byteLength;
      if (needed > PAGE_BYTES) P.fail("response_too_large");
      if (bytes + needed > PAGE_BYTES) break;
      conversations.push(value); bytes += needed;
    }
    const next = consumed < all.length ? `${page}:${consumed}:${encodeURIComponent(all[consumed - 1].conversation_id)}` :
      (page + 1) * size < raw._meta.numFound ? `${page + 1}:0:-` : null;
    const more = next !== null;
    return {conversations, next_cursor: more ? prefix + next : null, has_more: more,
      coverage: "page", complete: !more, account_user_id: command.user_id, _upstream_reached: true};
  }
  function clearExpired() {
    const now = Date.now();
    for (const [key, value] of references) if (value.expires <= now) references.delete(key);
  }
  function attachmentURL(value, messageId) {
    // The exact attachment route must be independently observed before enabling
    // image fetching; unknown server URLs are never treated as arbitrary fetches.
    return null;
  }
  function attach(raw, messageId, index, command, auth) {
    const item = {remote_id: `${messageId}:${index}`, unavailable: true};
    if (!object(raw)) return item;
    // The page's image component consumes this field. Retain only the fact
    // that a real attachment descriptor exists; never export or fetch its URL.
    if (typeof raw.gatewayUrl === "string" && raw.gatewayUrl.length > 0 && raw.gatewayUrl.length <= 8192) item._dom_attachment = true;
    if (["image/png", "image/jpeg", "image/webp", "image/gif"].includes(raw.format)) item.mime_type = raw.format;
    const url = attachmentURL(raw.gatewayUrl, messageId);
    if (!url || !["image/png", "image/jpeg", "image/webp", "image/gif"].includes(raw.format)) return item;
    const reference = crypto.randomUUID(), expires = Date.parse(command.expires_at);
    while (references.size >= 100) references.delete(references.keys().next().value);
    references.set(reference, {url, authorization: auth.authorization, account_id: command.account_id, user_id: command.user_id, command_id: command.id, expires});
    setTimeout(() => references.delete(reference), Math.max(1, expires - Date.now()));
    return {...item, reference};
  }
  async function get(command, args = {}) {
    checkCommand(command); clearExpired();
    const conversationId = id(args.conversation_id), size = limit(args);
    const auth = await session(command);
    const url = `${GATEWAY}/messagebox/api/users/${command.user_id}/conversations/${encodeURIComponent(conversationId)}?contentWarnings=true`;
    const raw = (await request(command, url, headers(auth), JSON_MAX)).data;
    const target = accountConversation(raw, command);
    if (target.conversation_id !== conversationId || !Array.isArray(raw.messages) || raw.messages.length > MAX_MESSAGES) P.fail("conversation_unverified");
    const seen = new Set(), supported = [], viewMessages = [];
    let unsupported = 0;
    for (const item of raw.messages) {
      if (!object(item)) P.fail("ui_unsupported");
      const messageId = id(item.messageId, "message_sender_unverified");
      if (seen.has(messageId)) P.fail("message_sender_unverified");
      seen.add(messageId);
      if (item.type !== "MESSAGE") {
        unsupported++; viewMessages.push({message_id: messageId, direction: null, type: "unsupported"});
        if (typeof item.type !== "string" || !/^[A-Za-z0-9_.:-]{1,80}$/.test(item.type)) P.fail("ui_unsupported");
        let created = null;
        try { if (item.receivedDate != null) created = timestamp(item.receivedDate); } catch (_) { /* Unknown event timestamps stay unknown. */ }
        const event = {event_id: messageId, event_type: item.type, created_at: created};
        if (typeof item.textShort === "string" && item.textShort.length <= 20000) event.text = item.textShort;
        // No sender is invented and no arbitrary payment/profile fields escape.
        // complete stays false because these event variants are not fully read.
        supported.push({_event: event, message_id: messageId, created_at: created});
        continue;
      }
      if (!["INBOUND", "OUTBOUND"].includes(item.boundness)) P.fail("message_sender_unverified");
      if (item.attachments != null && (!Array.isArray(item.attachments) || item.attachments.length > 20)) P.fail("image_unavailable");
      if (item.textShort == null && !item.attachments?.length) P.fail("message_text_unverified");
      viewMessages.push({message_id: messageId, direction: item.boundness.toLowerCase(), type: "message"});
      supported.push({message_id: messageId, sender_user_id: item.boundness === "OUTBOUND" ? command.user_id : target.recipient_user_id,
        direction: item.boundness.toLowerCase(), text: text(item.textShort ?? ""), created_at: timestamp(item.receivedDate),
        _attachments: item.attachments || []});
    }
    supported.sort((a, b) => (Date.parse(a.created_at) || 0) - (Date.parse(b.created_at) || 0));
    let end = supported.length;
    const prefix = `ka-message-v1:${command.account_id}:${command.user_id}:${conversationId}:`;
    if (args.cursor != null) {
      if (typeof args.cursor !== "string" || !args.cursor.startsWith(prefix)) P.fail("cursor_expired");
      const before = args.cursor.slice(prefix.length);
      end = supported.findIndex(item => item.message_id === before);
      if (end < 0) P.fail("cursor_expired");
    }
    let start = end, pageBytes = 0;
    while (start > 0 && end - start < size) {
      const {_attachments, _event, ...candidate} = supported[start - 1];
      const placeholder = _event || {...candidate, images: _attachments.map((_, index) => ({remote_id: `${candidate.message_id}:${index}`, unavailable: true,
        _dom_attachment: true, mime_type: "image/jpeg", reference: "00000000-0000-0000-0000-000000000000"}))};
      const needed = new TextEncoder().encode(JSON.stringify(placeholder)).byteLength;
      if (needed > PAGE_BYTES) P.fail("message_too_large");
      if (pageBytes + needed > PAGE_BYTES) break;
      pageBytes += needed; start--;
    }
    const selection = supported.slice(start, end);
    let attachments = 0;
    const system_events = selection.filter(item => item._event).map(item => item._event);
    const messages = selection.filter(item => !item._event).map(item => {
      const {_attachments, ...message} = item;
      message.images = _attachments.map((value, index) => ++attachments <= 20 ? attach(value, message.message_id, index, command, auth) : {remote_id: `${message.message_id}:${index}`, unavailable: true});
      return message;
    });
    return {...target, messages, system_events, next_cursor: start ? prefix + selection[0].message_id : null, has_more: start > 0,
      coverage: "supported_messages", complete: start === 0 && unsupported === 0, unsupported_message_count: unsupported,
      account_user_id: command.user_id, _upstream_reached: true,
      _view: {role: raw.role, attachments_enabled: typeof raw.attachmentsEnabled === "boolean" ? raw.attachmentsEnabled : null,
        messages: viewMessages}};
  }
  // These public pages are read within the same account-serialised, globally
  // limited operation. Only fixed routes derived from verified conversation IDs
  // are fetched; page scripts, remote images and profile hyperlinks are not run.
  function profileURL(userId) { return ORIGIN + "/s-bestandsliste.html?userId=" + userId; }
  function linkedProfile(value) {
    let url;
    try { url = new URL(value, ORIGIN); } catch (_) { return null; }
    if (!P.originAllowed(url.href) || url.hash) return null;
    if (url.pathname === "/s-bestandsliste.html") {
      const values = url.searchParams.getAll("userId");
      return values.length === 1 && P.USER.test(values[0]) && [...url.searchParams.keys()].every(key => key === "userId") ? values[0] : null;
    }
    return !url.search ? url.pathname.match(/^\/s-anzeigen-des-nutzers\/([0-9]{1,20})\/?$/)?.[1] || null : null;
  }
  function publicProURL(value) {
    let url;
    try { url = new URL(value, ORIGIN); } catch (_) { return null; }
    return P.originAllowed(url.href) && !url.search && !url.hash && /^\/pro\/[A-Za-z0-9._~-]+\/?$/.test(url.pathname) ? url.href : null;
  }
  function verifiedProProfile(page, expectedURL, peer, diagnostic = null) {
    if (diagnostic) diagnostic.guard = "pro_url";
    if (!page || page.url !== expectedURL) return false;
    const canonical = [...page.doc.querySelectorAll('link[rel="canonical"]')];
    if (canonical.length !== 1 || publicProURL(canonical[0].getAttribute("href")) !== expectedURL) return false;
    if (diagnostic) diagnostic.guard = "pro_identity_components";
    const islands = [...page.doc.querySelectorAll("astro-island[component-url][props]")];
    if (islands.length > 100) return false;
    const proofs = new Map([["ProfileActions", []], ["BrandProfilePage", []]]); let profile = null;
    for (const island of islands) {
      let url;
      try { url = new URL(island.getAttribute("component-url"), ORIGIN); } catch (_) { continue; }
      if (!P.originAllowed(url.href) || url.search || url.hash) continue;
      const component = url.pathname.match(/^\/frontend-web\/_pro-public-web\/assets\/(ProfileActions|BrandProfilePage)\.[A-Za-z0-9_-]{1,100}\.js$/)?.[1];
      if (!component) continue;
      const body = island.getAttribute("props");
      if (island.getAttribute("component-export") !== "default" || typeof body !== "string" || body.length > 262144) return false;
      try {
        uniqueRootKeys(body);
        const props = JSON.parse(body), value = props[component === "ProfileActions" ? "userId" : "sellerId"];
        // The observed Astro primitive encoding is [0, value]. Never search
        // arbitrary nested IDs, ad rows, current-user data or free page text.
        if (!object(props) || !Array.isArray(value) || value.length !== 2 || value[0] !== 0 || user(value[1]) !== peer) return false;
        proofs.get(component).push(peer);
        if (component === "BrandProfilePage") profile = {url: expectedURL, brandName: props.brandName, about: props.about, contactPerson: props.contactPerson};
      } catch (_) { return false; }
    }
    const verified = [...proofs.values()].every(values => values.length === 1) ? profile : null;
    if (verified && diagnostic) diagnostic.guard = null;
    return verified;
  }
  function publicText(nodes, maximumBytes) {
    const excluded = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "FORM", "INPUT", "TEXTAREA", "SELECT", "BUTTON", "IFRAME", "NAV", "FOOTER"]);
    const parts = []; let count = 0, truncated = false;
    function visit(node) {
      if (++count > 2000) { truncated = true; return; }
      if (node.nodeType === 3) { parts.push(node.textContent || ""); return; }
      if (excluded.has(node.tagName)) return;
      for (const child of node.childNodes || []) { if (count > 2000) break; visit(child); }
      if (["DIV", "P", "SECTION", "H1", "H2", "H3", "LI", "BR"].includes(node.tagName)) parts.push("\n");
    }
    for (const node of nodes) { visit(node); parts.push("\n"); }
    const value = parts.join("").replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
      .replace(/[^\S\n]+/g, " ").replace(/ *\n */g, "\n").replace(/\n{3,}/g, "\n\n").trim();
    let result = value;
    // Account for JSON escapes and UTF-8, not merely UTF-16 character count.
    if (new TextEncoder().encode(JSON.stringify(result)).byteLength > maximumBytes) {
      let low = 0, high = result.length;
      while (low < high) { const mid = Math.ceil((low + high) / 2);
        if (new TextEncoder().encode(JSON.stringify(result.slice(0, mid))).byteLength <= maximumBytes) low = mid; else high = mid - 1; }
      result = result.slice(0, low).replace(/[\uD800-\uDBFF]$/, ""); truncated = true;
    }
    return {text: result, truncated};
  }
  function proProfileText(profile) {
    const parts = []; let unsupported = false, shortened = false;
    function decode(value) {
      if (value === undefined) return undefined;
      if (!Array.isArray(value) || value[0] !== 0 || value.length < 1 || value.length > 2) { unsupported = true; return undefined; }
      return value[1];
    }
    function append(label, value, html = false) {
      if (value == null || value === "") return;
      if (typeof value !== "string") { unsupported = true; return; }
      let nodes = [{nodeType: 3, textContent: value}];
      if (html && /<\/?[A-Za-z][^>]*>/.test(value)) {
        const template = document.createElement("template"); template.innerHTML = value;
        nodes = [...template.content.childNodes];
      }
      const content = publicText(nodes, 3000); shortened ||= content.truncated;
      if (content.text) parts.push(label + "\n" + content.text);
    }
    append("Name", decode(profile.brandName));
    const about = decode(profile.about);
    if (object(about)) {
      append("Über uns", decode(about.description), true);
      if (Object.keys(about).some(key => key !== "description")) unsupported = true;
    }
    else if (about != null) unsupported = true;
    // [0] means absent in the observed page. A direct textual primitive can
    // be retained, but unknown contact objects are not recursively unpacked.
    append("Kontaktperson", decode(profile.contactPerson));
    if (unsupported) parts.push("[Erfassungshinweis: Ein Teil der PRO-Profilinformationen hat eine noch nicht unterstützte Struktur.]");
    const text = publicText([{nodeType: 3, textContent: parts.join("\n\n")}], 3000);
    return {text: text.text, truncated: text.truncated || shortened || unsupported};
  }
  async function publicDocument(command, url, deadline) {
    checkCommand(command);
    const remaining = Math.min(6000, deadline - Date.now(), Date.parse(command.expires_at) - Date.now() - 1500);
    if (remaining <= 0 || typeof document === "undefined" || typeof document.createElement !== "function") return null;
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), remaining);
    try {
      const response = await fetch(url, {method: "GET", credentials: "omit", cache: "no-store", redirect: "follow",
        referrerPolicy: "no-referrer", headers: {Accept: "text/html"}, signal: controller.signal});
      if (response.status === 429) P.fail("upstream_rate_limited");
      if (response.status === 403) P.fail("upstream_blocked");
      if (!response.ok || !response.body || !P.originAllowed(response.url) ||
          !(response.headers.get("content-type") || "").toLowerCase().startsWith("text/html")) return null;
      const reader = response.body.getReader(), chunks = []; let total = 0;
      while (true) {
        if (Date.now() >= deadline || controller.signal.aborted) { await reader.cancel().catch(() => {}); return null; }
        P.assertFresh(command);
        const part = await reader.read(); if (part.done) break;
        total += part.value.byteLength;
        if (total > 2000000) { controller.abort(); await reader.cancel().catch(() => {}); return null; }
        chunks.push(part.value);
      }
      const bytes = new Uint8Array(total); let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      // DOMParser's otherwise inert document may still load img/iframe URLs.
      // Template contents have an inert owner document and are never attached
      // to the live page, so resource nodes remain non-fetching and non-running.
      const template = document.createElement("template");
      template.innerHTML = new TextDecoder("utf-8", {fatal: true}).decode(bytes);
      const doc = template.content;
      if (doc.querySelector('input[type="password"]')) return null;
      if (doc.querySelector('#challenge-form, #px-captcha, .cf-challenge, [data-captcha], iframe[src*="captcha"]')) P.fail("upstream_blocked");
      return {doc, url: response.url, body_bytes: total};
    } catch (error) {
      if (["upstream_rate_limited", "upstream_blocked", "command_expired"].includes(error?.code)) throw error;
      return null;
    } finally { clearTimeout(timer); }
  }
  async function providerContext(command, snapshot) {
    checkCommand(command);
    const peer = user(snapshot.recipient_user_id), listing = snapshot.listing_id;
    if (peer === command.user_id || snapshot.account_user_id !== command.user_id || !P.LISTING.test(listing || "") ||
        snapshot.conversation_id !== command.args.conversation_id || !["Buyer", "Seller"].includes(snapshot._view?.role)) P.fail("conversation_unverified");
    const captured_at = new Date().toISOString(), deadline = Math.min(Date.now() + 10000, Date.parse(command.expires_at) - 1500);
    let upstreamError = null;
    async function read(url) {
      if (upstreamError) return null;
      try { return await publicDocument(command, url, deadline); }
      catch (error) {
        if (!["upstream_blocked", "upstream_rate_limited"].includes(error?.code)) throw error;
        // Keep already read messages. The native adapter reports this fixed
        // signal to the shared circuit breaker before returning the archive.
        upstreamError = error.code; return null;
      }
    }
    const section = (kind, status, source_url = null, extra = {}) => ({kind, status, source_url, captured_at,
      user_id: peer, ...(kind.startsWith("listing_") ? {listing_id: listing} : {}), ...extra});
    const sections = []; let verifiedPro = null;
    if (snapshot._view.role === "Seller") {
      // The chat concerns our own ad. Its seller/imprint belongs to us, never
      // to the buyer whose public profile is archived below.
      sections.push(section("listing_provider", "not_counterpart"), section("listing_imprint", "not_counterpart"));
    } else {
      const page = await read(ORIGIN + "/zur-anzeige/" + listing.split("-")[0]);
      let status = "unavailable", provider = null, profileLinks = [], providerGuard = null;
      const expired = !!page?.doc.querySelector("#srchrslt-adexpired");
      if (page && !expired) {
        const final = new URL(page.url), ad = final.pathname.match(/^\/s-anzeige\/(?:[^/]+\/)?([0-9]{6,20})(?:-[0-9]+){0,2}\/?$/)?.[1];
        const roots = [...page.doc.querySelectorAll("#viewad-contact")];
        if (ad === listing.split("-")[0] && !final.search && !final.hash && roots.length === 1) {
          const links = [...roots[0].querySelectorAll("a[href]")], ids = new Set(); let invalidNumericLink = false;
          for (const link of links.slice(0, 101)) {
            const value = linkedProfile(link.getAttribute("href"));
            if (value) ids.add(value);
            if (value === peer) profileLinks.push(link);
            if (!value) {
              try { const url = new URL(link.getAttribute("href"), ORIGIN);
                if (url.pathname === "/s-bestandsliste.html" || url.pathname.startsWith("/s-anzeigen-des-nutzers/")) invalidNumericLink = true;
              } catch (_) { /* An unrelated malformed link proves no identity. */ }
            }
          }
          if (links.length <= 100 && !invalidNumericLink && ids.size === 1 && ids.has(peer)) { status = "captured"; provider = roots[0]; }
          else {
            status = "identity_mismatch";
            providerGuard = invalidNumericLink || ids.size > 0 ? "conflicting_numeric_id" : "no_numeric_or_business_link";
            // Pro storefront links use a different, observed seller header.
            // Only absence of numeric evidence permits this independent proof;
            // a conflicting numeric seller is never overridden by a vanity URL.
            const headers = [...page.doc.querySelectorAll("#viewad-profile-box #viewad-bizteaser .bizteaser--header")];
            if (links.length <= 100 && !invalidNumericLink && ids.size === 0 && headers.length === 1) {
              providerGuard = "pro_url";
              const anchors = [...headers[0].querySelectorAll("a[href]")];
              const urls = new Set(anchors.map(link => publicProURL(link.getAttribute("href"))).filter(Boolean));
              const invalidProLink = anchors.some(link => {
                try { const url = new URL(link.getAttribute("href"), ORIGIN); return url.pathname.startsWith("/pro/") && !publicProURL(url.href); }
                catch (_) { return false; }
              });
              if (anchors.length <= 10 && urls.size === 1 && !invalidProLink && !anchors.some(link => linkedProfile(link.getAttribute("href")))) {
                const proURL = [...urls][0], proPage = await read(proURL);
                if (!proPage) status = "unavailable";
                else {
                  const diagnostic = {};
                  verifiedPro = verifiedProProfile(proPage, proURL, peer, diagnostic);
                  providerGuard = diagnostic.guard;
                  if (verifiedPro) {
                    status = "captured"; provider = roots[0];
                    profileLinks = anchors.filter(link => publicProURL(link.getAttribute("href")) === proURL);
                  }
                }
              }
            }
          }
        } else {
          status = "identity_mismatch";
          providerGuard = ad !== listing.split("-")[0] || final.search || final.hash ? "listing_url" : "provider_root";
        }
      }
      if (status === "captured") {
        const nodes = [...provider.querySelectorAll(".userprofile-vip, .userprofile-vip-details-text, .userprofile-vip-badges, #viewad-contact-phone")];
        let info = publicText(nodes, 1500);
        // The known provider's own profile anchors remain identity-bound even
        // if a site variant omits the decorative name/detail class names.
        if (!info.text) info = publicText(profileLinks, 1500);
        const imprint = [...page.doc.querySelectorAll("#viewad-imprint #viewad-imprint-text")];
        const triggers = [...page.doc.querySelectorAll("#viewad-imprint-link")];
        const details = imprint.length === 1 ? publicText(imprint, 6000) : {text: "", truncated: false};
        const structure = {
          provider_nodes: Math.min(nodes.length, 100), profile_links: Math.min(profileLinks.length, 100),
          imprint_nodes: Math.min(imprint.length, 2), imprint_triggers: Math.min(triggers.length, 2),
          provider_text_chars: Math.min(info.text.length, 20000), imprint_text_chars: Math.min(details.text.length, 20000),
          body_bytes: Math.min(page.body_bytes, 2000000),
        };
        sections.push(section("listing_provider", info.text ? "captured" : "unavailable", page.url,
          {...info, ...(!info.text ? {structure} : {})}));
        // An empty/missing modal next to a real opener is an unsupported page
        // variant, never evidence that this provider has no imprint.
        const imprintStatus = details.text ? "captured" : !imprint.length && !triggers.length ? "not_present" : "unavailable";
        sections.push(section("listing_imprint", imprintStatus, page.url,
          {...details, ...(imprintStatus !== "captured" ? {structure} : {})}));
      } else {
        const details = expired ? {error_code: "page_unavailable"} : providerGuard ? {provider_guard: providerGuard} : {};
        sections.push(section("listing_provider", status, null, details), section("listing_imprint", status, null, details));
      }
    }
    const url = profileURL(peer), page = await read(url);
    if (!page) sections.push(section("profile", "unavailable", url));
    else {
      const roots = [...page.doc.querySelectorAll(".userprofile[data-followed-user]")];
      const bound = roots.length === 1 && roots[0].getAttribute("data-followed-user") === peer;
      const final = new URL(page.url), targetId = linkedProfile(final.href);
      // A /pro/ vanity URL is accepted only with the same explicit numeric
      // profile-root binding; arbitrary redirects and query strings are not.
      const target = targetId === peer || (!final.search && !final.hash && /^\/pro\/[A-Za-z0-9._~-]+\/?$/.test(final.pathname));
      if (!bound || !target) sections.push(section("profile", "identity_mismatch", url));
      else {
        const modal = [...page.doc.querySelectorAll("#userprofile-info .userprofile--info-content")];
        const info = publicText([...roots, ...(modal.length === 1 ? modal : [])], 3500);
        sections.push(section("profile", info.text ? "captured" : "not_present", page.url, info));
      }
    }
    if (verifiedPro) {
      const pro = proProfileText(verifiedPro), profileIndex = sections.findIndex(item => item.kind === "profile"), previous = sections[profileIndex];
      if (pro.text) {
        const parts = ["PRO-Profil – Quelle: " + verifiedPro.url + "\n" + pro.text];
        if (previous.status === "captured") parts.push("Öffentliches Nutzerprofil – Quelle: " + previous.source_url + "\n" + previous.text);
        else parts.push("[Erfassungshinweis: Das zusätzliche numerische Nutzerprofil konnte bei diesem Abruf nicht gesichert werden.]");
        const merged = publicText([{nodeType: 3, textContent: parts.join("\n\n")}], 3500);
        sections[profileIndex] = section("profile", "captured", verifiedPro.url, {
          text: merged.text, truncated: merged.truncated || pro.truncated || previous.truncated === true || previous.status !== "captured",
        });
      }
    }
    return {schema_version: 1, recipient_user_id: peer, listing_id: listing, sections,
      ...(upstreamError ? {upstream_error: upstreamError} : {})};
  }
  async function image(command, reference) {
    checkCommand(command); clearExpired();
    const entry = typeof reference === "string" ? references.get(reference) : null;
    if (!entry || entry.command_id !== command.id || entry.account_id !== command.account_id || entry.user_id !== command.user_id) P.fail("image_unavailable");
    return request(command, entry.url, {Accept: "image/png,image/jpeg,image/webp,image/gif", Authorization: entry.authorization}, IMAGE_MAX, true);
  }
  root.KleinanzeigenMessagingRead = Object.freeze({list, get, image, providerContext});
})(globalThis);
