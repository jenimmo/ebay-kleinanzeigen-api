/* The timer polls only the local native bridge. No scheduled website polling. */
"use strict";
const P = KleinanzeigenProtocol;
const NATIVE_APP = "de.matthias.kleinanzeigen.safari";
let pendingAccounts = [];
let latestError = null;
let polling = false;
let pollStatePromise = null;
let timer = null;
let runningCommand = null;
let lastPairError = null;
let pairState = null;
const RESULT_RESERVE_MS = 5000;
const FOLLOW_RESERVE_MS = 3000;
const commandContentDeadline = command => Date.parse(command.expires_at) - RESULT_RESERVE_MS - (command.method === "send_step" ? FOLLOW_RESERVE_MS : 0);

async function beforeDeadline(operation, deadline, code) {
  if (!Number.isFinite(deadline) || Date.now() >= deadline) P.fail(code);
  let timerId;
  const expired = new Promise((_, reject) => {
    timerId = setTimeout(() => {const error = new Error(code); error.code = code; reject(error);}, Math.max(1, deadline - Date.now()));
  });
  try {
    const result = await Promise.race([Promise.resolve().then(() => {
      if (Date.now() >= deadline) P.fail(code);
      return operation();
    }), expired]);
    // The browser RPC cannot be cancelled. A late completion has no authority
    // to resume the expired command or produce a receipt.
    if (Date.now() >= deadline) P.fail(code);
    return result;
  } finally { clearTimeout(timerId); }
}

async function native(message, resultDeadline = null) {
  if (resultDeadline !== null && Date.now() >= resultDeadline) P.fail("safari_native_timeout");
  let timerId;
  const deadline = new Promise((_, reject) => {
    timerId = setTimeout(() => {
      const error = new Error("safari_native_timeout");
      error.code = "safari_native_timeout";
      reject(error);
    }, Math.min(40000, resultDeadline === null ? 40000 : Math.max(1, resultDeadline - Date.now()))); // Native URLSession resource limit is 35 seconds, plus IPC startup.
  });
  try {
    // Safari's native-messaging Promise cannot be cancelled. Only the bounded
    // winner is consumed; a late reply must never dispatch a delayed command.
    const answer = await Promise.race([
      browser.runtime.sendNativeMessage(NATIVE_APP, {version: 1, ...message}),
      deadline
    ]);
    if (resultDeadline !== null && Date.now() >= resultDeadline) P.fail("safari_native_timeout");
    if (!answer || typeof answer !== "object") P.fail("invalid_bridge_reply");
    if (answer.error) P.fail(P.errorCode(answer.error));
    return answer;
  } finally { clearTimeout(timerId); }
}

async function documentMarker(tabId, delivery = null, deadline = Date.now() + 2000) {
  const report = state => { if (delivery) delivery.value = state; };
  let answer;
  try {
    answer = await beforeDeadline(() => browser.tabs.sendMessage(tabId, {type: "kleinanzeigen-document-marker"}), Math.min(deadline, Date.now() + 2000), "safari_content_timeout");
  } catch (error) {
    report(error?.code === "safari_content_timeout" ? "no_reply" : "send_rejected");
    return null;
  }
  if (answer === undefined || answer === null) { report("no_reply"); return null; }
  if (answer?.error) {
    const code = P.errorCode(answer.error);
    report(code === "marker_sender_mismatch" ? "sender_rejected" : "handler_error");
    if (["upstream_blocked", "upstream_rate_limited"].includes(code)) P.fail(code);
    return null;
  }
  if (typeof answer !== "object" || typeof answer.document_id !== "string" || !P.ID.test(answer.document_id) ||
      typeof answer.url !== "string" || !["loading", "interactive", "complete"].includes(answer.ready_state)) {
    report("invalid_reply"); return null;
  }
  report("received");
  return answer;
}

async function contentBootstrap(tabId, deadline = Date.now() + 2000) {
  const result = {delivery: "no_reply", stage: "unavailable", protocol_ready: false, secure_random_ready: false, handler_ready: false};
  let answer;
  try { answer = await beforeDeadline(() => browser.tabs.sendMessage(tabId, {type: "kleinanzeigen-content-bootstrap"}), Math.min(deadline, Date.now() + 2000), "safari_content_timeout"); }
  catch (error) { return {...result, delivery: error?.code === "safari_content_timeout" ? "no_reply" : "send_rejected"}; }
  if (answer === undefined || answer === null) return result;
  if (answer?.error?.code === "marker_sender_mismatch") return {...result, delivery: "sender_rejected"};
  if (!answer || typeof answer !== "object" || !["bootstrap", "content_started", "initialized", "handler_ready"].includes(answer.stage) ||
      !["protocol_ready", "secure_random_ready", "handler_ready"].every(key => typeof answer[key] === "boolean")) return {...result, delivery: "invalid_reply"};
  return {delivery: "received", stage: answer.stage, protocol_ready: answer.protocol_ready,
    secure_random_ready: answer.secure_random_ready, handler_ready: answer.handler_ready};
}

