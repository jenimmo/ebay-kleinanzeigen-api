/* Runs in Safari's isolated content world. Authentication never leaves Safari. */
(function () {
  "use strict";
  const CONTENT_REVISION = "1.0.23";
  const bootstrapState = globalThis.KleinanzeigenContentBootstrapState;
  if (bootstrapState) bootstrapState.stage = "content_started";
  const P = KleinanzeigenProtocol;
  const messagingRead = globalThis.KleinanzeigenMessagingRead;
  const ORIGIN = "https://www.kleinanzeigen.de";
  const MAX_TEXT = 20000;
  const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
  const mediaNodes = new Map();
  const conversationSnapshots = new Map();
  const documentId = crypto.randomUUID();
  if (bootstrapState) bootstrapState.stage = "initialized";
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const localDeadline = (command, duration) => Math.min(Date.parse(command.expires_at), Date.now() + duration);
  async function waitLocally(command, deadline, interval) {
    P.assertFresh(command);
    const remaining = deadline - Date.now();
    if (remaining <= 0) return false;
    await delay(Math.min(interval, remaining));
    P.assertFresh(command); inspectBlock();
    return Date.now() < deadline;
  }
  function visible(element) {
    return !!element && !element.hidden && (typeof element.checkVisibility === "function" ? element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : !!element.getClientRects().length);
  }
  function one(values, code = "ui_unsupported") {
    const list = [...values]; if (list.length !== 1) P.fail(code); return list[0];
  }
  function pageAllowed() { if (!P.originAllowed(location.href) || window !== window.top) P.fail("unsafe_tab"); }
  function inspectBlock(doc = document) {
    if (doc === document) {
      if (["/captcha", "/challenge", "/blocked", "/access-denied"].some(path => location.pathname === path || location.pathname.startsWith(path + "/"))) P.fail("upstream_blocked");
      const challengeSelectors = ['#challenge-form', '#px-captcha', '.cf-challenge', '[data-captcha]', 'iframe[src*="captcha"]', 'iframe[src*="challenge"]', 'form[action*="captcha"]'];
      if (challengeSelectors.some(selector => [...doc.querySelectorAll(selector)].some(visible))) P.fail("upstream_blocked");
    }
    const nodes = [...doc.querySelectorAll('h1, h2, [role="alert"]')];
    const expected = doc.querySelector('#messagebox-bundle, [data-testid="conversation-list"], #viewad-title, #viewad-description-text, #srchrslt-list, .ad-listitem');
    if (!expected && nodes.some(e => (doc !== document || visible(e)) && /ip[- ]bereich (?:wurde |ist )?(?:vorübergehend )?gesperrt|zugriff (?:vorübergehend gesperrt|verweigert)|sicherheitsüberprüfung|verify (?:that )?you are human|ich bin kein roboter|ungewöhnlicher datenverkehr|checking your browser/i.test(e.textContent || ""))) P.fail("upstream_blocked");
  }
  function identityFrom(doc, pagePath = location.pathname) {
    if (!/^\/m-(?:meine-anzeigen|einstellungen|mein-konto)\.html$/.test(pagePath) && pagePath !== "/portal/anzeigen") P.fail("identity_page_required");
    const ids = new Set();
    const add = value => { if (P.USER.test(String(value || ""))) ids.add(String(value)); };
    const markers = [...doc.querySelectorAll('[data-current-user-id], [data-logged-in-user-id], meta[name="current-user-id"], meta[name="user-id"]')];
    for (const e of markers)
      add(e.getAttribute("data-current-user-id") || e.getAttribute("data-logged-in-user-id") || e.getAttribute("content"));
    // Only the current-account header can contribute profile links. Advertised
    // sellers and conversation participants elsewhere on an own page cannot.
    const headerData = [...doc.querySelectorAll('#user-menu [data-user-id], #my-menu [data-user-id], [data-testid="my-profile"][data-user-id]')];
    const headerLinks = [...doc.querySelectorAll('#user-menu a[href], #my-menu a[href], a[data-testid="my-profile"]')];
    for (const e of headerData) add(e.getAttribute("data-user-id"));
    for (const e of headerLinks) {
      try {
        const url = new URL(e.getAttribute("href"), ORIGIN);
        if (!P.originAllowed(url.href)) continue;
        if (url.pathname === "/s-bestandsliste.html") add(url.searchParams.get("userId"));
        else add(url.pathname.match(/^\/s-anzeigen-des-nutzers\/([0-9]+)$/)?.[1]);
      } catch (_) { /* Invalid header links are not identity evidence. */ }
    }
    const next = doc.getElementById("__NEXT_DATA__");
    let nextData = null, nextValidJSON = false;
    try {
      // The isolated content world cannot read the site's window variables.
      nextData = JSON.parse(next?.textContent || "{}");
      nextValidJSON = !!next;
      add(nextData?.props?.pageProps?.currentUser?.id);
      add(nextData?.props?.pageProps?.currentUser?.userId);
    } catch (_) { /* Unknown JSON cannot establish an account. */ }
    const astro = doc.getElementById("astroSharedData");
    let data = null;
    let validJSON = false;
    try {
      data = JSON.parse(astro?.textContent || "{}");
      validJSON = !!astro;
      add(data?.user?.id); add(data?.user?.userId); add(data?.userProfile?.userId);
    } catch (_) { /* Unknown markup is rejected, never guessed. */ }
    if (ids.size !== 1) {
      if (doc.querySelector('input[type="password"]')) P.fail("reauth_required");
      const error = new Error("identity_unverified"); error.code = "identity_unverified";
      const allowedKeys = ["id", "userId", "user", "currentUser", "profile", "account", "email", "name"];
      const nextUser = nextData?.props?.pageProps?.currentUser;
      const nextType = nextUser === undefined ? "missing" : nextUser === null ? "null" : Array.isArray(nextUser) ? "array" : ["object", "string", "number"].includes(typeof nextUser) ? typeof nextUser : "missing";
      error.identity_structure = {
        page_path: ["/m-meine-anzeigen.html", "/portal/anzeigen"].includes(pagePath) ? pagePath : "/m-meine-anzeigen.html",
        astro_present: !!astro, astro_valid_json: validJSON, current_user_marker_count: Math.min(markers.length, 100),
        own_header_data_count: Math.min(headerData.length, 100), own_header_link_count: Math.min(headerLinks.length, 100),
        candidate_id_count: Math.min(ids.size, 2), next_present: !!next, next_valid_json: nextValidJSON,
        next_current_user: {type: nextType, keys: nextType === "object" ? Object.keys(nextUser).filter(key => ["id", "userId"].includes(key)) : []},
        json_scripts: [["astroSharedData", astro], ["__NEXT_DATA__", next]].map(([name, node]) => {
          const type = node?.getAttribute?.("type");
          return {name, type: !node ? "missing" : ["application/json", "application/ld+json"].includes(type) ? type : "other"};
        }),
        sources: ["user", "userProfile", "currentUser", "account", "profile"].map(name => {
          const value = data && typeof data === "object" ? data[name] : undefined;
          const type = value === undefined ? "missing" : value === null ? "null" : Array.isArray(value) ? "array" : ["object", "string", "number"].includes(typeof value) ? typeof value : "missing";
          return {name, type, keys: type === "object" ? Object.keys(value).filter(key => allowedKeys.includes(key)) : []};
        })
      };
      throw error;
    }
    return {user_id: [...ids][0]};
  }
  async function boundedText(response, maximum = 4000000, command = null) {
    if (!response.body) P.fail("ui_unsupported");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let bytes = 0;
    let text = "";
    while (true) {
      if (command) P.assertFresh(command);
      const chunk = await reader.read();
      if (chunk.done) break;
      bytes += chunk.value.byteLength;
      if (bytes > maximum) { await reader.cancel(); P.fail("ui_unsupported"); }
      text += decoder.decode(chunk.value, {stream: true});
    }
    return text + decoder.decode();
  }
  async function readIdentityPage(path, command, {listing = false} = {}) {
    const allowed = listing ? /^\/s-anzeige\/[^/]+\/[0-9]{6,20}(?:-[0-9]{1,10}-[0-9]{1,10})?\/?$/.test(path) : ["/m-meine-anzeigen.html", "/portal/anzeigen"].includes(path);
    if (!allowed || path.includes("?") || path.includes("#")) P.fail("invalid_command");
    P.assertFresh(command);
    let response;
    try {
      response = await fetch(ORIGIN + path, {credentials: "same-origin", cache: "no-store", redirect: "error",
        signal: AbortSignal.timeout(Math.min(15000, Math.max(1, Date.parse(command.expires_at) - Date.now())))});
    } catch (_) { P.assertFresh(command); P.fail(listing ? "identity_unverified" : "reauth_required"); }
    if (response.status === 429) P.fail("upstream_rate_limited");
    if (response.status === 403) P.fail("upstream_blocked");
    if (response.redirected || !response.ok || !P.originAllowed(response.url) || new URL(response.url).pathname !== path || new URL(response.url).search)
      P.fail(listing ? "identity_unverified" : "reauth_required");
    const body = await boundedText(response, 4000000, command);
    P.assertFresh(command);
    const doc = new DOMParser().parseFromString(body, "text/html");
    inspectBlock(doc);
    if (doc.querySelector('input[type="password"]')) P.fail("reauth_required");
    return doc;
  }
  function profileUserId(body) {
    let userId;
    try {
      const value = JSON.parse(body);
      if (!value || typeof value !== "object" || Array.isArray(value)) P.fail("identity_unverified");
      userId = value.userId;
      // No other profile field leaves this scope, including email and CSRF data.
    } catch (_) { P.fail("identity_unverified"); }
    if (typeof userId !== "string" || !P.USER.test(userId)) P.fail("identity_unverified");
    // JSON.parse would silently replace duplicate keys. Count actual top-level
    // property names, including escaped names, without exporting other values.
    let depth = 0, count = 0;
    for (let index = 0; index < body.length; index++) {
      const character = body[index];
      if (character === '"') {
        const start = index;
        while (++index < body.length) {
          if (body[index] === "\\") index++;
          else if (body[index] === '"') break;
        }
        let next = index + 1;
        while (/\s/.test(body[next] || "") && next < body.length) next++;
        if (depth === 1 && body[next] === ":" && JSON.parse(body.slice(start, index + 1)) === "userId") count++;
      } else if (character === "{" || character === "[") depth++;
      else if (character === "}" || character === "]") depth--;
    }
    if (count !== 1) P.fail("identity_unverified");
    return userId;
  }
  async function profileIdentity(command) {
    P.assertFresh(command);
    const endpoint = ORIGIN + "/m-mein-profil.json";
    let response;
    try {
      response = await fetch(endpoint, {credentials: "same-origin", cache: "no-store", redirect: "error",
        signal: AbortSignal.timeout(Math.min(15000, Math.max(1, Date.parse(command.expires_at) - Date.now())))});
    } catch (_) { P.assertFresh(command); P.fail("reauth_required"); }
    if (response.status === 429) P.fail("upstream_rate_limited");
    if (response.status === 403) P.fail("upstream_blocked");
    if (response.status === 401 || response.redirected || response.url !== endpoint) P.fail("reauth_required");
    if (!response.ok) P.fail("identity_unverified");
    if (!/^application\/json(?:\s*;|$)/i.test(response.headers?.get("content-type") || "")) P.fail("identity_unverified");
    let body;
    try { body = await boundedText(response, 65536, command); }
    catch (error) { if (error?.code === "command_expired") throw error; P.fail("identity_unverified"); }
    P.assertFresh(command);
    return {user_id: profileUserId(body), _upstream_reached: true};
  }
  async function identity(command) {
    pageAllowed(); inspectBlock(); P.assertFresh(command);
    if (command.method === "identify") {
      // Pair only from the user's own account page, never another user's profile.
      if (!/^\/m-(?:meine-anzeigen|einstellungen|mein-konto)\.html$/.test(location.pathname) && location.pathname !== "/portal/anzeigen") P.fail("identity_page_required");
      if (location.pathname === "/portal/anzeigen") return identityFrom(document);
      let direct, diagnostic;
      try { direct = identityFrom(document); }
      catch (error) {
        if (error?.code !== "identity_unverified" || error.identity_structure?.candidate_id_count !== 0) throw error;
        diagnostic = error.identity_structure;
      }
      let verified;
      try { verified = await profileIdentity(command); }
      catch (error) { if (diagnostic) error.identity_structure = diagnostic; throw error; }
      if (direct && direct.user_id !== verified.user_id) P.fail("account_mismatch");
      return verified;
    }
    // An old open chat is not proof of the currently authenticated account.
    // The command's shared limiter permit covers this bounded same-origin UI read.
    const identityPath = command.identity_page_path || "/m-meine-anzeigen.html";
    if (!["/m-meine-anzeigen.html", "/portal/anzeigen"].includes(identityPath)) P.fail("invalid_command");
    const verified = identityPath === "/m-meine-anzeigen.html" ? await profileIdentity(command) : identityFrom(await readIdentityPage(identityPath, command), identityPath);
    if (verified.user_id !== command.user_id) P.fail("account_mismatch");
    return verified;
  }
  function profileId(scope) {
    const ids = new Set();
    const add = value => { if (P.USER.test(value || "")) ids.add(value); };
    add(scope?.getAttribute("data-recipient-user-id"));
    for (const e of scope?.querySelectorAll('[data-recipient-user-id], [data-participant-id]') || []) add(e.getAttribute("data-recipient-user-id") || e.getAttribute("data-participant-id"));
    for (const a of scope?.querySelectorAll('a[href*="s-bestandsliste.html"], a[href*="s-anzeigen-des-nutzers/"]') || []) {
      const u = new URL(a.href, ORIGIN); if (u.origin !== ORIGIN) continue;
      add(u.searchParams.get("userId") || u.pathname.match(/\/s-anzeigen-des-nutzers\/([0-9]+)$/)?.[1]);
    }
    return ids.size === 1 ? [...ids][0] : null;
  }
  function conversationId(node) {
    const id = node.getAttribute("data-conversation-id") || node.getAttribute("data-active-conversation-id");
    if (P.ID.test(id || "")) return id;
    const a = node.matches('a[href]') ? node : node.querySelector('a[href*="conversationId="]');
    if (!a) return null;
    const url = new URL(a.href, ORIGIN);
    return url.origin === ORIGIN && ["/m-nachrichten.html", "/portal/nachrichten"].includes(url.pathname) && P.ID.test(url.searchParams.get("conversationId") || "") ? url.searchParams.get("conversationId") : null;
  }
  function businessInboxEmpty() {
    if (location.pathname !== "/portal/nachrichten") return false;
    const main = document.querySelector("main");
    if (!visible(main) || ![...main.querySelectorAll("h1")].some(node => visible(node) && node.textContent?.trim() === "Meine Nachrichten")) return false;
    // Observed business empty state. Missing IDs alone never mean an empty inbox.
    if ([...main.querySelectorAll('#conversation-list, [data-testid="conversation-list"], [data-testid="conversation-wrapper"], [data-conversation-id], a[href*="conversationId="]')].some(visible)) return false;
    // The observed portal splits words across elements. innerText supplies the
    // rendered word boundaries; textContent can concatenate those words.
    const text = (main.innerText || "").replace(/\s+/g, " ");
    return text.includes("Hier geht bald die Post ab") &&
      text.includes("Jetzt inserieren und Anfragen erhalten oder nach spannenden Anzeigen suchen und andere Nutzer kontaktieren.");
  }
  function inbox() {
    const root = document.querySelector('#conversation-list, [data-testid="conversation-list"]');
    if (!root) {
      if (businessInboxEmpty()) return {conversations: [], more: false};
      P.fail("ui_unsupported");
    }
    const result = new Map();
    for (const node of root.querySelectorAll('[data-conversation-id], a[href*="conversationId="]')) {
      const id = conversationId(node); if (!id) continue;
      const row = node.closest("[data-conversation-id], li, article") || node;
      result.set(id, {conversation_id: id, recipient_user_id: profileId(row),
        listing_id: row.getAttribute("data-listing-id") || row.getAttribute("data-ad-id") || null,
        title: row.querySelector('[data-testid="conversation-title"], h2, h3, .subject')?.textContent?.trim() || null,
        unread: row.getAttribute("data-unread") === "true" || row.classList.contains("unread")});
    }
    const explicitEmpty = root.querySelector('[data-testid="empty-inbox"], [data-empty-inbox="true"]');
    if (!result.size && !explicitEmpty) P.fail("ui_unsupported");
    return {conversations: [...result.values()], more: !!document.querySelector('[data-has-more="true"], [data-virtualized="true"], button[data-testid="load-more-conversations"]')};
  }
  async function readyInbox(command) {
    // Client-side portal hydration can finish after document_end. This waits
    // only on the current DOM; it never polls a website or invents an empty list.
    const deadline = localDeadline(command, 10000);
    for (let attempt = 0; attempt < 50; attempt++) {
      P.assertFresh(command); inspectBlock();
      let snapshot;
      try { snapshot = inbox(); }
      catch (error) { if (error?.code !== "ui_unsupported") throw error; }
      if (snapshot) {
        if (attempt) {
          // Recheck the account once after waiting, then read the current view.
          await identity(command);
          P.assertFresh(command);
          return inbox();
        }
        return snapshot;
      }
      if (!await waitLocally(command, deadline, 200)) break;
    }
    P.fail("ui_unsupported");
  }
  function page(items, args, key) {
    const limit = Number.isInteger(args.limit) ? args.limit : 50;
    if (limit < 1 || limit > 200) P.fail("invalid_limit");
    let start = 0;
    if (args.cursor) {
      if (typeof args.cursor !== "string" || !args.cursor.startsWith("after:")) P.fail("cursor_expired");
      const positions = items.map((item, i) => item[key] === args.cursor.slice(6) ? i : -1).filter(i => i >= 0);
      if (positions.length !== 1) P.fail("cursor_expired");
      start = positions[0] + 1;
    }
    const selected = [];
    let size = 0;
    for (const item of items.slice(start, start + limit)) {
      const bytes = new TextEncoder().encode(JSON.stringify(item)).length;
      if (bytes > 45000) P.fail("message_too_large");
      if (size + bytes > 45000) break;
      selected.push(item); size += bytes;
    }
    const more = start + selected.length < items.length;
    return {items: selected, next_cursor: more && selected.length ? "after:" + selected.at(-1)[key] : null, has_more: more};
  }
  async function openConversation(id, command) {
    if (!P.ID.test(id || "")) P.fail("invalid_target");
    if (messagingRead) {
      const snapshot = await messagingRead.get(command, {conversation_id: id, limit: 200, cursor: null});
      await readyConversationView(snapshot, command);
      conversationSnapshots.set(command.id, snapshot);
      return snapshot;
    }
    let snapshot;
    try { snapshot = conversation(command.user_id); } catch (_) { snapshot = null; }
    if (snapshot?.conversation_id === id) return snapshot;
    const root = document.querySelector('#conversation-list, [data-testid="conversation-list"]');
    if (!root) P.fail("ui_unsupported");
    const candidates = [...root.querySelectorAll('[data-conversation-id], a[href*="conversationId="]')].filter(e => conversationId(e) === id);
    // A nested anchor and its row refer to one target; click the unique outer row.
    const outer = candidates.filter(e => !candidates.some(parent => parent !== e && parent.contains(e)));
    const selected = one(outer, "conversation_unverified");
    P.assertFresh(command); selected.click();
    const deadline = localDeadline(command, 4000);
    for (let attempt = 0; attempt < 40; attempt++) {
      P.assertFresh(command); inspectBlock();
      try { snapshot = conversation(command.user_id); if (snapshot.conversation_id === id) return snapshot; } catch (_) { /* Wait for the clicked chat only. */ }
      if (!await waitLocally(command, deadline, 100)) break;
    }
    P.fail("conversation_unverified");
  }
  function conversationView(snapshot, command) {
    const url = new URL(location.href);
    if (!["/m-nachrichten.html", "/portal/nachrichten"].includes(url.pathname) ||
        url.searchParams.getAll("conversationId").length !== 1 || url.searchParams.get("conversationId") !== snapshot.conversation_id ||
        snapshot.account_user_id !== command.user_id || !P.USER.test(snapshot.recipient_user_id || "") || snapshot.recipient_user_id === command.user_id)
      P.fail("conversation_unverified");
    const scope = one([...document.querySelectorAll('[data-testid="conversation-wrapper"]')].filter(visible), "conversation_unverified");
    const header = one([...scope.querySelectorAll("header")].filter(node => visible(node) && !node.closest("li[data-testid]")), "conversation_unverified");
    const listings = new Set();
    for (const link of header.querySelectorAll("a[href]")) {
      const href = new URL(link.getAttribute("href"), ORIGIN);
      if (href.origin !== ORIGIN || href.search || href.hash) continue;
      const id = href.pathname.match(/^\/zur-anzeige\/([0-9]{6,20})$/)?.[1];
      if (id) listings.add(id);
    }
    if (listings.size !== 1 || !listings.has(String(snapshot.listing_id || "").split("-")[0])) P.fail("listing_mismatch");
    const serverMessages = new Map((snapshot._view?.messages || snapshot.messages).map(message => [message.message_id, message]));
    const nodes = [...scope.querySelectorAll("li[data-testid]")].filter(node => visible(node) && UUID.test(node.getAttribute("data-testid") || ""));
    if (!nodes.length) P.fail("conversation_unverified");
    const seen = new Set();
    let provenMessages = 0;
    for (const node of nodes) {
      const id = node.getAttribute("data-testid"), message = serverMessages.get(id);
      if (!message || seen.has(id)) P.fail("conversation_unverified");
      seen.add(id);
      if (message.type === "unsupported") continue;
      if (message.type !== "message") P.fail("conversation_unverified");
      const inbound = !!node.querySelector('[data-testid="INBOUND"]'), outbound = !!node.querySelector('[data-testid="OUTBOUND"]');
      if (inbound === outbound || (outbound ? "outbound" : "inbound") !== message.direction) P.fail("conversation_unverified");
      provenMessages++;
    }
    if (!provenMessages) P.fail("conversation_unverified");
    return scope;
  }
  async function readyConversationView(snapshot, command) {
    const deadline = localDeadline(command, 10000);
    for (let attempt = 0; attempt < 50; attempt++) {
      P.assertFresh(command); inspectBlock();
      try { return conversationView(snapshot, command); }
      catch (error) { if (!["conversation_unverified", "listing_mismatch"].includes(error?.code)) throw error; }
      if (!await waitLocally(command, deadline, 200)) break;
    }
    P.fail("conversation_unverified");
  }
  const cappedImageCount = values => Math.min(Array.isArray(values) ? values.length : 0, 2);
  function imageMimeKind(value) {
    if (value == null || value === "") return "missing";
    return ({"image/jpeg": "jpeg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"})[value] || "other";
  }
  function imageFailure(stage, details = {}, code = "image_unavailable") {
    const error = new Error(code); error.code = code;
    error.image_structure = {stage, ...details};
    return error;
  }
  function failImage(stage, details = {}, code = "image_unavailable") {
    throw imageFailure(stage, details, code);
  }
  async function boundedImageTask(operation, command, stage, dispose = null, overallDeadline = null) {
    P.assertFresh(command);
    const deadline = Math.min(localDeadline(command, 5000), overallDeadline ?? Infinity);
    if (Date.now() >= deadline) failImage(stage);
    let accepting = true, timer;
    const work = Promise.resolve().then(() => {
      P.assertFresh(command);
      if (Date.now() >= deadline) failImage(stage);
      return operation();
    }).then(value => {
      if (!accepting || Date.now() >= deadline) {
        if (dispose) dispose(value);
        P.assertFresh(command); failImage(stage);
      }
      return value;
    });
    const expired = new Promise((_, reject) => {
      timer = setTimeout(() => reject(imageFailure(stage, {}, Date.now() >= Date.parse(command.expires_at) ? "command_expired" : "image_unavailable")), Math.max(1, deadline - Date.now()));
    });
    try {
      const value = await Promise.race([work, expired]);
      P.assertFresh(command); inspectBlock();
      return value;
    } finally { accepting = false; clearTimeout(timer); }
  }
  async function imagePixelProof(image, command) {
    P.assertFresh(command);
    const deadline = localDeadline(command, 5000);
    const width = image.width, height = image.height;
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 ||
        width > P.MAX_PROOF_PIXELS || height > P.MAX_PROOF_PIXELS || width * height > P.MAX_PROOF_PIXELS)
      failImage("pixel_decode", {within_pixel_limit: false});
    if (typeof createImageBitmap !== "function") failImage("pixel_decode");
    let bitmap, canvas;
    try {
      // Both preview and received bytes use this identical path. No resize or
      // encoding step participates. The hash covers every RGBA8 sample in row
      // order; dimensions and the versioned sRGB algorithm are bound separately.
      bitmap = await boundedImageTask(() => createImageBitmap(new Blob([image.bytes], {type: image.mime_type}),
        {imageOrientation: "from-image", premultiplyAlpha: "none", colorSpaceConversion: "default"}), command, "pixel_decode", value => value?.close(), deadline);
      if (bitmap.width !== width || bitmap.height !== height) failImage("pixel_decode", {pixel_dimensions_match: false});
      canvas = document.createElement("canvas"); canvas.width = width; canvas.height = height;
      if (canvas.width !== width || canvas.height !== height) failImage("pixel_decode", {pixel_dimensions_match: false});
      const context = canvas.getContext("2d", {colorSpace: "srgb", willReadFrequently: true});
      if (!context || typeof context.getContextAttributes !== "function" || context.getContextAttributes().colorSpace !== "srgb")
        failImage("pixel_decode", {pixel_space_supported: false});
      P.assertFresh(command);
      context.drawImage(bitmap, 0, 0);
      const pixels = context.getImageData(0, 0, width, height, {colorSpace: "srgb", pixelFormat: "rgba-unorm8"});
      if (pixels.colorSpace !== "srgb") failImage("pixel_decode", {pixel_space_supported: false});
      if (!(pixels.data instanceof Uint8ClampedArray) || pixels.width !== width || pixels.height !== height || pixels.data.byteLength !== width * height * 4)
        failImage("pixel_decode", {pixel_format_supported: false});
      P.assertFresh(command);
      const sha256 = await boundedImageTask(() => hashBytes(new Uint8Array(pixels.data.buffer, pixels.data.byteOffset, pixels.data.byteLength)), command, "pixel_decode", null, deadline);
      return {algorithm: "srgb-rgba8-v1", width, height, sha256};
    } catch (error) {
      if (error?.image_structure || ["command_expired", "upstream_blocked", "upstream_rate_limited"].includes(error?.code)) throw error;
      failImage("pixel_decode");
    } finally {
      bitmap?.close();
      if (canvas) { canvas.width = 0; canvas.height = 0; }
    }
  }
  function freshChatDocument(command) {
    if (!command.fresh_navigation || command.fresh_navigation.document_id !== documentId ||
        command.fresh_navigation.path !== location.pathname ||
        !["/m-nachrichten.html", "/portal/nachrichten"].includes(location.pathname)) failImage("fresh_document", {fresh_document: false});
  }
  function imageDataBytes(image, expectedMime) {
    const ready = {visible: visible(image), complete: image.complete === true,
      dimensions_valid: Number.isFinite(image.naturalWidth) && Number.isFinite(image.naturalHeight) && image.naturalWidth > 0 && image.naturalHeight > 0,
      within_pixel_limit: Number.isFinite(image.naturalWidth) && Number.isFinite(image.naturalHeight) && image.naturalWidth * image.naturalHeight <= 40000000};
    if (!ready.visible || !ready.complete || !ready.dimensions_valid || !ready.within_pixel_limit) failImage("image_ready", ready);
    const source = image.currentSrc || image.src || "", separator = source.indexOf(",");
    const sourceKind = !source ? "none" : source.startsWith("data:") ? "data" : source.startsWith("blob:") ? "blob" : source.startsWith("https:") ? "https" : "other";
    const declaredMime = source.slice(0, separator).match(/^data:([^;,]+)/)?.[1];
    const encoding = {source_kind: sourceKind, data_mime: imageMimeKind(declaredMime), expected_mime: imageMimeKind(expectedMime)};
    const mime = source.slice(0, separator).match(/^data:(image\/(?:jpeg|png|gif|webp));base64$/)?.[1];
    if (!mime) failImage("source_encoding", encoding);
    if (expectedMime && mime !== expectedMime) failImage("mime_match", encoding);
    let bytes;
    try { bytes = decodeBase64(source.slice(separator + 1), MAX_IMAGE_BYTES); }
    catch (_) { failImage("base64", encoding); }
    const magic = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255 ? "jpeg" :
      [137, 80, 78, 71, 13, 10, 26, 10].every((value, index) => bytes[index] === value) ? "png" :
      /^GIF8[79]a$/.test(String.fromCharCode(...bytes.subarray(0, 6))) ? "gif" :
      String.fromCharCode(...bytes.subarray(0, 4)) === "RIFF" && String.fromCharCode(...bytes.subarray(8, 12)) === "WEBP" ? "webp" : "unknown";
    if (magic !== imageMimeKind(mime)) failImage("magic_bytes", {...encoding, magic_type: magic});
    return {bytes, mime_type: mime, width: image.naturalWidth, height: image.naturalHeight};
  }
  function boundImageMessage(snapshot, message, command) {
    P.assertFresh(command); freshChatDocument(command);
    const descriptor = {attachment_count: cappedImageCount(message.images), remote_id_matches: message.images?.[0]?.remote_id === message.message_id + ":0",
      descriptor_present: message.images?.[0]?._dom_attachment === true,
      sender_matches: ["inbound", "outbound"].includes(message.direction) && message.sender_user_id === (message.direction === "outbound" ? command.user_id : snapshot.recipient_user_id),
      direction_matches: ["inbound", "outbound"].includes(message.direction), expected_mime: imageMimeKind(message.images?.[0]?.mime_type)};
    if (message.images?.length !== 1 || !descriptor.remote_id_matches || !descriptor.descriptor_present ||
        (message.images[0].mime_type != null && !["image/jpeg", "image/png", "image/gif", "image/webp"].includes(message.images[0].mime_type)) ||
        !descriptor.direction_matches || !descriptor.sender_matches) failImage("attachment_descriptor", descriptor);
    let scope;
    try { scope = conversationView(snapshot, command); }
    catch (error) {
      if (!["conversation_unverified", "listing_mismatch"].includes(error?.code)) throw error;
      failImage("message_binding", {view_verified: false});
    }
    const nodes = [...scope.querySelectorAll("li[data-testid]")].filter(node => visible(node) && node.getAttribute("data-testid") === message.message_id);
    if (nodes.length !== 1) failImage("message_binding", {view_verified: true, message_count: cappedImageCount(nodes)});
    const node = nodes[0];
    const expectedDirection = message.direction === "outbound" ? "OUTBOUND" : "INBOUND";
    if (!node.querySelector('[data-testid="' + expectedDirection + '"]')) failImage("message_binding", {view_verified: true, message_count: 1, direction_matches: false});
    return node;
  }
  function boundDOMImage(snapshot, message, command) {
    const node = boundImageMessage(snapshot, message, command);
    const containers = [...node.querySelectorAll('[data-testid="message-attachment-container"]')];
    if (containers.length !== 1) failImage("container", {container_count: cappedImageCount(containers)});
    const container = containers[0];
    if (!visible(container)) failImage("container", {container_count: 1, visible: false});
    const images = [...container.querySelectorAll('img[data-testid="attachment-image"]')];
    if (images.length !== 1) failImage("image_node", {image_count: cappedImageCount(images)});
    const image = images[0];
    return imageDataBytes(image, message.images[0].mime_type);
  }
  function existingImageModals() {
    return [...document.querySelectorAll('[data-testid="attachment-modal"], .Modal--Backdrop, [data-testid="modal-backdrop-div"], [role="dialog"], [aria-modal="true"]')];
  }
  async function readBoundImageViewer(snapshot, message, command, node, deadline) {
    if (command.method !== "get_conversation" || message.direction !== "inbound" || node.isConnected !== true || existingImageModals().length)
      failImage("viewer_binding", {view_verified: false});
    const overlays = [...node.querySelectorAll(".ImgFraudProtection")].filter(visible);
    if (overlays.length !== 1) failImage("viewer_binding", {container_count: cappedImageCount(overlays)});
    const buttons = [...overlays[0].querySelectorAll("button")].filter(button => visible(button) &&
      (button.getAttribute("aria-label") || button.textContent || "").trim() === "Bild anzeigen");
    if (buttons.length !== 1 || buttons[0].disabled || buttons[0].getAttribute("aria-disabled") === "true" || buttons[0].isConnected !== true)
      failImage("viewer_binding", {view_verified: false});
    P.assertFresh(command); inspectBlock();
    if (boundImageMessage(snapshot, message, command) !== node || existingImageModals().length) failImage("viewer_binding", {view_verified: false});
    // The public component's only action here opens this message's attachment
    // viewer. This is one read-only click, never a send or an automatic retry.
    buttons[0].click();
    let modal = null, backdrop = null;
    const stillBound = () => {
      if (node.isConnected !== true || boundImageMessage(snapshot, message, command) !== node ||
          modal?.isConnected !== true || backdrop?.isConnected !== true || modal.closest(".Modal--Backdrop") !== backdrop ||
          existingImageModals().some(item => item !== modal && item !== backdrop) ||
          document.querySelectorAll('[data-testid="attachment-modal"]').length !== 1 ||
          document.querySelectorAll('[data-testid="attachment-modal"]')[0] !== modal) failImage("viewer_binding", {view_verified: false});
    };
    for (let attempt = 0; attempt < 40; attempt++) {
      P.assertFresh(command); inspectBlock(); freshChatDocument(command);
      if (node.isConnected !== true || boundImageMessage(snapshot, message, command) !== node) failImage("viewer_binding", {view_verified: false});
      const current = [...document.querySelectorAll('[data-testid="attachment-modal"]')];
      if (current.length > 1 || (modal && current[0] !== modal)) failImage("viewer_binding", {container_count: cappedImageCount(current)});
      if (current.length === 1) {
        if (!modal) { modal = current[0]; backdrop = modal.closest(".Modal--Backdrop"); }
        stillBound();
        const centers = [...modal.querySelectorAll(".Modal--Center--Image")];
        const images = [...modal.querySelectorAll('img[data-testid="attachment-image"]')];
        if (centers.length !== 1 || images.length > 1 || modal.querySelectorAll('[data-testid="left-arrow-button-container"], [data-testid="right-arrow-button-container"]').length)
          failImage("viewer_binding", {image_count: cappedImageCount(images)});
        if (images.length === 1) {
          if (!centers[0].contains(images[0]) || modal.querySelectorAll("img").length !== 1) failImage("viewer_binding", {image_count: 2});
          const source = images[0].currentSrc || images[0].src;
          let image;
          try { image = imageDataBytes(images[0], message.images[0].mime_type); }
          catch (error) { if (error?.image_structure?.stage !== "image_ready") throw error; }
          if (image) {
            await identity(command);
            P.assertFresh(command); inspectBlock();
            stillBound();
            if (images[0].isConnected !== true || (images[0].currentSrc || images[0].src) !== source || modal.querySelectorAll('img[data-testid="attachment-image"]')[0] !== images[0])
              failImage("viewer_binding", {view_verified: false});
            const close = [...backdrop.querySelectorAll("button")].filter(button => visible(button) &&
              (button.getAttribute("aria-label") || button.textContent || "").trim() === "Schließen");
            if (close.length !== 1 || close[0].disabled || close[0].isConnected !== true) failImage("viewer_binding", {view_verified: false});
            P.assertFresh(command); stillBound();
            close[0].click(); // Close only the exact viewer opened above.
            return image;
          }
        }
      } else if (existingImageModals().length) failImage("viewer_binding", {view_verified: false});
      if (!await waitLocally(command, deadline, 125)) break;
    }
    failImage("viewer_ready", {readiness_timeout: true});
  }
  async function readyDOMImage(snapshot, message, command, deadline = Date.now() + 5000) {
    deadline = Math.min(deadline, Date.parse(command.expires_at));
    let lastError;
    for (let attempt = 0; attempt < 40; attempt++) {
      P.assertFresh(command); inspectBlock(); freshChatDocument(command);
      try { return boundDOMImage(snapshot, message, command); }
      catch (error) {
        if (!["image_unavailable", "conversation_unverified", "listing_mismatch"].includes(error?.code)) throw error;
        lastError = error;
        if (command.method === "get_conversation" && message.direction === "inbound" &&
            error?.image_structure?.stage === "container" && error.image_structure.container_count === 0) {
          const node = boundImageMessage(snapshot, message, command);
          if ([...node.querySelectorAll(".ImgFraudProtection")].some(visible)) return readBoundImageViewer(snapshot, message, command, node, deadline);
        }
      }
      if (!await waitLocally(command, deadline, 125)) break;
    }
    if (lastError?.image_structure) {
      lastError.image_structure = {...lastError.image_structure, readiness_timeout: true};
      throw lastError;
    }
    failImage("image_ready", {readiness_timeout: true});
  }
  function boundArchiveImageGroup(snapshot, message, command) {
    // Deliberately separate from the one-image send/receipt guard above. A group
    // is allowed only while reading an explicitly selected conversation.
    if (command.method !== "get_conversation" || !Array.isArray(message.images) ||
        message.images.length < 2 || message.images.length > 20 ||
        message.images.some((image, index) => image?.remote_id !== `${message.message_id}:${index}` ||
          image._dom_attachment !== true || (image.mime_type != null && !["image/jpeg", "image/png", "image/gif", "image/webp"].includes(image.mime_type))))
      failImage("attachment_descriptor", {attachment_count: cappedImageCount(message.images)});
    return boundImageMessage(snapshot, {...message, images: [message.images[0]]}, command);
  }
  function archiveGroupOpener(node, message) {
    const overlays = [...node.querySelectorAll(".ImgFraudProtection")].filter(visible);
    const containers = [...node.querySelectorAll('[data-testid="message-attachment-container"]')];
    let button, firstSource = null;
    if (overlays.length === 1 && containers.length === 0 && message.direction === "inbound") {
      const buttons = [...overlays[0].querySelectorAll("button")].filter(item => visible(item) &&
        (item.getAttribute("aria-label") || item.textContent || "").trim() === "Bild anzeigen");
      if (buttons.length !== 1) failImage("viewer_binding", {view_verified: false, viewer_guard: "opener_control"});
      button = buttons[0];
    } else if (!overlays.length && containers.length === 1 && visible(containers[0])) {
      // Observed MessageAttachments maps attachments.slice(0,4) to these exact
      // buttons; button zero opens gallery index zero. It does not send anything.
      const buttons = [...containers[0].querySelectorAll("button.MessageAttachment")];
      if (buttons.length !== Math.min(4, message.images.length) ||
          buttons.some(item => !visible(item) || item.querySelectorAll('img[data-testid="attachment-image"]').length !== 1))
        failImage("viewer_binding", {image_count: cappedImageCount(buttons), viewer_guard: "opener_control"});
      button = buttons[0];
      const image = button.querySelectorAll('img[data-testid="attachment-image"]')[0];
      imageDataBytes(image, message.images[0].mime_type);
      firstSource = image.currentSrc || image.src;
    } else failImage("viewer_binding", {container_count: cappedImageCount(containers), viewer_guard: "opener_control"});
    if (!button || button.isConnected !== true || button.disabled || button.getAttribute("aria-disabled") === "true" ||
        button.closest("form")) failImage("viewer_binding", {view_verified: false, viewer_guard: "opener_control"});
    return {button, firstSource};
  }
  function activeArchiveThumbnail(node) {
    // The observed component marks only its active thumbnail with this inline
    // shorthand. WebKit leaves its longhands empty when the color uses var().
    // Require both that authored marker and all four resolved border sides;
    // an unrelated stylesheet alone must not establish the active selection.
    const border = node?.style?.getPropertyValue("border")?.trim() || "";
    if (!/^2px\s+solid\s+\S/.test(border) || node.style.boxSizing !== "border-box" || typeof getComputedStyle !== "function")
      return false;
    try {
      const computed = getComputedStyle(node);
      return computed.boxSizing === "border-box" && ["Top", "Right", "Bottom", "Left"].every(side =>
        computed[`border${side}Width`] === "2px" && computed[`border${side}Style`] === "solid");
    } catch (_) { return false; }
  }
  async function readArchiveImageGroup(snapshot, message, command, deadline) {
    const assertDeadline = () => {
      P.assertFresh(command);
      if (Date.now() >= deadline) failImage("viewer_ready", {readiness_timeout: true});
    };
    let node = null, opener = null, bindingError = null;
    // The server read can finish before React mounts the matching chat. Wait
    // only on the current DOM, within the same gallery budget, before one click.
    for (let attempt = 0; attempt < 80; attempt++) {
      assertDeadline(); inspectBlock(); freshChatDocument(command);
      if (existingImageModals().length) failImage("viewer_binding", {view_verified: false, viewer_guard: "existing_modal"});
      try {
        const candidate = boundArchiveImageGroup(snapshot, message, command);
        if (candidate.isConnected !== true) failImage("viewer_binding", {view_verified: false, viewer_guard: "message_changed"});
        const readyOpener = archiveGroupOpener(candidate, message);
        node = candidate; opener = readyOpener; break;
      } catch (error) {
        if (error?.code !== "image_unavailable" ||
            !["message_binding", "viewer_binding", "image_ready"].includes(error?.image_structure?.stage)) throw error;
        bindingError = error;
      }
      if (!await waitLocally(command, deadline, 125)) break;
    }
    if (!opener) {
      if (bindingError?.image_structure) {
        bindingError.image_structure = {...bindingError.image_structure, readiness_timeout: true};
        throw bindingError;
      }
      failImage("viewer_ready", {readiness_timeout: true});
    }
    assertDeadline(); inspectBlock();
    if (boundArchiveImageGroup(snapshot, message, command) !== node || existingImageModals().length)
      failImage("viewer_binding", {view_verified: false, viewer_guard: "preopen_binding"});
    opener.button.click();
    let modal = null, backdrop = null, thumbnails = null, imageNodes = null, sources = null;
    const stillBound = (afterIdentity = false) => {
      P.assertFresh(command); freshChatDocument(command);
      const failBinding = viewerGuard => failImage("viewer_binding", {view_verified: false,
        viewer_guard: afterIdentity ? "post_identity_binding" : viewerGuard});
      if (node.isConnected !== true || boundArchiveImageGroup(snapshot, message, command) !== node)
        failBinding("message_changed");
      if (modal?.isConnected !== true || backdrop?.isConnected !== true || modal.closest(".Modal--Backdrop") !== backdrop)
        failBinding("modal_ownership");
      if (existingImageModals().some(item => item !== modal && item !== backdrop)) failBinding("existing_modal");
      if (document.querySelectorAll('[data-testid="attachment-modal"]').length !== 1 ||
          document.querySelectorAll('[data-testid="attachment-modal"]')[0] !== modal)
        failBinding("modal_changed");
    };
    for (let attempt = 0; attempt < 80; attempt++) {
      assertDeadline(); inspectBlock(); freshChatDocument(command);
      if (node.isConnected !== true || boundArchiveImageGroup(snapshot, message, command) !== node)
        failImage("viewer_binding", {view_verified: false, viewer_guard: "message_changed"});
      const current = [...document.querySelectorAll('[data-testid="attachment-modal"]')];
      if (current.length > 1 || (modal && current[0] !== modal))
        failImage("viewer_binding", {container_count: cappedImageCount(current), viewer_guard: "modal_changed"});
      if (current.length === 1) {
        if (!modal) { modal = current[0]; backdrop = modal.closest(".Modal--Backdrop"); }
        stillBound();
        const centers = [...modal.querySelectorAll(".Modal--Center--Image")];
        const main = [...modal.querySelectorAll('img[data-testid="attachment-image"]')];
        const strips = [...backdrop.querySelectorAll(".Modal--Thumbnails")];
        if (centers.length !== 1 || main.length > 1 || strips.length !== 1 ||
            modal.querySelectorAll('[data-testid="left-arrow-button-container"]').length !== 1 ||
            modal.querySelectorAll('[data-testid="right-arrow-button-container"]').length !== 1)
          failImage("viewer_binding", {image_count: cappedImageCount(main), viewer_guard: "modal_structure"});
        // The observed gallery maps the complete DTO attachments array directly
        // to Thumbnail components in order, each using the same original-byte
        // AttachmentContent loader as the main image (CSS scales display only).
        const thumbs = [...strips[0].querySelectorAll(".Attachment--View--Thumbnail")];
        if (thumbs.length !== message.images.length)
          failImage("viewer_binding", {image_count: cappedImageCount(thumbs), viewer_guard: "thumbnail_count"});
        if (thumbs.some(item => !visible(item) || item.isConnected !== true))
          failImage("viewer_binding", {image_count: cappedImageCount(thumbs), viewer_guard: "thumbnail_visibility"});
        if (thumbnails && thumbs.some((item, index) => item !== thumbnails[index]))
          failImage("viewer_binding", {image_count: cappedImageCount(thumbs), viewer_guard: "thumbnail_order"});
        thumbnails ||= thumbs;
        const active = thumbs.map((item, index) => activeArchiveThumbnail(item) ? index : -1).filter(index => index >= 0);
        if (active.length !== 1 || active[0] !== 0) failImage("viewer_binding", {view_verified: false, viewer_guard: "active_thumbnail"});
        const nested = thumbs.map(item => [...item.querySelectorAll('img[data-testid="attachment-image"]')]);
        if (nested.some(items => items.length > 1)) failImage("viewer_binding", {image_count: 2, viewer_guard: "nested_images"});
        if (main.length === 1 && nested.every(items => items.length === 1)) {
          const nodes = nested.map(items => items[0]);
          if (!centers[0].contains(main[0]) || modal.querySelectorAll("img").length !== 1 ||
              strips[0].querySelectorAll("img").length !== message.images.length ||
              (imageNodes && nodes.some((item, index) => item !== imageNodes[index])))
            failImage("viewer_binding", {view_verified: false, viewer_guard: "image_containment"});
          imageNodes ||= nodes;
          let images = null;
          try {
            images = nodes.map((image, index) => imageDataBytes(image, message.images[index].mime_type));
            imageDataBytes(main[0], message.images[0].mime_type);
          } catch (error) {
            if (error?.image_structure?.stage !== "image_ready") throw error;
            images = null;
          }
          if (images) {
            sources = nodes.map(image => image.currentSrc || image.src);
            const mainSource = main[0].currentSrc || main[0].src;
            if (mainSource !== sources[0] || (opener.firstSource !== null && opener.firstSource !== sources[0]))
              failImage("viewer_binding", {view_verified: false, viewer_guard: "source_match"});
            await identity(command);
            assertDeadline(); inspectBlock(); stillBound(true);
            const again = [...strips[0].querySelectorAll(".Attachment--View--Thumbnail")];
            if (again.length !== thumbnails.length || again.some((item, index) => item !== thumbnails[index]) ||
                backdrop.querySelectorAll(".Modal--Thumbnails").length !== 1 || backdrop.querySelectorAll(".Modal--Thumbnails")[0] !== strips[0] ||
                strips[0].isConnected !== true || strips[0].querySelectorAll("img").length !== message.images.length ||
                thumbs.some((item, index) => activeArchiveThumbnail(item) !== (index === 0)))
              failImage("viewer_binding", {view_verified: false, viewer_guard: "thumbnails_changed"});
            if (nodes.some((image, index) => image.isConnected !== true || (image.currentSrc || image.src) !== sources[index] ||
                  thumbnails[index].querySelectorAll('img[data-testid="attachment-image"]').length !== 1 ||
                  thumbnails[index].querySelectorAll('img[data-testid="attachment-image"]')[0] !== image) ||
                main[0].isConnected !== true || (main[0].currentSrc || main[0].src) !== mainSource ||
                modal.querySelectorAll('img[data-testid="attachment-image"]')[0] !== main[0] || modal.querySelectorAll("img").length !== 1)
              failImage("viewer_binding", {view_verified: false, viewer_guard: "images_changed"});
            const close = [...backdrop.querySelectorAll("button")].filter(button => visible(button) &&
              (button.getAttribute("aria-label") || button.textContent || "").trim() === "Schließen");
            if (close.length !== 1 || close[0].disabled || close[0].isConnected !== true)
              failImage("viewer_binding", {view_verified: false, viewer_guard: "close_control"});
            P.assertFresh(command); stillBound(true); close[0].click();
            return images;
          }
        }
      } else if (existingImageModals().length) failImage("viewer_binding", {view_verified: false, viewer_guard: "existing_modal"});
      if (!await waitLocally(command, deadline, 125)) break;
    }
    failImage("viewer_ready", {readiness_timeout: true});
  }
  function conversation(userId) {
    const scope = document.querySelector('[data-testid="conversation-wrapper"], [data-active-conversation-id], [data-testid="conversation-detail"]');
    if (!scope) P.fail("ui_unsupported");
    const urlId = new URL(location.href).searchParams.get("conversationId");
    // A URL alone may be ahead of a stale SPA view. Require the actual view ID.
    const id = conversationId(scope);
    if (!id) P.fail("conversation_unverified");
    if (urlId && id !== urlId) P.fail("conversation_unverified");
    // Never infer the recipient from profile links inside message bodies.
    const header = scope.querySelector('[data-testid="conversation-header"], #conversation-header, .messagebox-header, header');
    if (!header) P.fail("recipient_unverified");
    const recipient = profileId(header);
    if (!recipient || recipient === userId) P.fail("recipient_unverified");
    const seen = new Set();
    const messages = [];
    for (const node of scope.querySelectorAll('[data-message-id], li[data-testid]')) {
      const messageId = node.getAttribute("data-message-id") || (UUID.test(node.getAttribute("data-testid") || "") ? node.getAttribute("data-testid") : null);
      if (!messageId || !P.ID.test(messageId) || seen.has(messageId)) continue;
      const direction = node.getAttribute("data-direction") || (node.querySelector('[data-testid="OUTBOUND"]') ? "outbound" : node.querySelector('[data-testid="INBOUND"]') ? "inbound" : null);
      const sender = node.getAttribute("data-sender-user-id") || node.getAttribute("data-sender-id") || (direction?.toLowerCase() === "outbound" ? userId : direction?.toLowerCase() === "inbound" ? recipient : null);
      if (![userId, recipient].includes(sender)) P.fail("message_sender_unverified");
      const textNode = node.querySelector('[data-testid="message-text"], [data-message-text], .message-text');
      const imageElements = [...node.querySelectorAll('[data-testid="message-attachment-container"] img[data-testid="attachment-image"], img[data-attachment-id], [data-testid="message-image"]')];
      mediaNodes.set(messageId, imageElements);
      const images = imageElements.map(img => ({
        remote_id: img.getAttribute("data-attachment-id") || img.closest('[data-attachment-id]')?.getAttribute("data-attachment-id") || null,
        unavailable: true
      }));
      // Unknown text structure is not silently archived as an empty message.
      if (!textNode && !images.length) P.fail("message_text_unverified");
      const created = node.querySelector("time[datetime]")?.getAttribute("datetime") || node.getAttribute("data-timestamp");
      seen.add(messageId);
      messages.push({message_id: messageId, sender_user_id: sender, direction: direction?.toLowerCase() || (sender === userId ? "outbound" : "inbound"), text: textNode?.textContent || "", created_at: created || null, images});
    }
    if (!messages.length && !scope.querySelector('[data-testid="empty-conversation"], [data-empty-conversation="true"]')) P.fail("ui_unsupported");
    const more = !!scope.querySelector('button[data-testid="load-more-messages"], button[data-action="load-more-messages"], [data-has-more="true"], [data-virtualized="true"]');
    return {conversation_id: id, recipient_user_id: recipient, messages, coverage: "visible_page", complete: !more};
  }
  function imageURL(image) {
    const url = new URL(image.currentSrc || image.src || "", ORIGIN);
    if (url.protocol !== "https:" || url.username || url.password || url.port ||
        !(url.hostname === "kleinanzeigen.de" || url.hostname.endsWith(".kleinanzeigen.de"))) P.fail("image_unavailable");
    return url.href;
  }
  async function transferImage(image, command) {
    P.assertFresh(command);
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), Math.min(15000, Math.max(1, Date.parse(command.expires_at) - Date.now())));
    try {
      const response = await fetch(imageURL(image), {credentials: "same-origin", redirect: "error", signal: abort.signal});
      if (response.status === 429) P.fail("upstream_rate_limited");
      if (response.status === 403) P.fail("upstream_blocked");
      if (!response.ok || !response.body || !/^image\/(?:png|jpeg|gif|webp)(?:;|$)/i.test(response.headers.get("content-type") || "")) P.fail("image_unavailable");
      const reader = response.body.getReader();
      const chunks = [];
      let total = 0;
      while (true) {
        P.assertFresh(command);
        const chunk = await reader.read();
        if (chunk.done) break;
        total += chunk.value.byteLength;
        if (total > MAX_IMAGE_BYTES) { abort.abort(); P.fail("image_too_large"); }
        chunks.push(chunk.value);
      }
      if (!total) P.fail("image_unavailable");
      const bytes = new Uint8Array(total);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      return await archiveBytes(bytes, command);
    } finally { clearTimeout(timer); }
  }
  async function hashBytes(bytes) {
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
    return [...digest].map(value => value.toString(16).padStart(2, "0")).join("");
  }
  async function archiveBytes(bytes, command) {
      if (!(bytes instanceof Uint8Array) || !bytes.byteLength || bytes.byteLength > MAX_IMAGE_BYTES) P.fail("image_unavailable");
      const total = bytes.byteLength, sha256 = await hashBytes(bytes);
      const transferId = crypto.randomUUID();
      for (let offset = 0; offset < total; offset += 32768) {
        P.assertFresh(command);
        let binary = "";
        const chunk = bytes.subarray(offset, offset + 32768);
        for (let start = 0; start < chunk.length; start += 8192) binary += String.fromCharCode(...chunk.subarray(start, start + 8192));
        const reply = await browser.runtime.sendMessage({type: "archive_image_chunk", command_id: command.id, transfer_id: transferId, offset, data_base64: btoa(binary), total_bytes: total, sha256});
        if (reply?.error || reply?.result?.accepted !== true || reply.result.next_offset !== offset + chunk.length) P.fail("image_unavailable");
      }
      return {transfer_id: transferId};
  }
  async function archiveImages(messages, command, snapshot = null) {
    let count = 0;
    const deadline = Date.now() + 5000;
    const groupDeadline = localDeadline(command, 10000);
    for (const message of messages) {
      const nodes = mediaNodes.get(message.message_id) || [];
      let groupImages = null, groupError = null;
      if (messagingRead && snapshot && message.images.length > 1 && count < 20) {
        try { groupImages = await readArchiveImageGroup(snapshot, message, command, groupDeadline); }
        catch (error) {
          if (["upstream_rate_limited", "upstream_blocked", "command_expired", "account_mismatch", "reauth_required"].includes(error?.code)) throw error;
          groupError = error;
        }
      }
      for (let i = 0; i < message.images.length; i++) {
        if (count++ >= 20) continue;
        try {
          let transferred;
          if (messagingRead && snapshot) {
            if (groupError) throw groupError;
            const image = groupImages ? groupImages[i] : await readyDOMImage(snapshot, message, command, deadline);
            transferred = await archiveBytes(image.bytes, command);
          } else if (nodes[i]) transferred = await transferImage(nodes[i], command);
          else continue;
          message.images[i] = {...transferred, remote_id: message.images[i].remote_id};
        } catch (error) {
          if (["upstream_rate_limited", "upstream_blocked", "command_expired", "account_mismatch", "reauth_required"].includes(error?.code)) throw error;
          // Deleted/unavailable individual pictures do not discard readable text.
          message.images[i].unavailable = true;
          message.images[i].error = {code: "image_unavailable", image_structure: error?.image_structure || {stage: "archive_transfer"}};
        }
      }
    }
  }
  function composer() {
    const editors = [...document.querySelectorAll('#nachricht, #viewad-contact-message, textarea[name="message"], textarea[name="text"]')].filter(visible);
    const editor = one(editors);
    const root = editor.closest("form") || editor.closest('[data-testid="conversation-wrapper"], #messagebox-bundle, #viewad-contact-form, #contact-form');
    if (!root) P.fail("ui_unsupported");
    const buttons = [...root.querySelectorAll('button[data-testid="submit-button"], button, input[type="submit"]')].filter(e => visible(e) && (/^(?:Nachricht )?Senden$/i.test((e.textContent || e.value || "").trim()) || e.getAttribute("data-testid") === "submit-button"));
    return {root, editor, button: one(buttons)};
  }
  function fileInput(form) {
    const inputs = [...form.root.querySelectorAll('input[type="file"][data-testid="reply-box-file-input"]')];
    if (inputs.length !== 1 || inputs[0].getAttribute("accept") !== "image/*" || !inputs[0].multiple) P.fail("images_unsupported");
    return inputs[0];
  }
  function attachmentPreviews(form) {
    const buttons = [...form.root.querySelectorAll("button")].filter(button =>
      (button.getAttribute("aria-label") || button.textContent || "").trim() === "Anhang entfernen");
    const previews = buttons.map(button => {
      const item = button.closest("li");
      if (!item || !form.root.contains(item)) P.fail("draft_present");
      return one(item.querySelectorAll("img"), "draft_present");
    });
    if (new Set(previews).size !== previews.length) P.fail("draft_present");
    return previews;
  }
  function unverifiedCapabilities() {
    return {scope: "current_page", text_status: "unverified", images_status: "unverified", first_contact_images_status: "unverified", can_send_text: false, can_send_images: false, first_contact_images: false};
  }
  function capabilities(snapshot = null, observedForm = null) {
    let form;
    try { form = observedForm || composer(); } catch (_) { return unverifiedCapabilities(); }
    const max = Number(form.editor.getAttribute("maxlength"));
    const input = form.root.querySelector('input[type="file"]');
    let canImages = false;
    if (messagingRead && snapshot?._view?.attachments_enabled === true) {
      try { fileInput(form); canImages = true; } catch (_) { /* Unknown input is not a verified image composer. */ }
    }
    const firstContact = location.pathname.startsWith("/s-anzeige/") && !!document.querySelector("#viewad-contact-form");
    const imagesDisabled = snapshot?._view?.attachments_enabled === false;
    return {scope: "current_page", text_status: "supported", images_status: canImages ? "supported" : imagesDisabled ? "unsupported" : "unverified", first_contact_images_status: firstContact && !input ? "unsupported" : "unverified", can_send_text: true, can_send_images: canImages, first_contact_images: false,
      image_send_mode: canImages ? "explicit_submit" : null, image_limitation: canImages ? null : imagesDisabled ? "Dieses Gespräch erlaubt keine Bildanhänge." : "Bildversand ist in dieser Safari-Oberfläche noch nicht verifiziert.",
      ...(canImages ? {max_images_per_message: 5, image_limit_source: "website"} : {}),
      accepted_image_types: (input?.getAttribute("accept") || "").split(",").map(x => x.trim()).filter(Boolean),
      max_text_length: max > 0 ? Math.min(max, MAX_TEXT) : MAX_TEXT, text_limit_source: max > 0 ? "form" : "application"};
  }
  async function readyConversationCapabilities(snapshot, command) {
    const deadline = localDeadline(command, 5000);
    for (let attempt = 0; attempt <= 40; attempt++) {
      P.assertFresh(command); inspectBlock();
      let current = unverifiedCapabilities();
      try {
        const scope = conversationView(snapshot, command), form = composer();
        if (!scope.contains(form.editor) || !scope.contains(form.button)) P.fail("conversation_unverified");
        current = capabilities(snapshot, form);
      } catch (error) {
        if (!["conversation_unverified", "listing_mismatch", "ui_unsupported"].includes(error?.code)) throw error;
      }
      // Reading server messages must not claim that an unrelated or still
      // hydrating composer can send. Wait on this document only, without any
      // additional request, and retain readable messages if UI proof is absent.
      if (current.can_send_text && (snapshot._view?.attachments_enabled !== true || current.can_send_images)) return current;
      if (attempt === 40 || !await waitLocally(command, deadline, 125)) return current;
    }
  }
  function scopedListingProfiles() {
    const result = {scopes: [], links: [], recipient: null, truncated: false, blocked: false};
    if (!/^\/s-anzeige\/(?:[^/]+\/)?[0-9]{6,20}(?:-[0-9]+){0,2}\/?$/.test(location.pathname)) return result;
    const roots = [...document.querySelectorAll(".userprofile-vip, .userprofile")];
    result.truncated = roots.length > 20;
    const seen = new Set(), ids = new Set(), visibleIds = new Set();
    let invalid = false;
    for (const root of roots.slice(0, 20)) {
      const scope = root.matches(".userprofile-vip") ? "userprofile-vip" : "userprofile";
      result.scopes.push({node: root, scope});
      const links = [...(root.matches("a[href]") ? [root] : []), ...root.querySelectorAll("a[href]")];
      for (const link of links) {
        if (seen.has(link)) continue;
        if (seen.size >= 100) { result.truncated = true; break; }
        seen.add(link);
        let url;
        try { url = new URL(link.href, ORIGIN); } catch (_) { continue; }
        if (!P.originAllowed(url.href)) continue;
        let userId;
        if (url.pathname === "/s-bestandsliste.html") {
          const values = url.searchParams.getAll("userId");
          if (values.length !== 1 || !P.USER.test(values[0]) || url.hash || [...url.searchParams.keys()].some(key => key !== "userId")) { invalid = true; continue; }
          userId = values[0];
        } else if (/^\/s-anzeigen-des-nutzers\//.test(url.pathname)) {
          userId = url.pathname.match(/^\/s-anzeigen-des-nutzers\/([0-9]{1,20})$/)?.[1];
          if (!userId || url.search || url.hash) { invalid = true; continue; }
        } else continue;
        ids.add(userId);
        if (visible(root) && visible(link)) visibleIds.add(userId);
        result.links.push({node: link, scope, userId, path: url.pathname});
      }
    }
    result.blocked = result.truncated || invalid || ids.size > 1;
    if (!result.blocked && ids.size === 1 && visibleIds.has([...ids][0])) result.recipient = [...ids][0];
    return result;
  }
  function listingTarget(args, userId, profiles = scopedListingProfiles()) {
    if (!P.LISTING.test(args.target_id || "")) P.fail("invalid_target");
    const expected = args.target_id.split("-")[0];
    const actual = location.pathname.match(/^\/s-anzeige\/(?:[^/]+\/)?([0-9]{6,20})(?:-[0-9]+){0,2}\/?$/)?.[1];
    if (expected !== actual) P.fail("listing_mismatch");
    const recipient = profiles.recipient;
    if (!recipient) P.fail("recipient_unverified");
    if (recipient === userId) P.fail("own_listing");
    return {target_kind: "listing", target_id: args.target_id, listing_id: args.target_id, account_user_id: userId, recipient_user_id: recipient};
  }
  async function readyListingTarget(args, command) {
    const deadline = localDeadline(command, 5000);
    while (true) {
      P.assertFresh(command); inspectBlock();
      const profiles = scopedListingProfiles();
      try { return listingTarget(args, command.user_id, profiles); }
      catch (error) {
        // Hydration may add the known visible seller section after document_end.
        // Contradictory, invalid or truncated evidence is never a wait condition.
        if (error?.code !== "recipient_unverified" || profiles.blocked || Date.now() >= deadline) throw error;
      }
      if (!await waitLocally(command, deadline, 125)) P.fail("recipient_unverified");
    }
  }
  async function prepare(args, command) {
    let target, snapshot;
    if (args.target_kind === "conversation") {
      const current = await openConversation(args.target_id, command);
      snapshot = current;
      target = {target_kind: "conversation", target_id: args.target_id, conversation_id: current.conversation_id, account_user_id: command.user_id, recipient_user_id: current.recipient_user_id,
        ...(messagingRead ? {listing_id: current.listing_id} : {})};
    } else if (args.target_kind === "listing") {
      target = await readyListingTarget(args, command);
      try { composer(); } catch (_) {
        const links = [...document.querySelectorAll("button, a")].filter(e => visible(e) && /^Nachricht schreiben$/i.test((e.textContent || "").trim()));
        if (links.length === 1) { P.assertFresh(command); links[0].click(); await delay(100); }
      }
    } else P.fail("invalid_target");
    return {...target, capabilities: capabilities(snapshot)};
  }
  function exactTarget(actual, expected) {
    for (const field of ["target_kind", "target_id", "listing_id", "conversation_id", "account_user_id", "recipient_user_id"])
      if ((actual[field] ?? null) !== (expected[field] ?? null)) P.fail("target_mismatch");
  }
  function observedSendFollow() {
    const candidates = [];
    for (const root of document.querySelectorAll('[data-testid="message-send-success"], [data-message-send-success="true"], [role="status"], [role="alert"], .successbox')) {
      if (!visible(root) || root.closest('[data-testid="conversation-wrapper"], [data-testid="OUTBOUND"], [data-testid="INBOUND"]') || root.parentElement?.closest('[data-message-id]')) continue;
      const explicit = root.matches('[data-testid="message-send-success"], [data-message-send-success="true"]');
      if (!explicit && !/(?:nachricht.*(?:gesendet|verschickt)|erfolgreich.*(?:gesendet|verschickt))/i.test(root.textContent || "")) continue;
      for (const anchor of root.querySelectorAll("a[href]")) {
        let candidate;
        try { candidate = P.followTarget(new URL(anchor.getAttribute("href"), ORIGIN).href); } catch (_) { continue; }
        if (!candidate) continue;
        for (const messageId of [root.getAttribute("data-message-id"), anchor.getAttribute("data-message-id")]) {
          if (!messageId) continue;
          if (!P.ID.test(messageId) || (candidate.message_id && candidate.message_id !== messageId)) P.fail("outcome_unknown");
          candidate.message_id = messageId;
        }
        candidates.push(candidate);
      }
    }
    return P.singleFollow(candidates);
  }
  async function confirmSend(command) {
    P.validateConfirmation(command);
    if (command.args.kind === "image") return confirmImage(command);
    if (messagingRead) {
      for (let attempt = 0; attempt < 2; attempt++) {
        P.assertFresh(command); inspectBlock();
        const snapshot = await messagingRead.get(command, {conversation_id: command.args.conversation_id, limit: 200, cursor: null});
        if (snapshot.account_user_id !== command.user_id || String(snapshot.listing_id || "").split("-")[0] !== command.args.target.listing_id.split("-")[0]) P.fail("outcome_unknown");
        // Only real server messages participate; local optimistic UUIDs never do.
        const receipt = P.confirmationReceipt(snapshot, command);
        if (receipt) return receipt;
        if (!attempt) await delay(500);
      }
      P.fail("outcome_unknown");
    }
    // This path never touches a composer, selects a file, or clicks a button.
    // The background opened only the canonical inbox route for the observed ID.
    const deadline = localDeadline(command, 8000);
    for (let attempt = 0; attempt < 40; attempt++) {
      P.assertFresh(command); inspectBlock();
      let snapshot;
      try { snapshot = conversation(command.user_id); }
      catch (error) {
        if (!["ui_unsupported", "conversation_unverified", "recipient_unverified", "message_sender_unverified", "message_text_unverified"].includes(error?.code)) throw error;
      }
      if (snapshot) {
        const receipt = P.confirmationReceipt(snapshot, command);
        if (receipt) return receipt;
      }
      if (!await waitLocally(command, deadline, 200)) break;
    }
    P.fail("outcome_unknown");
  }
  async function confirmImage(command) {
    if (!messagingRead) P.fail("images_unsupported");
    const args = command.args;
    try { freshChatDocument(command); }
    catch (error) { failImage("fresh_document", error?.image_structure || {fresh_document: false}, "outcome_unknown"); }
    for (let attempt = 0; attempt < 2; attempt++) {
      P.assertFresh(command); inspectBlock();
      const snapshot = await messagingRead.get(command, {conversation_id: args.conversation_id, limit: 200, cursor: null});
      if (snapshot.account_user_id !== command.user_id || snapshot.conversation_id !== args.conversation_id ||
          snapshot.recipient_user_id !== args.target.recipient_user_id || snapshot.listing_id !== args.target.listing_id) failImage("receipt_metadata", {}, "outcome_unknown");
      const candidates = snapshot.messages.filter(message => message.message_id === args.expected_message_id &&
        !args.baseline_message_ids.includes(message.message_id) && message.sender_user_id === command.user_id && message.direction === "outbound" &&
        message.text === "" && message.images?.length === 1 && P.preciseTime(message.created_at) &&
        Date.parse(message.created_at) >= Date.parse(args.not_before) && Date.parse(message.created_at) <= Date.now() + 5000);
      if (candidates.length > 1) failImage("receipt_metadata", {message_count: cappedImageCount(candidates)}, "outcome_unknown");
      if (candidates.length === 1) {
        const message = candidates[0];
        let image;
        try { image = await readyDOMImage(snapshot, message, command); }
        catch (error) {
          if (["upstream_blocked", "upstream_rate_limited", "command_expired"].includes(error?.code)) throw error;
          failImage("image_ready", error?.image_structure || {}, "outcome_unknown");
        }
        const digest = await hashBytes(image.bytes);
        let evidence = {source: "fresh_conversation_dom", sha256: digest};
        if (image.mime_type !== "image/jpeg" || digest !== args.expected_image_sha256) {
          if (!args.expected_image_pixels)
            failImage("preview_digest", {data_mime: imageMimeKind(image.mime_type), preview_digest_matches: digest === args.expected_image_sha256}, "outcome_unknown");
          if (image.width !== args.expected_image_pixels.width || image.height !== args.expected_image_pixels.height)
            failImage("pixel_compare", {pixel_dimensions_match: false}, "outcome_unknown");
          let pixels;
          try { pixels = await imagePixelProof(image, command); }
          catch (error) {
            if (["command_expired", "upstream_blocked", "upstream_rate_limited"].includes(error?.code)) throw error;
            failImage("pixel_decode", error?.image_structure || {}, "outcome_unknown");
          }
          if (!P.imagePixelsEqual(pixels, args.expected_image_pixels)) failImage("pixel_compare", {pixel_dimensions_match: true, pixel_digest_matches: false}, "outcome_unknown");
          evidence = {...evidence, comparison: "exact_pixels", pixels};
        }
        P.assertFresh(command);
        return {message_id: message.message_id, conversation_id: snapshot.conversation_id, recipient_user_id: snapshot.recipient_user_id,
          created_at: message.created_at, kind: "image", confirmed: true,
          image_evidence: evidence};
      }
      if (!attempt) await delay(500);
    }
    failImage("receipt_metadata", {message_count: 0}, "outcome_unknown");
  }
  function decodeBase64(value, maximum) {
    if (typeof value !== "string" || value.length % 4 || value.length > Math.ceil(maximum / 3) * 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) P.fail("image_unavailable");
    let binary;
    try { binary = atob(value); } catch (_) { P.fail("image_unavailable"); }
    if (!binary.length || binary.length > maximum) P.fail("image_unavailable");
    return Uint8Array.from(binary, character => character.charCodeAt(0));
  }
  async function outgoingFile(command) {
    const expected = command.args.image;
    if (!expected || !Number.isInteger(expected.size_bytes) || expected.size_bytes < 1 || expected.size_bytes > MAX_IMAGE_BYTES ||
        !/^[a-f0-9]{64}$/.test(expected.sha256 || "") || !["image/png", "image/jpeg", "image/webp"].includes(expected.mime_type) ||
        typeof expected.filename !== "string" || expected.filename.length > 200) P.fail("images_unsupported");
    const bytes = new Uint8Array(expected.size_bytes);
    for (let offset = 0; offset < bytes.length; offset += 32768) {
      P.assertFresh(command);
      const answer = await browser.runtime.sendMessage({type: "image_chunk", command_id: command.id, offset});
      const chunk = answer?.result;
      if (answer?.error || !chunk || chunk.offset !== offset || chunk.total_bytes !== bytes.length || chunk.sha256 !== expected.sha256 ||
          chunk.mime_type !== expected.mime_type || chunk.filename !== expected.filename) P.fail("image_unavailable");
      const data = decodeBase64(chunk.data_base64, 32768);
      if (data.length !== Math.min(32768, bytes.length - offset)) P.fail("image_unavailable");
      bytes.set(data, offset);
    }
    if (await hashBytes(bytes) !== expected.sha256) P.fail("image_changed");
    P.assertFresh(command);
    return new File([bytes], expected.filename, {type: expected.mime_type});
  }
  async function prepareImage(form, command) {
    const input = fileInput(form);
    if (input.disabled) P.fail("composer_not_ready");
    const file = await outgoingFile(command), transfer = new DataTransfer();
    transfer.items.add(file);
    P.assertFresh(command);
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", {bubbles: true}));
    const deadline = localDeadline(command, 5000);
    for (let attempt = 0; attempt < 50; attempt++) {
      P.assertFresh(command); inspectBlock();
      const previews = attachmentPreviews(form);
      if (previews.length > 1) P.fail("draft_present");
      if (previews.length === 1) {
        const source = previews[0].currentSrc || previews[0].src || "";
        if (!source.startsWith("data:image/jpeg;base64,")) P.fail("composer_not_ready");
        let image;
        try { image = imageDataBytes(previews[0], "image/jpeg"); }
        catch (error) {
          if (error?.image_structure?.stage !== "image_ready") throw error;
          if (!await waitLocally(command, deadline, 100)) throw error;
          continue;
        }
        const sha256 = await hashBytes(image.bytes), image_pixels = await imagePixelProof(image, command);
        return {source, sha256, image_pixels};
      }
      if (!await waitLocally(command, deadline, 100)) break;
    }
    P.fail("composer_not_ready");
  }
  async function apiReceipt(command, target, baseline, clickedAt, text, imageProof) {
    for (let attempt = 0; attempt < 2; attempt++) {
      P.assertFresh(command); inspectBlock();
      const snapshot = await messagingRead.get(command, {conversation_id: target.conversation_id, limit: 200, cursor: null});
      if (snapshot.account_user_id !== command.user_id || snapshot.conversation_id !== target.conversation_id ||
          snapshot.recipient_user_id !== target.recipient_user_id || snapshot.listing_id !== target.listing_id) P.fail("outcome_unknown");
      const candidates = snapshot.messages.filter(message => !baseline.includes(message.message_id) && message.sender_user_id === command.user_id &&
        message.direction === "outbound" && P.messageTextEqual(message.text, text) && P.preciseTime(message.created_at) &&
        Date.parse(message.created_at) >= clickedAt && Date.parse(message.created_at) <= Date.now() + 5000 &&
        (imageProof ? message.images?.length === 1 : !message.images?.length));
      if (candidates.length > 1) P.fail("outcome_unknown");
      if (candidates.length === 1) {
        const message = candidates[0];
        if (imageProof) {
          // Metadata identifies the new server message but cannot prove image
          // bytes. Persist this hint and the transformed preview hash before a
          // separate command reloads the chat into a fresh content document.
          return {_follow: {kind: "image", conversation_id: snapshot.conversation_id, message_id: message.message_id,
            dispatched_at: new Date(clickedAt).toISOString(), image_sha256: imageProof.sha256, image_pixels: imageProof.image_pixels}};
        }
        return {message_id: message.message_id, conversation_id: snapshot.conversation_id,
          recipient_user_id: snapshot.recipient_user_id, created_at: message.created_at, confirmed: true};
      }
      if (!attempt) await delay(500);
    }
    P.fail("outcome_unknown");
  }
  function sameListingPeer(conversation, target) {
    return String(conversation.listing_id || "").split("-")[0] === target.listing_id.split("-")[0] &&
      conversation.recipient_user_id === target.recipient_user_id;
  }
  async function listingBaseline(command, target) {
    const page = await messagingRead.list(command, {limit: 200, cursor: null, unread_only: false});
    // No hidden full synchronization: an incomplete first page disables this
    // recovery path. The ordinary explicit success-link path remains available.
    if (page.has_more || page.complete !== true) return null;
    const matches = page.conversations.filter(conversation => sameListingPeer(conversation, target));
    if (matches.length > 1) return null;
    if (!matches.length) return {conversation_id: null, message_ids: []};
    const snapshot = await messagingRead.get(command, {conversation_id: matches[0].conversation_id, limit: 200, cursor: null});
    if (!sameListingPeer(snapshot, target) || snapshot.account_user_id !== command.user_id) P.fail("target_mismatch");
    return {conversation_id: snapshot.conversation_id, message_ids: (snapshot._view?.messages || snapshot.messages).map(message => message.message_id)};
  }
  async function listingApiFollow(command, target, baseline, clickedAt, text) {
    if (!baseline) P.fail("outcome_unknown");
    const page = await messagingRead.list(command, {limit: 200, cursor: null, unread_only: false});
    const matches = page.conversations.filter(conversation => sameListingPeer(conversation, target));
    if (matches.length !== 1 || (baseline.conversation_id && matches[0].conversation_id !== baseline.conversation_id)) P.fail("outcome_unknown");
    const snapshot = await messagingRead.get(command, {conversation_id: matches[0].conversation_id, limit: 200, cursor: null});
    if (!sameListingPeer(snapshot, target) || snapshot.account_user_id !== command.user_id) P.fail("outcome_unknown");
    const receipts = snapshot.messages.filter(message => !baseline.message_ids.includes(message.message_id) &&
      message.sender_user_id === command.user_id && message.direction === "outbound" && P.messageTextEqual(message.text, text) && !message.images?.length &&
      P.preciseTime(message.created_at) && Date.parse(message.created_at) >= clickedAt && Date.parse(message.created_at) <= Date.now() + 5000);
    if (receipts.length !== 1) P.fail("outcome_unknown");
    // Persist the observed conversation before the backend's separate read-only
    // confirm_send. The recovery itself never acknowledges final success.
    return {_follow: {conversation_id: snapshot.conversation_id, message_id: receipts[0].message_id, dispatched_at: new Date(clickedAt).toISOString()}};
  }
  async function send(command) {
    const args = command.args;
    if (!["text", "image"].includes(args.kind) || (args.kind === "image" && (!messagingRead || args.target?.target_kind !== "conversation"))) P.fail("images_unsupported");
    const target = await prepare(args.target, command);
    exactTarget(target, args.target);
    const form = composer();
    const text = args.kind === "image" ? "" : args.text;
    if (args.kind === "text" && (typeof text !== "string" || !text.trim() || text.length > target.capabilities.max_text_length)) P.fail("invalid_text");
    if (args.kind === "image" && !target.capabilities.can_send_images) P.fail("images_unsupported");
    if (form.editor.value || form.root.querySelector('[data-testid="attachment-preview"], .attachment-preview') || attachmentPreviews(form).length || [...form.root.querySelectorAll('input[type="file"]')].some(e => e.files?.length)) P.fail("draft_present");
    const snapshot = conversationSnapshots.get(command.id);
    const baseline = target.target_kind === "conversation" ? (messagingRead ? snapshot._view?.messages || snapshot.messages : conversation(command.user_id).messages).map(m => m.message_id) : [];
    const firstContactBaseline = messagingRead && target.target_kind === "listing" ? await listingBaseline(command, target) : null;
    // The backend and background have both persisted dispatch intent before this
    // code is invoked. Never automatically try again after this point.
    P.assertFresh(command);
    let imageProof = null;
    if (args.kind === "image") {
      imageProof = await prepareImage(form, command);
      const acknowledgement = await boundedImageTask(() => browser.runtime.sendMessage({type: "image_preview", command_id: command.id,
        preview_sha256: imageProof.sha256, image_pixels: imageProof.image_pixels}), command, "preview_persistence");
      if (acknowledgement?.error || acknowledgement?.result?.accepted !== true) failImage("preview_persistence");
      P.assertFresh(command);
    }
    else {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(form.editor, text);
      form.editor.dispatchEvent(new Event("input", {bubbles: true}));
      form.editor.dispatchEvent(new Event("change", {bubbles: true}));
    }
    await delay(0);
    await identity(command);
    if (messagingRead && target.target_kind === "conversation") conversationView(snapshot, command);
    const current = target.target_kind === "listing" ? listingTarget(args.target, command.user_id) : messagingRead ? target : {...target, recipient_user_id: conversation(command.user_id).recipient_user_id, conversation_id: conversation(command.user_id).conversation_id};
    exactTarget(current, target);
    const currentForm = composer();
    if (currentForm.editor.value !== text || currentForm.button.disabled || currentForm.button.getAttribute("aria-disabled") === "true") P.fail("composer_not_ready");
    const previews = attachmentPreviews(currentForm);
    if (imageProof ? previews.length !== 1 || (previews[0].currentSrc || previews[0].src) !== imageProof.source : previews.length !== 0) P.fail("draft_present");
    P.assertFresh(command); inspectBlock();
    const beforeFollow = target.target_kind === "listing" ? observedSendFollow() : null;
    const clickedAt = Date.now();
    currentForm.button.click();
    const receiptDeadline = localDeadline(command, 8000);
    if (messagingRead && target.target_kind === "conversation") {
      // Wait on local UI only, then confirm with bounded fresh server reads.
      // The website's immediate UUID is optimistic and cannot be a receipt.
      for (let attempt = 0; attempt < 40; attempt++) {
        if (!await waitLocally(command, receiptDeadline, 200)) break;
        inspectBlock();
        try { const current = composer(); if (!current.editor.value && !attachmentPreviews(current).length) break; }
        catch (_) { break; }
      }
      return apiReceipt(command, target, baseline, clickedAt, text, imageProof);
    }
    for (let attempt = 0; attempt < 40; attempt++) {
      if (!await waitLocally(command, receiptDeadline, 200)) break;
      inspectBlock();
      if (target.target_kind === "listing") {
        const follow = observedSendFollow();
        if (follow && JSON.stringify(follow) !== JSON.stringify(beforeFollow))
          return {_follow: {...follow, dispatched_at: new Date(clickedAt).toISOString()}};
        // With the server reader installed, optimistic/stale DOM messages are
        // never a final receipt for a first contact either.
        if (messagingRead) continue;
      }
      try {
        const snapshot = conversation(command.user_id);
        if (snapshot.recipient_user_id !== target.recipient_user_id || (target.conversation_id && snapshot.conversation_id !== target.conversation_id)) P.fail("outcome_unknown");
        if (target.target_kind === "listing") {
          const receipt = P.confirmationReceipt(snapshot, {...command, args: {target, text: args.text, conversation_id: snapshot.conversation_id,
            expected_message_id: null, not_before: new Date(clickedAt).toISOString(), baseline_message_ids: baseline}});
          if (receipt) return receipt;
          continue;
        }
        const matches = snapshot.messages.filter(m => !baseline.includes(m.message_id) && m.sender_user_id === command.user_id && P.messageTextEqual(m.text, args.text));
        if (matches.length === 1) return {message_id: matches[0].message_id, conversation_id: snapshot.conversation_id, recipient_user_id: snapshot.recipient_user_id, created_at: matches[0].created_at};
      } catch (error) {
        if (!["ui_unsupported", "conversation_unverified", "recipient_unverified", "message_sender_unverified", "message_text_unverified"].includes(error?.code)) throw error;
      }
    }
    if (messagingRead && target.target_kind === "listing") return listingApiFollow(command, target, firstContactBaseline, clickedAt, text);
    P.fail("outcome_unknown");
  }
  function structure() {
    const roots = [...document.querySelectorAll('#conversation-list, [data-testid="conversation-wrapper"], #viewad-contact-form, #contact-form')];
    if (!roots.length) {
      const fallback = document.querySelector("main") || document.body;
      if (fallback) roots.push(fallback);
    }
    const profiles = scopedListingProfiles();
    // Include the same bounded seller evidence used by prepare_send before
    // navigation/footer links or an unrelated contact-form subtree use up the
    // diagnostic budget. No profile links outside these scopes are evidence.
    const profileScopes = new Map(profiles.scopes.map(value => [value.node, value.scope]));
    const profileLinks = new Map(profiles.links.map(value => [value.node, value]));
    const allProfileNodes = [...new Set([...profileScopes.keys(), ...profileLinks.keys()])];
    const profileNodes = allProfileNodes.slice(0, 40);
    const nodes = new Set([...profileNodes, ...[...document.querySelectorAll("h1, iframe[src]")].slice(0, 20)]);
    const all = document.querySelectorAll("*");
    const shadowHosts = [];
    for (let index = 0; index < Math.min(all.length, 5000); index++) if (all[index].shadowRoot) shadowHosts.push(all[index]);
    for (const host of shadowHosts.slice(0, 20)) nodes.add(host);
    const budget = roots.length ? Math.max(1, Math.floor((100 - nodes.size) / roots.length)) : 100 - nodes.size;
    for (const root of roots) {
      const candidates = new Set([root]);
      // The observed chat component renders its listing link inside HEADER.
      // Prioritize that existing proof without exporting titles or link queries.
      for (const header of [...root.querySelectorAll("header")].filter(node => !node.closest("li[data-testid]")).slice(0, 2)) {
        candidates.add(header);
        for (const link of [...header.querySelectorAll("a[href]")].filter(node => {
          try {
            const url = new URL(node.getAttribute("href"), ORIGIN);
            return P.originAllowed(url.href) && !url.search && !url.hash && /^\/zur-anzeige\/[0-9]{6,20}$/.test(url.pathname);
          } catch (_) { return false; }
        }).slice(0, 4)) candidates.add(link);
      }
      // Inspect representative message wrappers before the entire chat history;
      // otherwise a long list of message IDs hides the unknown text structure.
      for (const message of [...root.querySelectorAll('[data-testid="OUTBOUND"], [data-testid="INBOUND"]')].slice(0, 2)) {
        const ancestors = [];
        let ancestor = message.parentElement;
        while (ancestor && ancestor !== root && ancestors.length < 5) { ancestors.unshift(ancestor); ancestor = ancestor.parentElement; }
        for (const node of ancestors) candidates.add(node);
        candidates.add(message);
        for (const node of [...message.querySelectorAll("*")].slice(0, 12)) candidates.add(node);
      }
      for (const node of root.querySelectorAll('[data-testid], [data-conversation-id], [data-message-id], a[href], textarea, input[type="file"], button, time, h1, iframe[src]')) candidates.add(node);
      for (const node of [...candidates].slice(0, budget)) nodes.add(node);
    }
    const selected = [...nodes].slice(0, 100);
    const main = document.querySelector("main"), body = document.body || document.documentElement;
    const heading = scope => !!scope && [...scope.querySelectorAll("h1")].some(node => visible(node) && node.textContent?.trim() === "Meine Nachrichten");
    const emptyText = scope => {
      const text = (scope?.textContent || "").replace(/\s+/g, " ");
      return text.includes("Hier geht bald die Post ab") && text.includes("Jetzt inserieren und Anfragen erhalten oder nach spannenden Anzeigen suchen und andere Nutzer kontaktieren.");
    };
    const intro = "Hier geht bald die Post ab";
    const detail = "Jetzt inserieren und Anfragen erhalten oder nach spannenden Anzeigen suchen und andere Nutzer kontaktieren.";
    const normalize = value => (value || "").replace(/\s+/g, " ");
    const textContent = normalize(body?.textContent), innerText = normalize(body?.innerText);
    const labelled = body ? [...body.querySelectorAll("[aria-label], [alt]")].slice(0, 2000).filter(visible) : [];
    const labelContains = expected => labelled.some(node => ["aria-label", "alt"].some(name => normalize(node.getAttribute(name)).includes(expected)));
    const variants = {intro_text_content: textContent.includes(intro), detail_text_content: textContent.includes(detail),
      intro_inner_text: innerText.includes(intro), detail_inner_text: innerText.includes(detail),
      detail_without_whitespace: textContent.replace(/\s/g, "").includes(detail.replace(/\s/g, "")),
      detail_without_terminal_dot: textContent.includes(detail.slice(0, -1)),
      intro_aria_alt: labelContains(intro), detail_aria_alt: labelContains(detail)};
    return {page: location.pathname, content_revision: CONTENT_REVISION, empty_heading_in_main: heading(main), empty_text_in_main: emptyText(main),
      empty_heading_in_document: heading(body), empty_text_in_document: emptyText(body),
      empty_variants: variants, open_shadow_host_count: Math.min(shadowHosts.length, 100),
      ...(/^\/s-anzeige\//.test(location.pathname) ? {listing_recipient_verified: profiles.recipient !== null && allProfileNodes.length <= 40} : {}),
      truncated: all.length > 5000 || shadowHosts.length > 20 || nodes.size > 100 || profiles.truncated || allProfileNodes.length > 40,
      nodes: selected.map(node => {
      const result = {tag: node.tagName.toLowerCase(), attributes: [...node.attributes].map(a => a.name).filter(name => !/^on/i.test(name)), visible: visible(node),
        children_tags: [...node.children].slice(0, 12).map(child => child.tagName.toLowerCase()),
        has_direct_text: [...node.childNodes].some(child => child.nodeType === 3 && (child.textContent || "").trim().length > 0)};
      const parentIndex = selected.indexOf(node.parentElement);
      if (parentIndex >= 0) result.parent_index = parentIndex;
      const profileScope = profileScopes.get(node) || profileLinks.get(node)?.scope;
      if (profileScope) result.profile_scope = profileScope;
      const profileLink = profileLinks.get(node);
      if (profileLink) { result.userId = profileLink.userId; result.link_path = profileLink.path; }
      for (const name of ["id", "data-testid", "data-conversation-id", "data-active-conversation-id", "data-message-id", "data-direction", "accept", "type", "data-auto-send", "maxlength"])
        if (node.hasAttribute(name)) result[name] = node.getAttribute(name)?.slice(0, 120);
      if (node.matches("button, input, textarea")) result.disabled = node.disabled === true;
      if (node.shadowRoot) result.shadow_child_count = Math.min(node.shadowRoot.children.length, 100);
      if (node.matches("iframe[src]")) {
        try {
          const url = new URL(node.getAttribute("src"), ORIGIN);
          if (P.originAllowed(url.href) && ["/m-meine-anzeigen.html", "/portal/anzeigen", "/m-nachrichten.html", "/portal/nachrichten"].includes(url.pathname)) result.frame_path = url.pathname;
        } catch (_) { /* No foreign frame URLs or query values leave the page. */ }
      }
      if (node.matches("a[href]")) {
        try {
          const url = new URL(node.href, ORIGIN);
          if (P.originAllowed(url.href) && /^(?:\/m-nachrichten\.html|\/portal\/(?:nachrichten|anzeigen)$|\/s-bestandsliste\.html|\/s-anzeigen-des-nutzers\/\d+|\/s-anzeige\/|\/zur-anzeige\/\d{6,20}$)/.test(url.pathname)) {
            result.link_path = url.pathname.slice(0, 250);
            for (const name of ["conversationId", "userId", "messageId"])
              if (P.ID.test(url.searchParams.get(name) || "")) result[name] = url.searchParams.get(name);
          }
        } catch (_) { /* No arbitrary URLs leave the page. */ }
      }
      return result;
    })};
  }
  async function execute(command) {
    P.validate(command); pageAllowed(); inspectBlock();
    mediaNodes.clear();
    conversationSnapshots.clear();
    const verified = await identity(command);
    if (command.method === "identify") return {...verified, identity_page_path: location.pathname, capabilities: capabilities(),
      _upstream_reached: verified._upstream_reached === true || (command.fresh_navigation?.document_id === documentId && command.fresh_navigation.path === location.pathname)};
    let result;
    if (command.method === "inspect_ui") result = structure();
    else if (command.method === "list_conversations") {
      if (messagingRead) result = await messagingRead.list(command, command.args);
      else {
        const snapshot = await readyInbox(command);
        const items = command.args.unread_only ? snapshot.conversations.filter(c => c.unread) : snapshot.conversations;
        const selection = page(items, command.args, "conversation_id");
        result = {conversations: selection.items, ...P.pagination(selection, snapshot.more)};
      }
    } else if (command.method === "get_conversation") {
      if (messagingRead) {
        const snapshot = await messagingRead.get(command, command.args);
        await archiveImages(snapshot.messages, command, snapshot);
        result = {...snapshot, capabilities: await readyConversationCapabilities(snapshot, command)};
        if (command.args.cursor == null && typeof messagingRead.providerContext === "function")
          result.provider_context = await messagingRead.providerContext(command, snapshot);
      } else {
        const snapshot = await openConversation(command.args.conversation_id, command);
        const selection = page(snapshot.messages, command.args, "message_id");
        await archiveImages(selection.items, command);
        result = {...snapshot, messages: selection.items, ...P.pagination(selection, !snapshot.complete), capabilities: capabilities()};
      }
    } else if (command.method === "prepare_send") result = await prepare(command.args, command);
    else if (command.method === "send_step") result = await send(command);
    else if (command.method === "confirm_send") result = await confirmSend(command);
    else P.fail("invalid_command");
    // Opaque media references and the complete UI-binding snapshot are private to
    // this command's isolated content world, never native/API payload fields.
    if (result?._view) { const {_view, ...publicResult} = result; result = publicResult; }
    if (result?.messages) result.messages = result.messages.map(message => ({...message,
      images: (message.images || []).map(image => { const {reference, _dom_attachment, ...publicImage} = image; return publicImage; })}));
    return {...result, account_user_id: verified.user_id, _upstream_reached: true};
  }
  browser.runtime.onMessage.addListener((message, sender) => {
    if (sender?.id !== browser.runtime.id) {
      // Diagnostics disclose no document data and do not weaken the sender gate.
      if (message?.type === "kleinanzeigen-document-marker") return Promise.resolve({error: {code: "marker_sender_mismatch"}});
      return undefined;
    }
    if (message?.type === "kleinanzeigen-document-marker") {
      try {
        pageAllowed(); inspectBlock();
        return Promise.resolve({document_id: documentId, navigation_started_at: performance.timeOrigin, ready_state: document.readyState,
          url: location.origin + location.pathname});
      } catch (error) { return Promise.resolve({error: {code: P.errorCode(error)}}); }
    }
    if (message?.type !== "kleinanzeigen-command") return undefined;
    return execute(message.command).then(result => ({result}), error => ({error: {code: P.errorCode(error), content_revision: CONTENT_REVISION,
      ...(error?.image_structure ? {image_structure: error.image_structure} : {}),
      ...(error?.reader_structure ? {reader_structure: error.reader_structure} : {}),
      ...(error?.identity_structure ? {identity_structure: error.identity_structure} : {})}}));
  });
  if (bootstrapState) {
    bootstrapState.handlerReady = true;
    bootstrapState.stage = "handler_ready";
  }
})();