async function waitForTab(tabId, command, destination, before, startedAt) {
  const deadline = Math.min(Date.parse(command.expires_at), Date.now() + 30000);
  const diagnostic = {tab_complete: false, target_matched: false, marker_received: false, marker_ready: false, fresh_document: false};
  const markerDelivery = {value: "no_reply"};
  while (Date.now() < deadline) {
    let tab;
    try { tab = await beforeDeadline(() => browser.tabs.get(tabId), Math.min(deadline, Date.now() + 2000), "safari_navigation_timeout"); }
    catch (error) { if (error?.code === "safari_navigation_timeout") break; throw error; }
    if (!P.originAllowed(tab.url)) P.fail("reauth_required");
    if (/^\/m-einloggen\.html/.test(new URL(tab.url).pathname)) P.fail("reauth_required");
    diagnostic.tab_complete = tab.status === "complete";
    diagnostic.target_matched = P.targetMatches(tab.url, destination);
    if (diagnostic.target_matched) {
      const delivery = {value: "no_reply"};
      const marker = await documentMarker(tabId, delivery, deadline);
      // Keep the last completed observation if only the overall deadline
      // prevented a final probe; that is not evidence of missing injection.
      if (!marker && Date.now() >= deadline) break;
      markerDelivery.value = delivery.value;
      const newDocument = marker && (before ? marker.document_id !== before.document_id :
        Number.isFinite(marker.navigation_started_at) && marker.navigation_started_at >= startedAt);
      diagnostic.marker_received = !!marker;
      diagnostic.marker_ready = !!marker && ["interactive", "complete"].includes(marker.ready_state);
      diagnostic.fresh_document = !!newDocument;
      // Wait for a newly parsed document, not for unrelated advertisements and
      // third-party subresources to finish loading. Identity is verified next.
      // Marker URLs intentionally omit queries. The tab URL above verifies the
      // canonical conversationId; the content parser separately verifies the
      // active conversation before returning any receipt.
      const documentDestination = new URL(destination);
      documentDestination.search = "";
      if (newDocument && diagnostic.marker_ready && P.targetMatches(marker.url, documentDestination.href)) return marker;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const error = new Error("safari_navigation_timeout");
  error.code = "safari_navigation_timeout";
  error.navigation_state = diagnostic;
  error.marker_delivery = markerDelivery.value;
  error.content_bootstrap = await contentBootstrap(tabId, Date.parse(command.expires_at));
  throw error;
}

async function claimDispatch(command) {
  // Persist BEFORE handing a potentially sending action to a content script.
  // The journal intentionally is not pruned: restarting Safari must never replay.
  const key = "dispatch:" + command.id;
  const previous = await browser.storage.local.get(key);
  if (previous[key]) P.fail("outcome_unknown");
  await browser.storage.local.set({[key]: {state: "attempted", at: new Date().toISOString()}});
}

async function navigationFollow(command, oldDocumentId, startedAt) {
  if (command.method !== "send_step" || command.args.kind !== "text" || command.args.target?.target_kind !== "listing" || !oldDocumentId) return null;
  // A full navigation can destroy the sending content world's response channel.
  // Observe only a fresh known inbox document; this is a hint, never a receipt.
  const tabId = command.args.tab_id;
  const deadline = Math.min(Date.parse(command.expires_at) - RESULT_RESERVE_MS, Date.now() + FOLLOW_RESERVE_MS);
  while (Date.now() < deadline) {
    let tab;
    try { tab = await beforeDeadline(() => browser.tabs.get(tabId), Math.min(deadline, Date.now() + 2000), "safari_content_timeout"); }
    catch (_) { return null; }
    const follow = P.followTarget(tab.url);
    if (!follow) return null;
    const marker = await documentMarker(tabId, null, deadline);
    if (marker && marker.document_id !== oldDocumentId && marker.navigation_started_at >= startedAt &&
        ["interactive", "complete"].includes(marker.ready_state) && P.originAllowed(marker.url) &&
        new URL(marker.url).pathname === new URL(tab.url).pathname) {
      let current;
      try { current = await beforeDeadline(() => browser.tabs.get(tabId), Math.min(deadline, Date.now() + 2000), "safari_content_timeout"); }
      catch (_) { return null; }
      if (JSON.stringify(P.followTarget(current.url)) !== JSON.stringify(follow)) return null;
      return {_follow: {...follow, dispatched_at: new Date(startedAt).toISOString()},
        account_user_id: command.user_id, _upstream_reached: false};
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  return null;
}

function recoveryTab(tab) {
  if (!tab || !Number.isInteger(tab.id) || tab.id < 0 || tab.incognito !== false || !P.originAllowed(tab.url)) return false;
  return ["/m-meine-anzeigen.html", "/m-einstellungen.html", "/m-mein-konto.html", "/m-nachrichten.html", "/portal/anzeigen", "/portal/nachrichten"].includes(new URL(tab.url).pathname);
}

async function recoverTab(command) {
  // A Safari profile owns this background instance and its tabs API. The native
  // host supplies the profile UUID; only an explicitly queued connect command
  // can enter this branch. Idle polls and sending commands never recover tabs.
  const deadline = Date.parse(command.expires_at) - RESULT_RESERVE_MS;
  let tabs;
  try { tabs = await beforeDeadline(() => browser.tabs.query({url: "https://www.kleinanzeigen.de/*"}), Math.min(deadline, Date.now() + 2000), "safari_recovery_unavailable"); }
  catch (_) { P.fail("safari_recovery_unavailable"); }
  if (!Array.isArray(tabs)) P.fail("safari_recovery_unavailable");
  const candidates = tabs.filter(recoveryTab);
  if (!candidates.length) P.fail("safari_tab_missing");
  if (candidates.length !== 1) P.fail("safari_tab_ambiguous");
  const tabId = candidates[0].id;
  let current;
  try { current = await beforeDeadline(() => browser.tabs.get(tabId), Math.min(deadline, Date.now() + 2000), "safari_content_timeout"); }
  catch (error) { if (error?.code === "safari_content_timeout") throw error; P.fail("safari_tab_missing"); }
  if (!recoveryTab(current)) P.fail("safari_tab_missing");
  P.assertFresh(command);
  // Existing identify navigates to a fresh own-account page without activating
  // the tab. No composer, message or upload method is used for recovery.
  const result = await execute({...command, method: "identify", args: {tab_id: tabId}});
  if (result.user_id !== command.user_id) P.fail("account_mismatch");
  try { current = await beforeDeadline(() => browser.tabs.get(tabId), Math.min(deadline, Date.now() + 2000), "safari_content_timeout"); }
  catch (error) { if (error?.code === "safari_content_timeout") throw error; P.fail("safari_tab_missing"); }
  if (!recoveryTab(current) || !["/m-meine-anzeigen.html", "/portal/anzeigen"].includes(new URL(current.url).pathname)) P.fail("safari_tab_missing");
  P.assertFresh(command);
  return {...result, account_user_id: result.user_id, recovered_tab_id: tabId};
}

async function execute(command) {
  P.validate(command);
  if (command.method === "recover_tab") return recoverTab(command);
  const contentDeadline = commandContentDeadline(command);
  const contentCommand = {...command, expires_at: new Date(contentDeadline).toISOString()};
  P.assertFresh(contentCommand);
  const tabId = command.args.tab_id;
  let tab;
  try { tab = await beforeDeadline(() => browser.tabs.get(tabId), Math.min(contentDeadline, Date.now() + 2000), "safari_content_timeout"); }
  catch (error) { if (error?.code === "safari_content_timeout") throw error; P.fail("safari_tab_missing"); }
  if (!tab || typeof tab.url !== "string") P.fail("safari_tab_missing");
  if (!P.originAllowed(tab.url)) P.fail("unsafe_tab");
  if (command.method === "disconnect") return {disconnected: true};
  const routeKey = "identity-route:" + command.account_id;
  const routeState = (await beforeDeadline(() => browser.storage.local.get(routeKey), contentDeadline, "safari_content_timeout"))[routeKey];
  const identityPath = routeState?.user_id === command.user_id && ["/m-meine-anzeigen.html", "/portal/anzeigen"].includes(routeState.path) ? routeState.path : "/m-meine-anzeigen.html";
  const destination = P.targetURL({...command, identity_page_path: identityPath});
  let freshNavigation = null;
  if (destination) {
    // Each backend command owns a shared outbound permit covering this navigation,
    // the fresh account check and the requested UI interaction.
    P.assertFresh(contentCommand);
    const before = await documentMarker(tabId, null, contentDeadline);
    const startedAt = Date.now();
    await beforeDeadline(() => browser.tabs.update(tabId, {url: destination}), Math.min(contentDeadline, Date.now() + 5000), "safari_navigation_timeout");
    const marker = await waitForTab(tabId, contentCommand, destination, before, startedAt);
    freshNavigation = {document_id: marker.document_id, path: new URL(marker.url).pathname};
  }
  P.assertFresh(contentCommand);
  if (command.method === "send_step") await beforeDeadline(() => claimDispatch(contentCommand), contentDeadline, "outcome_unknown");
  P.assertFresh(contentCommand);
  const contentStartedAt = Date.now();
  let response, channelError;
  try {
    response = await beforeDeadline(() => {
      P.assertFresh(contentCommand);
      return browser.tabs.sendMessage(tabId, {type: "kleinanzeigen-command", command: {...contentCommand, fresh_navigation: freshNavigation, identity_page_path: identityPath}});
    }, contentDeadline, "safari_content_timeout");
  } catch (error) { channelError = error; }
  if (channelError || !response || ["outcome_unknown", "command_expired"].includes(response.error?.code)) {
    const hint = await navigationFollow(command, freshNavigation?.document_id, contentStartedAt);
    if (hint) return hint;
  }
  if (channelError) P.fail(command.method === "send_step" ? "outcome_unknown" : P.errorCode(channelError));
  if (!response || typeof response !== "object") P.fail(command.method === "send_step" ? "outcome_unknown" : "invalid_page_reply");
  if (response.error) {
    const code = P.errorCode(response.error);
    const error = new Error(code); error.code = code;
    if (response.error.identity_structure) error.identity_structure = response.error.identity_structure;
    if (response.error.reader_structure) error.reader_structure = response.error.reader_structure;
    if (response.error.image_structure) error.image_structure = response.error.image_structure;
    if (/^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$/.test(response.error.content_revision || "")) error.content_revision = response.error.content_revision;
    throw error;
  }
  if (command.method === "identify" && command.user_id && response.result?.user_id !== command.user_id) P.fail("account_mismatch");
  if (command.method === "identify" && P.USER.test(response.result?.user_id || "") &&
      ["/m-meine-anzeigen.html", "/portal/anzeigen"].includes(response.result.identity_page_path))
    await beforeDeadline(() => browser.storage.local.set({[routeKey]: {user_id: response.result.user_id, path: response.result.identity_page_path}}), contentDeadline, "safari_content_timeout");
  return response.result;
}

async function deliver(command) {
  runningCommand = command || null;
  let envelope;
  try { envelope = {result: await execute(command)}; }
  catch (error) {
    envelope = {error: {code: P.errorCode(error)}};
    if (error.identity_structure) envelope.error.identity_structure = error.identity_structure;
    if (error.reader_structure) envelope.error.reader_structure = error.reader_structure;
    if (error.image_structure) envelope.error.image_structure = error.image_structure;
    if (error.content_revision) envelope.error.content_revision = error.content_revision;
    if (envelope.error.code === "safari_navigation_timeout" && error.navigation_state) {
      envelope.error.navigation_state = error.navigation_state;
      envelope.error.marker_delivery = error.marker_delivery;
      envelope.error.content_bootstrap = error.content_bootstrap;
    }
  }
  if (new TextEncoder().encode(JSON.stringify(envelope)).length > 60000)
    envelope = {error: {code: "response_too_large"}};
  try {
    // Results are not retried. A missing receipt remains unknown in the backend.
    await native({action: "result", command_id: command.id, ...envelope}, Date.parse(command.expires_at));
  } catch (error) { latestError = P.errorCode(error); }
  finally { runningCommand = null; }
}

function statusSnapshot() {
  return {pending_accounts: pendingAccounts, error: latestError, running: !!runningCommand, last_pair_error: lastPairError, pair_state: pairState};
}

function validatePollReply(answer) {
  if (!Object.prototype.hasOwnProperty.call(answer, "command") ||
      !(answer.command === null || (answer.command && typeof answer.command === "object" && !Array.isArray(answer.command))) ||
      !Array.isArray(answer.pending_accounts) || answer.pending_accounts.length > 100 ||
      answer.pending_accounts.some(a => !a || typeof a !== "object" || !P.ACCOUNT.test(a.account_id || "") ||
        (a.label !== undefined && (typeof a.label !== "string" || a.label.length > 128)))) P.fail("invalid_bridge_reply");
  return answer;
}

function poll() {
  if (polling) return pollStatePromise;
  polling = true;
  let stateReady;
  const statePromise = new Promise(resolve => { stateReady = resolve; });
  pollStatePromise = statePromise;
  void (async () => {
    let nextPollDelay = 5000;
    try {
      const answer = validatePollReply(await native({action: "poll"}));
      pendingAccounts = answer.pending_accounts;
      latestError = null;
      // Only the local queue is polled. Idle profiles need fewer native IPCs;
      // pending pairing or a just-delivered command stays responsive.
      if (pendingAccounts.length || answer.command) nextPollDelay = 1000;
      // Popup waits for the fresh local response, not the possibly lengthy UI
      // command. A concurrent status caller shares this exact in-flight poll.
      stateReady();
      if (answer.command) await deliver(answer.command);
    } catch (error) {
      latestError = P.errorCode(error);
      pendingAccounts = [];
      stateReady();
    } finally {
      polling = false;
      pollStatePromise = null;
      clearTimeout(timer);
      timer = setTimeout(poll, latestError ? 5000 : nextPollDelay);
    }
  })();
  return statePromise;
}

browser.runtime.onMessage.addListener((message, sender) => {
  if (sender.id === browser.runtime.id && sender.tab && P.originAllowed(sender.tab.url) && message?.type === "image_preview") {
    return (async () => {
      const command = runningCommand;
      const hasPixels = Object.prototype.hasOwnProperty.call(message, "image_pixels");
      if (!command || command.method !== "send_step" || command.args.kind !== "image" ||
          command.id !== message.command_id || command.args.tab_id !== sender.tab.id ||
          typeof message.preview_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(message.preview_sha256) ||
          (hasPixels && !P.validImagePixels(message.image_pixels))) P.fail("invalid_command");
      const deadline = commandContentDeadline(command);
      const contentCommand = {...command, expires_at: new Date(deadline).toISOString()};
      P.assertFresh(contentCommand);
      const result = await native({action: "image_preview", command_id: message.command_id, preview_sha256: message.preview_sha256,
        ...(hasPixels ? {image_pixels: message.image_pixels} : {})}, deadline);
      if (runningCommand !== command) P.fail("invalid_command");
      P.assertFresh(contentCommand);
      if (result.accepted !== true) P.fail("invalid_bridge_reply");
      return {result: {accepted: true}};
    })().catch(error => ({error: {code: P.errorCode(error)}}));
  }
  if (sender.id === browser.runtime.id && sender.tab && P.originAllowed(sender.tab.url) && message?.type === "image_chunk") {
    return (async () => {
      if (!runningCommand || runningCommand.method !== "send_step" || runningCommand.args.kind !== "image" ||
          runningCommand.id !== message.command_id || runningCommand.args.tab_id !== sender.tab.id) P.fail("invalid_command");
      P.assertFresh(runningCommand);
      const result = await native({action: "image_chunk", command_id: message.command_id, offset: message.offset});
      return {result};
    })().catch(error => ({error: {code: P.errorCode(error)}}));
  }
  if (sender.id === browser.runtime.id && sender.tab && P.originAllowed(sender.tab.url) && message?.type === "archive_image_chunk") {
    return (async () => {
      if (!runningCommand || runningCommand.method !== "get_conversation" || runningCommand.id !== message.command_id || runningCommand.args.tab_id !== sender.tab.id) P.fail("invalid_command");
      P.assertFresh(runningCommand);
      const result = await native({action: "archive_image_chunk", command_id: message.command_id, transfer_id: message.transfer_id,
        offset: message.offset, data_base64: message.data_base64, total_bytes: message.total_bytes, sha256: message.sha256});
      return {result};
    })().catch(error => ({error: {code: P.errorCode(error)}}));
  }
  // Only extension pages may pair a tab; website/content messages cannot enroll it.
  if (sender.id !== browser.runtime.id || sender.tab) return undefined;
  if (message?.type === "status") {
    return (async () => { await poll(); return statusSnapshot(); })();
  }
  if (message?.type === "pair") {
    return (async () => {
      lastPairError = null;
      pairState = "checking_tab";
      if (!pendingAccounts.some(a => a.account_id === message.account_id)) P.fail("pairing_not_pending");
      let tabs;
      try { tabs = await browser.tabs.query({active: true, currentWindow: true}); }
      catch (_) { P.fail("safari_active_tab_unavailable"); }
      if (tabs.length !== 1 || !Number.isInteger(tabs[0].id) || !P.originAllowed(tabs[0].url)) P.fail("unsafe_tab");
      pairState = "pairing";
      const response = await native({action: "pair", account_id: message.account_id, tab_id: tabs[0].id});
      if (response.accepted !== true || response.identity_pending !== true) P.fail("invalid_bridge_reply");
      pairState = "accepted";
      if (!polling) void poll();
      return {paired: true};
    })().catch(error => {
      lastPairError = P.errorCode(error);
      pairState = "failed";
      return {error: {code: lastPairError}};
    });
  }
  return undefined;
});
void poll();
