"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {webcrypto, createHash} = require("node:crypto");
const P = require("../protocol.js");
const BASE_ID = "11111111-1111-4111-8111-111111111111";
const NEW_ID = "22222222-2222-4222-8222-222222222222";
const SYSTEM_ID = "44444444-4444-4444-8444-444444444444";
const ORIGINAL = Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10]);
const JPEG = Uint8Array.from([255, 216, 255, 224, 0, 2, 255, 217]);
const RGBA = Uint8Array.from([47, 160, 96, 255]);
const hash = bytes => createHash("sha256").update(bytes).digest("hex");
const pixelProof = {algorithm: "srgb-rgba8-v1", width: 1, height: 1, sha256: hash(RGBA)};
const variantJPEG = Uint8Array.from([...JPEG, 0]);
const image = {image_id: "own-image", filename: "own-image.png", mime_type: "image/png", size_bytes: ORIGINAL.length, sha256: hash(ORIGINAL)};
const target = {target_kind: "conversation", target_id: "c-own", conversation_id: "c-own", listing_id: "1234500001", account_user_id: "12345", recipient_user_id: "67890"};
function command(method = "send_step", changes = {}) {
  return {id: "one-command", account_id: "own_a", user_id: "12345", method,
    expires_at: new Date(Date.now() + 30000).toISOString(), args: {tab_id: 7, target, kind: "text", text: "Grüße mit Umlauten", ...changes}};
}
function fixture(options = {}) {
  let listener, clicks = 0, selections = 0, profileReads = 0, detailReads = 0, imageReads = 0, chunkReads = 0, listReads = 0;
  let elapsed = 0, waits = 0;
  const boundedTimers = new Map(), pixelCalls = [], previewMessages = [], previewClicks = [], archiveChunks = [];
  let timerSequence = 0, canvasCalls = 0, bitmapCloses = 0;
  let viewerClicks = 0, viewerCloses = 0, viewerOpen = !!options.existingViewer;
  const startedAt = Date.now();
  class Clock extends Date {
    constructor(...args) {super(...(args.length ? args : [startedAt + elapsed]));}
    static now() {return startedAt + elapsed;}
  }
  const hydrated = () => elapsed >= (options.hydrateAfter || 0);
  const fileReady = () => !options.noFileInput && elapsed >= (options.fileAfter || 0);
  let previews = options.existingPreview ? [JPEG] : [];
  const window = {}; window.top = window;
  class Editor {
    get value() { return this.text || ""; }
    set value(value) { this.text = value; }
    closest() { return form; }
    getAttribute(name) { return name === "maxlength" ? "20000" : null; }
    checkVisibility() { return !options.noComposer && hydrated(); }
    dispatchEvent() {}
  }
  const editor = new Editor(); editor.value = options.existingText || "";
  const button = {disabled: false, textContent: "Senden", checkVisibility: () => true, getAttribute: () => null,
    click() {clicks++; if (!options.keepDraft) {editor.value = ""; previews = [];}}};
  const input = {multiple: true, disabled: false, files: [], getAttribute: name => name === "accept" ? "image/*" : null,
    dispatchEvent() {selections++; if (!options.noPreview) previews = options.extraPreview ? [JPEG, JPEG] : [JPEG];}};
  function previewButtons() {
    return previews.map(bytes => {
      const img = {src: "data:image/jpeg;base64," + Buffer.from(bytes).toString("base64"), checkVisibility: () => true,
        complete: !options.previewLoading, naturalWidth: options.pixelWidth || 1, naturalHeight: options.pixelHeight || 1};
      const li = {querySelectorAll: () => [img]};
      return {getAttribute: () => "Anhang entfernen", closest: () => li};
    });
  }
  const form = {
    contains: () => true,
    querySelector: selector => selector === 'input[type="file"]' && fileReady() ? input : null,
    querySelectorAll: selector => {
      if (selector === 'input[type="file"][data-testid="reply-box-file-input"]' || selector === 'input[type="file"]') return fileReady() ? [input] : [];
      if (selector === "button") return [button, ...previewButtons()];
      if (selector.startsWith('button[data-testid="submit-button"]')) return [button];
      return [];
    }
  };
  const header = {checkVisibility: () => true, closest: () => null,
    querySelectorAll: () => [{getAttribute: () => "/zur-anzeige/" + (options.headerListing || "1234500001")}]};
  const imageNode = {isConnected: true, checkVisibility: () => !options.hiddenImage, complete: !options.imageUnavailable, naturalWidth: options.pixelWidth || 1, naturalHeight: options.pixelHeight || 1,
    src: options.imageSource || "data:image/jpeg;base64," + Buffer.from(options.imageMismatch ? ORIGINAL : JPEG).toString("base64")};
  const groupImages = Array.from({length: options.groupCount || 0}, (_, index) => ({...imageNode,
    src: "data:image/jpeg;base64," + Buffer.from(index === 0 || options.groupSameBytes ? JPEG : [...JPEG, index]).toString("base64")}));
  const groupButtons = groupImages.slice(0, 4).map(img => ({isConnected: true, disabled: false, checkVisibility: () => true,
    getAttribute: () => null, closest: () => {elapsed += options.groupBeforeOpenAdvance || 0; return options.groupButtonInForm ? {} : null;},
    querySelectorAll: () => [img], click: () => {viewerClicks++; viewerOpen = true;}}));
  const attachmentContainer = {checkVisibility: () => true,
    querySelectorAll: selector => selector === "button.MessageAttachment" ? options.groupButtonCountMismatch ? groupButtons.slice(1) : groupButtons :
      options.noDOMImage ? [] : options.multipleDOMImages ? Array.from({length: typeof options.multipleDOMImages === "number" ? options.multipleDOMImages : 2}, () => imageNode) : [imageNode]};
  const showImage = {isConnected: true, disabled: false, checkVisibility: () => true, getAttribute: () => null, textContent: "Bild anzeigen",
    closest: () => null,
    click: () => {viewerClicks++; viewerOpen = true;}};
  const overlay = {checkVisibility: () => true, querySelectorAll: () => options.multipleViewerButtons ? [showImage, showImage] : [showImage]};
  const messageNode = {isConnected: true, checkVisibility: () => true, getAttribute: () => options.visibleId || BASE_ID,
    querySelector: selector => selector === (options.groupOutbound ? '[data-testid="OUTBOUND"]' : '[data-testid="INBOUND"]') ? {} : null,
    querySelectorAll: selector => selector === ".ImgFraudProtection" ? options.fraudOverlay ? [overlay] : [] :
      selector === '[data-testid="message-attachment-container"]' && options.archiveImage && !options.fraudOverlay && elapsed >= (options.groupContainerAfter || 0) ? [attachmentContainer] : []};
  const sentMessageNode = {checkVisibility: () => true, getAttribute: () => options.optimisticOnly ? SYSTEM_ID : NEW_ID,
    querySelector: selector => selector === (options.wrongDirection ? '[data-testid="INBOUND"]' : '[data-testid="OUTBOUND"]') ? {} : null,
    querySelectorAll: () => options.multipleContainers ? [attachmentContainer, attachmentContainer] : [attachmentContainer]};
  const systemNode = {checkVisibility: () => true, getAttribute: () => SYSTEM_ID, querySelector: () => null};
  const scope = {checkVisibility: () => !options.noScope && elapsed >= (options.groupViewAfter || 0),
    contains: node => !options.unrelatedComposer && [editor, button].includes(node),
    querySelectorAll: selector => selector === "header" ? options.noHeader ? [] : [header] : selector === "li[data-testid]" ?
      [...(options.noMessages || options.systemOnly ? [] : [messageNode]), ...(options.systemRow || options.systemOnly ? [systemNode] : []),
        ...(options.confirmed ? [sentMessageNode] : [])] : []};
  const viewerCenter = {contains: image => !options.groupMainOutsideCenter && image === imageNode};
  const closeViewer = {isConnected: true, disabled: !!options.viewerCloseDisabled, checkVisibility: () => true, getAttribute: name => name === "aria-label" ? "Schließen" : null,
    click: () => {viewerCloses++; viewerOpen = false;}};
  function thumbnailStyle(active) {
    return {getPropertyValue(name) {return this[name] || "";}, ...(active ? {
      border: options.groupNoInlineBorder ? "" : options.groupCSSVariable ? "2px solid var(--color-primary)" : "2px solid rgb(0, 128, 0)",
      borderWidth: options.groupCSSVariable ? "" : "2px", borderStyle: options.groupCSSVariable ? "" : "solid", boxSizing: "border-box",
    } : {})};
  }
  const thumbnails = groupImages.map((img, index) => ({isConnected: true, checkVisibility: () => !options.groupHiddenThumb,
    style: thumbnailStyle(index === (options.groupActiveIndex || 0) || (options.groupMultipleActive && index === 1)),
    querySelectorAll: () => options.groupThumbDelayed && elapsed < 500 ? [] : options.groupExtraNestedImage ? [img, img] : [img]}));
  const currentThumbnails = () => options.groupReordered && profileReads > 1 ? [...thumbnails].reverse() :
    options.groupCountMismatch ? thumbnails.slice(1) : thumbnails;
  const thumbnailStrip = {isConnected: true, querySelectorAll: selector => selector === ".Attachment--View--Thumbnail" ? currentThumbnails() : groupImages};
  const backdrop = {get isConnected() {return viewerOpen;}, querySelectorAll: selector => selector === ".Modal--Thumbnails" ?
    options.groupNoStrip ? [] : [thumbnailStrip] : [closeViewer]};
  const modal = {get isConnected() {return viewerOpen;}, closest: () => options.groupNoBackdrop ? null : backdrop,
    querySelectorAll: selector => selector === ".Modal--Center--Image" ? [viewerCenter] : selector === 'img[data-testid="attachment-image"]' || selector === "img" ?
      options.noDOMImage ? [] : options.multipleDOMImages ? [imageNode, imageNode] : [imageNode] :
      options.groupCount && ['[data-testid="left-arrow-button-container"]', '[data-testid="right-arrow-button-container"]'].includes(selector) ? options.groupNoArrows ? [] : [{}] : options.viewerArrows ? [{}] : []};
  const replacementModal = {...modal};
  const currentModal = () => options.viewerReplaced && profileReads > 1 ? replacementModal : modal;
  const document = {readyState: "complete", getElementById: () => null,
    createElement: tag => {
      assert.equal(tag, "canvas"); canvasCalls++;
      const canvas = {width: 0, height: 0};
      canvas.getContext = (kind, attributes) => {
        pixelCalls.push({kind, attributes});
        return {
          getContextAttributes: () => ({colorSpace: options.unsupportedColorSpace ? "display-p3" : "srgb"}),
          drawImage: (...args) => {pixelCalls.push({draw_arguments: args.length}); assert.equal(args.length, 3);},
          getImageData: (x, y, width, height, attributes) => {
            pixelCalls.push({read_attributes: attributes});
            return {width, height, colorSpace: options.unsupportedPixelSpace ? "display-p3" : "srgb",
              data: options.unsupportedPixelFormat ? new Float32Array(4) : new Uint8ClampedArray(options.pixelRGBA || RGBA)};
          },
        };
      };
      return canvas;
    },
    querySelector: selector => options.listingMode && selector === "#viewad-contact-form" ? form : null,
    querySelectorAll: selector => selector.startsWith('[data-testid="attachment-modal"],') ? viewerOpen ? [currentModal(), backdrop, ...(options.foreignViewer ? [{}] : [])] : [] :
      selector === '[data-testid="attachment-modal"]' ? viewerOpen ? [currentModal()] : [] :
      selector === "#challenge-form" && options.blockedAfter != null && elapsed >= options.blockedAfter ? [{checkVisibility: () => true}] :
      selector.startsWith("#nachricht") ? [editor] : selector === '[data-testid="conversation-wrapper"]' ? [scope] :
      options.listingMode && selector === ".userprofile-vip, .userprofile" ? [{href: "https://www.kleinanzeigen.de/s-bestandsliste.html?userId=67890",
        matches: value => [".userprofile-vip", "a[href]"].includes(value), querySelectorAll: () => [], checkVisibility: () => true}] : []};
  const baseline = {message_id: BASE_ID, sender_user_id: "67890", direction: "inbound", text: "Eigene Testnachricht", created_at: "2026-01-01T00:00:00.000Z", images: []};
  const imageDTO = (id, index = 0) => ({reference: "opaque-reference", remote_id: options.wrongRemoteId ? "other:0" : id + ":" + index, mime_type: options.missingDtoMime ? undefined : options.dtoMime || "image/jpeg",
    _dom_attachment: !options.invalidAttachment, unavailable: true});
  const newMessage = () => ({message_id: NEW_ID, sender_user_id: options.wrongSender ? "67890" : "12345", direction: "outbound", text: options.imageMode ? "" : options.serverText ?? "Grüße mit Umlauten",
    created_at: new Date((options.clock ? Clock.now() : Date.now()) + 20).toISOString(), images: options.imageMode ? options.multipleDTOImages ? [imageDTO(NEW_ID), imageDTO(NEW_ID)] : [imageDTO(NEW_ID)] : []});
  const reader = {
    async list() {
      listReads++;
      if (options.readerError) throw options.readerError;
      const row = {conversation_id: "c-own", listing_id: options.recoveryListing || "1234500001", recipient_user_id: options.recoveryPeer || "67890"};
      return {account_user_id: "12345", conversations: clicks || options.existingConversation ? options.ambiguousConversation ? [row, {...row, conversation_id: "another"}] : [row] : [],
        has_more: !!options.incompleteInbox, complete: !options.incompleteInbox};
    },
    async get() {
      detailReads++;
      const messages = [baseline];
      if ((clicks || options.confirmed) && !options.noReceipt) messages.push(newMessage());
      if (clicks && options.duplicateReceipt) messages.push({...newMessage(), message_id: "33333333-3333-4333-8333-333333333333"});
      if (options.archiveImage) messages[0] = {...baseline,
        ...(options.groupOutbound ? {sender_user_id: "12345", direction: "outbound"} : {}),
        images: options.groupCount ? Array.from({length: options.groupCount}, (_, index) => imageDTO(BASE_ID, index)) :
          options.multipleDTOImages ? [imageDTO(BASE_ID), imageDTO(BASE_ID)] : [imageDTO(BASE_ID)]};
      return {account_user_id: "12345", conversation_id: "c-own", listing_id: "1234500001", recipient_user_id: options.recipient || "67890", messages,
        has_more: false, complete: true, _view: {attachments_enabled: !options.imagesDisabled,
          messages: [...messages.map(({message_id, direction}) => ({message_id, direction, type: "message"})),
            ...(options.systemRow || options.systemOnly ? [{message_id: SYSTEM_ID, direction: null, type: "unsupported"}] : [])]}};
    },
    async image() {imageReads++; if (options.imageUnavailable) P.fail("image_unavailable"); return {bytes: options.imageMismatch ? ORIGINAL : JPEG, mime_type: "image/jpeg"};}
  };
  const context = vm.createContext({KleinanzeigenProtocol: options.clock ? {...P, assertFresh: cmd => P.assertFresh(cmd, Clock.now())} : P,
    KleinanzeigenMessagingRead: reader, URL, Date: options.clock ? Clock : Date, TextEncoder, TextDecoder, Uint8Array, Uint8ClampedArray, Blob, crypto: webcrypto,
    createImageBitmap: options.noBitmap ? undefined : async (_blob, attributes) => {
      pixelCalls.push({decode_attributes: attributes});
      if (options.decodeGate) await options.decodeGate();
      return {width: options.decodedWidth || options.pixelWidth || 1, height: options.pixelHeight || 1, close: () => {bitmapCloses++;}};
    },
    getComputedStyle: node => {
      if (options.groupComputedError) throw new Error("PRIVATE CSS ERROR");
      const active = node.style.boxSizing === "border-box" || options.groupForeignCSS;
      const computed = {boxSizing: active ? options.groupComputedBoxSizing || "border-box" : "content-box"};
      for (const side of ["Top", "Right", "Bottom", "Left"]) {
        computed[`border${side}Width`] = active ? "2px" : "0px";
        computed[`border${side}Style`] = active ? "solid" : "none";
      }
      if (options.groupComputedOverride) Object.assign(computed, options.groupComputedOverride);
      if (options.groupComputedChanged && profileReads > 1) computed.borderRightWidth = "1px";
      return computed;
    },
    window, document, performance: {timeOrigin: Date.now() - 1000}, location: new URL(options.listingMode ? "https://www.kleinanzeigen.de/s-anzeige/1234500001" : "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=" + (options.urlId || "c-own")),
    HTMLTextAreaElement: Editor, File, Event: class {}, AbortSignal, atob, btoa,
    DataTransfer: class {constructor() {this.files = []; this.items = {add: file => this.files.push(file)};}},
    setTimeout: (callback, ms = 0) => {
      // Local DOM waits are at most 500 ms. A real clock can shorten a nominal
      // five-second deadline to 4999 ms; it must still remain a controlled timer.
      if (ms > 500) {const id = "bounded-" + ++timerSequence; boundedTimers.set(id, {callback, ms}); return id;}
      waits++; elapsed += Math.max(ms, options.timerMinimum || 0); return setImmediate(callback);
    }, clearTimeout: id => {if (typeof id === "string") boundedTimers.delete(id); else clearImmediate(id);},
    fetch: async url => {
      profileReads++;
      if (profileReads === 2 && options.beforeSecondProfile) await options.beforeSecondProfile();
      if (profileReads > 1 && options.viewerSourceChanged) imageNode.src = "data:image/jpeg;base64," + Buffer.from(variantJPEG).toString("base64");
      if (profileReads > 1 && options.viewerMessageDetached) messageNode.isConnected = false;
      if (profileReads > 1 && options.groupSourceChanged) groupImages[1].src = "data:image/jpeg;base64," + Buffer.from([...JPEG, 99]).toString("base64");
      if (profileReads > 1 && options.groupSelectionChanged) {thumbnails[0].style = thumbnailStyle(false); thumbnails[1].style = thumbnailStyle(true);}
      const userId = options.switchAccount && profileReads > 1 ? "99999" : "12345";
      return {status: 200, ok: true, url, headers: {get: () => "application/json"}, body: new ReadableStream({start(c) {c.enqueue(new TextEncoder().encode(JSON.stringify({userId}))); c.close();}})};
    },
    browser: {runtime: {id: "extension", onMessage: {addListener: fn => {listener = fn;}},
      async sendMessage(message) {
        if (message.type === "image_preview") {
          previewMessages.push(message); previewClicks.push(clicks);
          if (options.previewGate) await options.previewGate();
          return options.previewRejected ? {error: {code: "image_unavailable"}} : {result: {accepted: true}};
        }
        if (message.type === "image_chunk") {chunkReads++; return {result: {offset: 0, total_bytes: ORIGINAL.length, ...image,
          sha256: options.badChunkHash ? "a".repeat(64) : image.sha256, data_base64: Buffer.from(ORIGINAL).toString("base64")}};}
        if (message.type === "archive_image_chunk") {
          archiveChunks.push(message);
          return options.archiveError ? {error: {code: "image_unavailable", message: "PRIVATE TRANSFER ERROR"}} :
            {result: {accepted: true, next_offset: message.offset + Buffer.from(message.data_base64, "base64").length}};
        }
        throw new Error("Unexpected operation");
      }}}
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8"), context);
  return {run: async cmd => {
    const marker = await listener({type: "kleinanzeigen-document-marker"}, {id: "extension"});
    return listener({type: "kleinanzeigen-command", command: {...cmd, fresh_navigation: options.noFreshNavigation ? null :
      {document_id: options.staleDocument ? "old-document" : marker.document_id, path: new URL(marker.url).pathname}}}, {id: "extension"});
  },
    state: () => ({clicks, selections, profileReads, detailReads, imageReads, chunkReads, listReads}), waiting: () => ({elapsed, waits}),
    archived: () => archiveChunks,
    advance: ms => {elapsed += ms;}, pixels: () => ({pixelCalls, previewMessages, previewClicks, canvasCalls, bitmapCloses}),
    viewer: () => ({viewerClicks, viewerCloses, viewerOpen}),
    expireImageTask: () => {const [id, timer] = boundedTimers.entries().next().value; boundedTimers.delete(id); elapsed += timer.ms; timer.callback();}};
}
test("chat navigation contains only the validated conversation ID on the account inbox", () => {
  for (const cmd of [command(), command("prepare_send", target), command("get_conversation", {conversation_id: "c-own"})])
    assert.equal(P.targetURL(cmd), "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=c-own");
  assert.throws(() => P.targetURL(command("get_conversation", {conversation_id: "c&other=value"})), e => e.code === "invalid_target");
});

test("content forwards authored reader shapes without the original error message", async () => {
  const diagnostic = {operation: "list", stage: "list_unread", reason: "item_unread", item_types: {unread: "missing"}};
  const readerError = Object.assign(new Error("PRIVATE BODY AND AUTH"), {code: "ui_unsupported", reader_structure: diagnostic, raw: "PRIVATE"});
  const fixtureWithError = fixture({readerError});
  const reply = await fixtureWithError.run(command("list_conversations", {limit: 10}));
  assert.equal(reply.error.code, "ui_unsupported");
  assert.deepEqual(JSON.parse(JSON.stringify(reply.error.reader_structure)), diagnostic);
  assert.equal(reply.error.content_revision, JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8")).version);
  assert.doesNotMatch(JSON.stringify(reply), /PRIVATE/);
  assert.equal(fixtureWithError.state().clicks, 0);
});
test("a fresh server message confirms one UI text click, never an optimistic DOM UUID", async () => {
  const f = fixture(), result = await f.run(command());
  assert.equal(result.result.message_id, NEW_ID);
  assert.equal(result.result.confirmed, true);
  assert.equal(f.state().clicks, 1);
  assert.equal(f.state().profileReads, 2);
  assert.equal(f.state().detailReads, 2);
});
test("server CRLF receipts match submitted LF text without broad text normalization or repeated sends", async () => {
  const submitted = "Grüße mit Umlauten\nZweite Zeile";
  for (const listingMode of [false, true]) {
    const f = fixture({listingMode, serverText: submitted.replace(/\n/g, "\r\n")});
    const reply = await f.run(command("send_step", {text: submitted, ...(listingMode ? {target: listingTarget} : {})}));
    assert.equal(listingMode ? reply.result._follow.message_id : reply.result.message_id, NEW_ID);
    assert.equal(f.state().clicks, 1);
    for (const serverText of [submitted + " ", submitted.replace(/\n/g, "\r"), submitted.replace("Grüße", "Gruesse")]) {
      const mismatch = fixture({listingMode, serverText});
      const refused = await mismatch.run(command("send_step", {text: submitted, ...(listingMode ? {target: listingTarget} : {})}));
      assert.equal(refused.error.code, "outcome_unknown");
      assert.equal(mismatch.state().clicks, 1);
    }
  }
});
test("URL, header listing and real visible server messages must all identify the selected chat", async () => {
  for (const options of [{urlId: "another"}, {headerListing: "9999999999"}, {visibleId: NEW_ID}, {noMessages: true}, {recipient: "99999"}]) {
    const f = fixture(options), result = await f.run(command());
    assert.ok(result.error);
    assert.equal(f.state().clicks, 0);
    assert.equal(f.state().selections, 0);
  }
});
test("existing text or a React attachment draft is preserved without any click or selection", async () => {
  for (const options of [{existingText: "Unfertiger eigener Entwurf"}, {existingPreview: true}]) {
    const f = fixture(options), result = await f.run(command());
    assert.equal(result.error.code, "draft_present");
    assert.equal(f.state().clicks, 0);
    assert.equal(f.state().selections, 0);
  }
});
test("known server system rows are skipped but cannot establish the conversation by themselves", async () => {
  const f = fixture({systemRow: true}), response = await f.run(command());
  assert.equal(response.result.message_id, NEW_ID);
  assert.equal(f.state().clicks, 1);
  const onlySystem = fixture({systemOnly: true}), refused = await onlySystem.run(command());
  assert.equal(refused.error.code, "conversation_unverified");
  assert.equal(onlySystem.state().clicks, 0);
});
test("session changes after preparing a text or image prevent the send click", async () => {
  for (const imageMode of [false, true]) {
    const f = fixture({switchAccount: true, imageMode});
    const result = await f.run(command("send_step", imageMode ? {kind: "image", image, text: null} : {}));
    assert.equal(result.error.code, "account_mismatch");
    assert.equal(f.state().clicks, 0);
    assert.equal(f.state().selections, imageMode ? 1 : 0);
  }
});
test("an expired command returning from the final identity request never clicks send", async () => {
  let reportEntered, releaseProfile;
  const entered = new Promise(resolve => {reportEntered = resolve;});
  const release = new Promise(resolve => {releaseProfile = resolve;});
  const f = fixture({clock: true, beforeSecondProfile: async () => {reportEntered(); await release;}});
  const pending = f.run(command());
  await entered;
  assert.equal(f.state().clicks, 0);
  assert.equal(f.state().profileReads, 2);
  f.advance(31000);
  releaseProfile();
  const response = await pending;
  assert.equal(response.error.code, "command_expired");
  assert.equal(f.state().clicks, 0);
  assert.equal(f.state().detailReads, 1);
});
test("one image click yields only a server-ID follow hint and the transformed JPEG hash", async () => {
  const f = fixture({imageMode: true}), result = await f.run(command("send_step", {kind: "image", image, text: null}));
  assert.equal(result.result._follow.message_id, NEW_ID);
  assert.equal(result.result._follow.kind, "image");
  assert.equal(result.result._follow.image_sha256, hash(JPEG));
  assert.notEqual(result.result._follow.image_sha256, image.sha256);
  assert.deepEqual(JSON.parse(JSON.stringify(result.result._follow.image_pixels)), pixelProof);
  assert.deepEqual(f.pixels().previewClicks, [0]);
  assert.equal(f.pixels().previewMessages[0].preview_sha256, hash(JPEG));
  assert.deepEqual(JSON.parse(JSON.stringify(f.pixels().previewMessages[0].image_pixels)), pixelProof);
  assert.equal(result.result.confirmed, undefined);
  assert.equal(result.result.message_id, undefined);
  assert.deepEqual({...f.state()}, {clicks: 1, selections: 1, profileReads: 2, detailReads: 2, imageReads: 0, chunkReads: 1, listReads: 0});
});
test("unverified file inputs, unavailable previews and changed upload bytes never click send", async () => {
  for (const options of [{noFileInput: true}, {imagesDisabled: true}, {noPreview: true}, {extraPreview: true}, {badChunkHash: true}]) {
    const f = fixture({...options, imageMode: true}), result = await f.run(command("send_step", {kind: "image", image, text: null}));
    assert.ok(result.error);
    assert.equal(f.state().clicks, 0);
    assert.ok(f.state().selections <= 1);
  }
});
test("unclear image server metadata never causes a second selection or send", async () => {
  for (const options of [{noReceipt: true}, {duplicateReceipt: true}]) {
    const f = fixture({...options, imageMode: true}), result = await f.run(command("send_step", {kind: "image", image, text: null}));
    assert.ok(result.error);
    assert.equal(f.state().clicks, 1);
    assert.equal(f.state().selections, 1);
    assert.ok(f.state().detailReads <= 3);
  }
});
test("private API references and the full UI snapshot never leave the content script", async () => {
  const f = fixture({archiveImage: true}), result = await f.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.equal(result.result.messages[0].images[0].unavailable, undefined);
  assert.ok(result.result.messages[0].images[0].transfer_id);
  assert.doesNotMatch(JSON.stringify(result), /opaque-reference|_view|_dom_attachment|attachments_enabled/);
  assert.equal(f.state().clicks, 0);
  assert.equal(f.state().imageReads, 0);
});
test("verified chat image capabilities report the observed limit and explicit submit behavior", async () => {
  const f = fixture(), result = await f.run(command("prepare_send", target));
  assert.equal(result.result.capabilities.can_send_images, true);
  assert.equal(result.result.capabilities.max_images_per_message, 5);
  assert.equal(result.result.capabilities.image_limit_source, "website");
  assert.equal(result.result.capabilities.image_send_mode, "explicit_submit");
  assert.equal(f.state().clicks, 0);
  const disabled = fixture({imagesDisabled: true});
  const disabledResult = await disabled.run(command("prepare_send", target));
  assert.equal(disabledResult.result.capabilities.images_status, "unsupported");
  assert.equal(disabledResult.result.capabilities.can_send_images, false);
});
test("conversation reads wait only on bounded local composer hydration before claiming capabilities", async () => {
  const f = fixture({clock: true, hydrateAfter: 250, fileAfter: 500});
  const response = await f.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.equal(response.result.capabilities.can_send_text, true);
  assert.equal(response.result.capabilities.can_send_images, true);
  assert.equal(response.result.capabilities.max_images_per_message, 5);
  assert.equal(f.waiting().elapsed, 500);
  assert.deepEqual(f.state(), {clicks: 0, selections: 0, profileReads: 1, detailReads: 1, imageReads: 0, chunkReads: 0, listReads: 0});
  const disabled = fixture({clock: true, imagesDisabled: true, noFileInput: true});
  const disabledReply = await disabled.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.equal(disabledReply.result.capabilities.images_status, "unsupported");
  assert.equal(disabled.waiting().waits, 0);
});
test("missing or unrelated chat UI preserves readable server messages with unverified capabilities", async () => {
  for (const options of [{noComposer: true}, {noHeader: true}, {unrelatedComposer: true}, {urlId: "another"}, {headerListing: "9999999999"}, {visibleId: NEW_ID}]) {
    const f = fixture({clock: true, ...options});
    const response = await f.run(command("get_conversation", {conversation_id: "c-own"}));
    assert.equal(response.result.messages[0].text, "Eigene Testnachricht");
    assert.equal(response.result.capabilities.text_status, "unverified");
    assert.equal(response.result.capabilities.images_status, "unverified");
    assert.equal(response.result.capabilities.can_send_images, false);
    assert.equal(f.waiting().elapsed, 5000);
    assert.equal(f.waiting().waits, 40);
    assert.equal(f.state().detailReads, 1);
    assert.equal(f.state().clicks, 0);
  }
  const noFile = fixture({clock: true, noFileInput: true});
  const reply = await noFile.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.equal(reply.result.capabilities.text_status, "supported");
  assert.equal(reply.result.capabilities.images_status, "unverified");
  assert.equal(noFile.waiting().elapsed, 5000);
});
test("composer readiness stops on command expiry or a visible block without additional requests", async () => {
  const expired = fixture({clock: true, noComposer: true});
  const expiringCommand = command("get_conversation", {conversation_id: "c-own"});
  expiringCommand.expires_at = new Date(Date.now() + 250).toISOString();
  assert.equal((await expired.run(expiringCommand)).error.code, "command_expired");
  assert.ok(expired.waiting().elapsed <= 251);
  const blocked = fixture({clock: true, noComposer: true, blockedAfter: 250});
  assert.equal((await blocked.run(command("get_conversation", {conversation_id: "c-own"}))).error.code, "upstream_blocked");
  assert.equal(blocked.waiting().elapsed, 250);
  const blockedAtDeadline = fixture({clock: true, noComposer: true, blockedAfter: 5000});
  assert.equal((await blockedAtDeadline.run(command("get_conversation", {conversation_id: "c-own"}))).error.code, "upstream_blocked");
  for (const f of [expired, blocked, blockedAtDeadline]) {
    assert.equal(f.state().detailReads, 1);
    assert.equal(f.state().profileReads, 1);
    assert.equal(f.state().clicks, 0);
  }
});
test("throttled local timers honor wallclock limits instead of repeating every nominal interval", async () => {
  const chat = fixture({clock: true, timerMinimum: 2000, headerListing: "9999999999"});
  assert.equal((await chat.run(command("prepare_send", target))).error.code, "conversation_unverified");
  assert.equal(chat.waiting().elapsed, 10000);
  assert.equal(chat.waiting().waits, 5);
  const preview = fixture({clock: true, timerMinimum: 2000, imageMode: true, noPreview: true});
  assert.equal((await preview.run(command("send_step", {kind: "image", image, text: null}))).error.code, "composer_not_ready");
  assert.equal(preview.waiting().elapsed, 6000); // First wake-up after the5s deadline.
  assert.equal(preview.waiting().waits, 3);
  assert.equal(preview.state().selections, 1);
  for (const f of [chat, preview]) assert.equal(f.state().clicks, 0);
  const capabilities = fixture({clock: true, timerMinimum: 2000, noComposer: true});
  const reply = await capabilities.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.equal(reply.result.capabilities.text_status, "unverified");
  assert.equal(capabilities.waiting().elapsed, 6000);
  assert.equal(capabilities.waiting().waits, 3);
  assert.equal(capabilities.state().detailReads, 1);
});
test("throttled timers after one send proceed to bounded read-only receipts without forty delayed ticks", async () => {
  for (const listingMode of [false, true]) {
    const f = fixture({clock: true, timerMinimum: 2000, keepDraft: true, listingMode});
    const response = await f.run(command("send_step", listingMode ? {target: listingTarget} : {}));
    assert.equal(listingMode ? response.result._follow.message_id : response.result.message_id, NEW_ID);
    assert.equal(f.state().clicks, 1);
    assert.equal(f.waiting().waits, 5); // One pre-click tick plus four post-click ticks.
    assert.equal(f.waiting().elapsed, 10000);
    assert.equal(f.state().detailReads, listingMode ? 1 : 2);
  }
});
const listingTarget = {target_kind: "listing", target_id: "1234500001", listing_id: "1234500001", account_user_id: "12345", recipient_user_id: "67890"};
test("first contact without a success link yields only a server-backed durable follow hint", async () => {
  for (const existingConversation of [false, true]) {
    const f = fixture({listingMode: true, noFileInput: true, existingConversation});
    const response = await f.run(command("send_step", {target: listingTarget}));
    assert.equal(response.result._follow.conversation_id, "c-own");
    assert.equal(response.result._follow.message_id, NEW_ID);
    assert.equal(response.result.confirmed, undefined);
    assert.equal(response.result.message_id, undefined);
    assert.equal(f.state().clicks, 1);
    assert.equal(f.state().listReads, 2);
    assert.equal(f.state().detailReads, existingConversation ? 2 : 1);
  }
});
test("first-contact recovery never guesses from partial inboxes, another listing or a missing unique message", async () => {
  for (const options of [{incompleteInbox: true}, {recoveryListing: "9999999999"}, {recoveryPeer: "99999"}, {noReceipt: true}, {duplicateReceipt: true}, {ambiguousConversation: true}]) {
    const f = fixture({...options, listingMode: true, noFileInput: true});
    const response = await f.run(command("send_step", {target: listingTarget}));
    assert.equal(response.error.code, "outcome_unknown");
    assert.equal(f.state().clicks, 1);
    assert.ok(f.state().listReads <= 2);
    assert.equal(f.state().selections, 0);
  }
});
test("first-contact file sending is rejected before selecting or clicking anything", async () => {
  const f = fixture({listingMode: true, noFileInput: true});
  const response = await f.run(command("send_step", {target: listingTarget, kind: "image", image}));
  assert.equal(response.error.code, "images_unsupported");
  assert.equal(f.state().clicks, 0);
  assert.equal(f.state().selections, 0);
});
function imageConfirmation(changes = {}) {
  return command("confirm_send", {kind: "image", target, conversation_id: "c-own", expected_message_id: NEW_ID,
    text: "", baseline_message_ids: [BASE_ID], not_before: new Date(Date.now() - 1000).toISOString(), expected_image_sha256: hash(JPEG), ...changes});
}
test("a fresh server-bound DOM JPEG confirms the persisted preview hash without another click or gateway fetch", async () => {
  const f = fixture({confirmed: true, imageMode: true});
  const response = await f.run(imageConfirmation());
  assert.equal(response.result.confirmed, true);
  assert.equal(response.result.kind, "image");
  assert.equal(response.result.message_id, NEW_ID);
  assert.equal(response.result.image_evidence.source, "fresh_conversation_dom");
  assert.equal(response.result.image_evidence.sha256, hash(JPEG));
  assert.notEqual(response.result.image_evidence.sha256, image.sha256);
  assert.equal(f.state().clicks, 0);
  assert.equal(f.state().selections, 0);
  assert.equal(f.state().imageReads, 0);
  assert.equal(f.state().chunkReads, 0);
  const unknownFormat = fixture({confirmed: true, imageMode: true, missingDtoMime: true});
  assert.equal((await unknownFormat.run(imageConfirmation())).result.image_evidence.sha256, hash(JPEG));
});
test("image confirmation requires its exact conversation, existing server ID and transformed hash", () => {
  assert.equal(P.targetURL(imageConfirmation()), "https://www.kleinanzeigen.de/m-nachrichten.html?conversationId=c-own");
  for (const changes of [{expected_message_id: null}, {expected_image_sha256: "not-a-hash"}, {conversation_id: "other"}, {text: "Extra text"},
    {target: listingTarget}, {target: {...target, recipient_user_id: "12345"}}])
    assert.throws(() => P.validateConfirmation(imageConfirmation(changes)), error => error.code === "invalid_command");
  assert.throws(() => P.confirmationReceipt({}, imageConfirmation()), error => error.code === "invalid_command");
});
test("pixel proofs have one fixed algorithm and exact bounded dimensions and cannot be supplied to text confirmations", () => {
  assert.equal(P.validImagePixels(pixelProof), true);
  assert.equal(P.validImagePixels({...pixelProof, width: 2000, height: 2000}), true);
  for (const change of [{algorithm: "near-pixels"}, {width: true}, {width: 1.5}, {height: 0}, {width: 4000001}, {width: 2001, height: 2000}, {sha256: "private"}, {extra: true}]) {
    assert.equal(P.validImagePixels({...pixelProof, ...change}), false);
    assert.throws(() => P.validateConfirmation(imageConfirmation({expected_image_pixels: {...pixelProof, ...change}})), error => error.code === "invalid_command");
  }
  assert.equal(P.imagePixelsEqual(pixelProof, {...pixelProof, height: 2}), false);
  assert.equal(P.imagePixelsEqual(pixelProof, {...pixelProof, sha256: "a".repeat(64)}), false);
  assert.throws(() => P.validateConfirmation(command("confirm_send", {target: listingTarget, conversation_id: "c-own", expected_message_id: NEW_ID,
    not_before: new Date().toISOString(), baseline_message_ids: [], expected_image_pixels: pixelProof})), error => error.code === "invalid_command");
});
test("byte receipts remain preferred while a different encoded JPEG needs exact previously bound full RGBA pixels", async () => {
  const equalBytes = fixture({confirmed: true, imageMode: true, noBitmap: true});
  const byteReceipt = await equalBytes.run(imageConfirmation({expected_image_pixels: pixelProof}));
  assert.equal(byteReceipt.result.image_evidence.sha256, hash(JPEG));
  assert.equal(byteReceipt.result.image_evidence.comparison, undefined);
  assert.equal(equalBytes.pixels().canvasCalls, 0);
  const options = {confirmed: true, imageMode: true, imageSource: "data:image/jpeg;base64," + Buffer.from(variantJPEG).toString("base64")};
  const f = fixture(options);
  const response = await f.run(imageConfirmation({expected_image_pixels: pixelProof}));
  assert.equal(response.result.confirmed, true);
  assert.equal(response.result.image_evidence.comparison, "exact_pixels");
  assert.equal(response.result.image_evidence.sha256, hash(variantJPEG));
  assert.notEqual(response.result.image_evidence.sha256, hash(JPEG));
  assert.deepEqual(JSON.parse(JSON.stringify(response.result.image_evidence.pixels)), pixelProof);
  assert.equal(f.pixels().pixelCalls.find(call => call.draw_arguments).draw_arguments, 3);
  assert.deepEqual(JSON.parse(JSON.stringify(f.pixels().pixelCalls.find(call => call.read_attributes).read_attributes)), {colorSpace: "srgb", pixelFormat: "rgba-unorm8"});
  const legacy = fixture(options);
  assert.equal((await legacy.run(imageConfirmation())).error.code, "outcome_unknown");
  assert.equal(legacy.pixels().canvasCalls, 0);
  for (const item of [equalBytes, f, legacy]) {assert.equal(item.state().clicks, 0); assert.equal(item.state().imageReads, 0);}
});
test("one changed pixel component dimensions unsupported color space or non-RGBA8 never confirms", async () => {
  for (const options of [{pixelRGBA: [48, 160, 96, 255]}, {pixelRGBA: [47, 160, 96, 254]}, {pixelWidth: 2}, {unsupportedColorSpace: true},
    {unsupportedPixelSpace: true}, {unsupportedPixelFormat: true}, {decodedWidth: 2}, {noBitmap: true}]) {
    const f = fixture({...options, confirmed: true, imageMode: true, imageSource: "data:image/jpeg;base64," + Buffer.from(variantJPEG).toString("base64")});
    const response = await f.run(imageConfirmation({expected_image_pixels: pixelProof}));
    assert.equal(response.error.code, "outcome_unknown");
    assert.ok(["pixel_compare", "pixel_decode"].includes(response.error.image_structure.stage));
    assert.equal(f.state().clicks, 0);
    assert.equal(f.state().imageReads, 0);
  }
});
test("preview proof limits unsupported decoding and failed durable acknowledgement prevent the sole send click", async () => {
  for (const options of [{pixelWidth: 4000001}, {unsupportedColorSpace: true}, {unsupportedPixelFormat: true}, {noBitmap: true}, {previewRejected: true}]) {
    const f = fixture({...options, imageMode: true});
    const response = await f.run(command("send_step", {kind: "image", image, text: null}));
    assert.equal(response.error.code, "image_unavailable");
    assert.equal(f.state().selections, 1);
    assert.equal(f.state().clicks, 0);
    if (options.pixelWidth) assert.equal(f.pixels().canvasCalls, 0);
  }
});
test("late decode or preview acknowledgement cannot resume a failed image send", async () => {
  for (const stage of ["decode", "preview"]) {
    let enter, release;
    const entered = new Promise(resolve => {enter = resolve;});
    const gate = new Promise(resolve => {release = resolve;});
    const f = fixture({clock: true, imageMode: true, [stage + "Gate"]: async () => {enter(); await gate;}});
    const pending = f.run(command("send_step", {kind: "image", image, text: null}));
    await entered;
    assert.equal(f.state().clicks, 0);
    f.expireImageTask();
    const response = await pending;
    assert.equal(response.error.code, "image_unavailable");
    release(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.state().clicks, 0);
    assert.equal(f.state().selections, 1);
    if (stage === "decode") assert.equal(f.pixels().bitmapCloses, 1);
  }
});
test("stale documents, optimistic IDs, wrong senders and ambiguous attachments never prove an image", async () => {
  for (const options of [{noFreshNavigation: true}, {staleDocument: true}, {optimisticOnly: true}, {wrongDirection: true}, {wrongSender: true},
    {multipleDTOImages: true}, {multipleContainers: true}, {multipleDOMImages: true}, {wrongRemoteId: true}, {noDOMImage: true},
    {dtoMime: "image/png"}, {invalidAttachment: true}, {hiddenImage: true}, {imageUnavailable: true}, {imageMismatch: true},
    {imageSource: "https://gateway.kleinanzeigen.de/unproven"}, {imageSource: "data:image/svg+xml;base64,PHN2Zy8+"}]) {
    const f = fixture({...options, confirmed: true, imageMode: true});
    const response = await f.run(imageConfirmation());
    assert.equal(response.error?.code, "outcome_unknown", JSON.stringify(options));
    assert.equal(f.state().clicks, 0);
    assert.equal(f.state().selections, 0);
    assert.equal(f.state().imageReads, 0);
    assert.ok(f.state().detailReads <= 2);
  }
  const wrongHash = fixture({confirmed: true, imageMode: true});
  assert.equal((await wrongHash.run(imageConfirmation({expected_image_sha256: image.sha256}))).error.code, "outcome_unknown");
});
test("archive images stay unavailable if DOM freshness or exact single-attachment binding is absent", async () => {
  for (const options of [{noFreshNavigation: true}, {staleDocument: true}, {multipleDTOImages: true}, {multipleDOMImages: true},
    {wrongRemoteId: true}, {invalidAttachment: true}, {noDOMImage: true}, {hiddenImage: true}, {imageSource: "data:text/html;base64,PGgxLz4="}]) {
    const f = fixture({...options, archiveImage: true});
    const response = await f.run(command("get_conversation", {conversation_id: "c-own"}));
    assert.ok(response.result.messages[0].images.every(image => image.unavailable === true && !image.transfer_id));
    assert.equal(response.result.messages[0].text, "Eigene Testnachricht");
    assert.equal(f.state().imageReads, 0);
    assert.equal(f.state().clicks, 0);
  }
});
test("unavailable image diagnostics retain only the precise failed guard and fixed bounded shapes", async () => {
  const webp = Buffer.from("RIFF0000WEBP", "ascii").toString("base64");
  const cases = [
    [{staleDocument: true}, "fresh_document", {fresh_document: false}],
    [{invalidAttachment: true}, "attachment_descriptor", {descriptor_present: false}],
    [{wrongRemoteId: true}, "attachment_descriptor", {remote_id_matches: false}],
    [{multipleDTOImages: true}, "attachment_descriptor", {attachment_count: 2}],
    [{noHeader: true}, "message_binding", {view_verified: false}],
    [{noDOMImage: true}, "image_node", {image_count: 0}],
    [{multipleDOMImages: 7}, "image_node", {image_count: 2}],
    [{hiddenImage: true}, "image_ready", {visible: false}],
    [{imageUnavailable: true}, "image_ready", {complete: false}],
    [{imageSource: "https://private.example/image?token=SECRET"}, "source_encoding", {source_kind: "https", data_mime: "missing"}],
    [{imageSource: "data:image/webp;base64," + webp}, "mime_match", {data_mime: "webp", expected_mime: "jpeg"}],
    [{imageSource: "data:image/jpeg;base64,PRIVATE!!!"}, "base64", {data_mime: "jpeg"}],
    [{imageMismatch: true}, "magic_bytes", {magic_type: "png"}],
    [{archiveError: true}, "archive_transfer", {}],
  ];
  const allowed = new Set(["stage", "fresh_document", "descriptor_present", "remote_id_matches", "sender_matches", "direction_matches", "view_verified",
    "visible", "complete", "dimensions_valid", "within_pixel_limit", "preview_digest_matches", "readiness_timeout", "attachment_count", "message_count",
    "container_count", "image_count", "source_kind", "data_mime", "expected_mime", "magic_type"]);
  for (const [options, stage, expected] of cases) {
    const f = fixture({...options, archiveImage: true, clock: true});
    const reply = await f.run(command("get_conversation", {conversation_id: "c-own"}));
    const archived = reply.result.messages[0].images[0];
    assert.equal(archived.unavailable, true);
    assert.equal(archived.error.code, "image_unavailable");
    const diagnostic = archived.error.image_structure;
    assert.equal(diagnostic.stage, stage, JSON.stringify(options));
    for (const [key, value] of Object.entries(expected)) assert.equal(diagnostic[key], value);
    assert.ok(Object.keys(diagnostic).every(key => allowed.has(key)));
    assert.ok(Object.entries(diagnostic).filter(([key]) => key.endsWith("_count")).every(([, value]) => Number.isInteger(value) && value >= 0 && value <= 2));
    assert.doesNotMatch(JSON.stringify(diagnostic), /PRIVATE|SECRET|https:|;base64,|22222222|Eigene Testnachricht|12345|67890|c-own|11111111|sha256|[a-f0-9]{64}/);
    assert.equal(reply.result.messages[0].text, "Eigene Testnachricht");
    assert.equal(f.state().clicks, 0);
    assert.equal(f.state().selections, 0);
    assert.equal(f.state().imageReads, 0);
    if (!["fresh_document", "archive_transfer"].includes(stage) && !options.multipleDTOImages) assert.equal(diagnostic.readiness_timeout, true);
  }
});
test("image confirmation reports MIME, metadata or digest failures without exposing bytes hashes or repeating send", async () => {
  const webp = Buffer.from("RIFF0000WEBP", "ascii").toString("base64");
  const cases = [
    [{staleDocument: true}, {}, "fresh_document"],
    [{noReceipt: true}, {}, "receipt_metadata"],
    [{noDOMImage: true}, {}, "image_node"],
    [{imageSource: "data:image/webp;base64," + webp}, {}, "mime_match"],
    [{imageSource: "data:image/webp;base64," + webp, missingDtoMime: true}, {}, "preview_digest"],
    [{}, {expected_image_sha256: image.sha256}, "preview_digest"],
  ];
  for (const [options, args, stage] of cases) {
    const f = fixture({...options, confirmed: true, imageMode: true});
    const reply = await f.run(imageConfirmation(args));
    assert.equal(reply.error.code, "outcome_unknown");
    assert.equal(reply.error.image_structure.stage, stage);
    if (stage === "preview_digest") assert.equal(reply.error.image_structure.preview_digest_matches, false);
    assert.doesNotMatch(JSON.stringify(reply.error.image_structure), /https:|base64|11111111|22222222|c-own|12345|67890|sha256|[a-f0-9]{64}/);
    assert.equal(f.state().clicks, 0);
    assert.equal(f.state().selections, 0);
    assert.equal(f.state().imageReads, 0);
  }
});
test("the exact inbound server message opens and closes only its newly created single-attachment viewer", async () => {
  const f = fixture({archiveImage: true, fraudOverlay: true});
  const reply = await f.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.ok(reply.result.messages[0].images[0].transfer_id);
  assert.equal(reply.result.messages[0].images[0].unavailable, undefined);
  assert.deepEqual(f.viewer(), {viewerClicks: 1, viewerCloses: 1, viewerOpen: false});
  assert.equal(f.state().clicks, 0);
  assert.equal(f.state().imageReads, 0);
  assert.equal(f.state().profileReads, 2); // Recheck current account after the viewer loads.
});
test("existing or ambiguous viewers never trigger a display click", async () => {
  for (const options of [{existingViewer: true}, {multipleViewerButtons: true}, {visibleId: NEW_ID}, {noFreshNavigation: true}]) {
    const f = fixture({...options, archiveImage: true, fraudOverlay: true});
    const reply = await f.run(command("get_conversation", {conversation_id: "c-own"}));
    assert.equal(reply.result.messages[0].images[0].unavailable, true);
    assert.equal(f.viewer().viewerClicks, 0);
    assert.equal(f.viewer().viewerCloses, 0);
    assert.equal(f.state().clicks, 0);
  }
});
test("changed modal image message or account fails closed after one display click without retry", async () => {
  for (const options of [{viewerReplaced: true}, {viewerSourceChanged: true}, {viewerMessageDetached: true}, {foreignViewer: true},
    {multipleDOMImages: true}, {viewerArrows: true}, {noDOMImage: true}, {switchAccount: true}]) {
    const f = fixture({...options, archiveImage: true, fraudOverlay: true, clock: true});
    const reply = await f.run(command("get_conversation", {conversation_id: "c-own"}));
    if (options.switchAccount) assert.equal(reply.error.code, "account_mismatch");
    else assert.equal(reply.result.messages[0].images[0].unavailable, true);
    assert.equal(f.viewer().viewerClicks, 1);
    assert.equal(f.viewer().viewerCloses, 0);
    assert.equal(f.state().clicks, 0);
    assert.equal(f.state().imageReads, 0);
  }
});
test("read-only groups archive all original gallery bytes in the server-proven order including the fifth image", async () => {
  for (const groupCount of [2, 4, 5]) {
    for (const variant of [{}, {fraudOverlay: true}, {groupOutbound: true}]) {
      const f = fixture({archiveImage: true, groupCount, ...variant});
      const reply = await f.run(command("get_conversation", {conversation_id: "c-own"}));
      const images = reply.result.messages[0].images;
      assert.equal(images.length, groupCount);
      assert.ok(images.every(image => image.transfer_id && !image.unavailable));
      assert.deepEqual(images.map(image => image.remote_id), Array.from({length: groupCount}, (_, index) => `${BASE_ID}:${index}`));
      assert.deepEqual(f.archived().map(chunk => [...Buffer.from(chunk.data_base64, "base64")]),
        Array.from({length: groupCount}, (_, index) => index === 0 ? [...JPEG] : [...JPEG, index]));
      assert.deepEqual(f.viewer(), {viewerClicks: 1, viewerCloses: 1, viewerOpen: false});
      assert.equal(f.state().clicks, 0); assert.equal(f.state().selections, 0); assert.equal(f.state().imageReads, 0);
    }
  }
});
test("group reading waits for the exact chat and attachment DOM before its one opening click", async () => {
  for (const options of [{groupViewAfter: 750}, {groupContainerAfter: 750}, {groupViewAfter: 500, groupContainerAfter: 1500}]) {
    const f = fixture({archiveImage: true, groupCount: 4, clock: true, ...options});
    const reply = await f.run(command("get_conversation", {conversation_id: "c-own"}));
    assert.ok(reply.result.messages[0].images.every(image => image.transfer_id && !image.unavailable));
    assert.ok(f.waiting().elapsed >= Math.max(options.groupViewAfter || 0, options.groupContainerAfter || 0));
    assert.deepEqual(f.viewer(), {viewerClicks: 1, viewerCloses: 1, viewerOpen: false});
    assert.equal(f.state().clicks, 0); assert.equal(f.state().imageReads, 0);
    assert.equal(f.state().detailReads, 1); // No extra website request while waiting for React.
  }
});
test("group DOM readiness and gallery loading share one local deadline without retry clicks", async () => {
  const absent = fixture({archiveImage: true, groupCount: 4, groupViewAfter: 10000, clock: true});
  const missing = await absent.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.ok(missing.result.messages[0].images.every(image => image.unavailable && !image.transfer_id));
  assert.equal(absent.viewer().viewerClicks, 0);
  assert.ok(absent.waiting().elapsed <= 10000);
  const late = fixture({archiveImage: true, groupCount: 4, groupViewAfter: 7500, fraudOverlay: true, noDOMImage: true, clock: true});
  const unavailable = await late.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.ok(unavailable.result.messages[0].images.every(image => image.unavailable && !image.transfer_id));
  assert.equal(late.viewer().viewerClicks, 1);
  assert.equal(late.archived().length, 0);
  assert.ok(late.waiting().elapsed >= 7500 && late.waiting().elapsed <= 10000);
  assert.equal(late.state().detailReads, 1);
});
test("group binding failures prevent any read click or archive transfer", async () => {
  for (const options of [{existingViewer: true}, {staleDocument: true}, {wrongRemoteId: true}, {invalidAttachment: true},
    {visibleId: NEW_ID}, {groupButtonInForm: true}, {groupButtonCountMismatch: true}]) {
    const f = fixture({archiveImage: true, groupCount: 4, ...options});
    const reply = await f.run(command("get_conversation", {conversation_id: "c-own"}));
    assert.ok(reply.result.messages[0].images.every(image => image.unavailable && !image.transfer_id));
    assert.equal(f.viewer().viewerClicks, 0, JSON.stringify(options));
    assert.equal(f.archived().length, 0);
    assert.equal(f.state().clicks, 0);
  }
});
test("ambiguous, incomplete or changed galleries never attach bytes to server image slots", async () => {
  for (const options of [{groupCountMismatch: true}, {groupNoStrip: true}, {groupNoArrows: true}, {groupActiveIndex: 1},
    {groupExtraNestedImage: true}, {groupReordered: true}, {groupSourceChanged: true}, {groupSelectionChanged: true},
    {viewerReplaced: true}, {viewerMessageDetached: true}, {viewerSourceChanged: true}, {foreignViewer: true}, {noDOMImage: true}]) {
    const f = fixture({archiveImage: true, groupCount: 5, fraudOverlay: true, clock: true, ...options});
    const reply = await f.run(command("get_conversation", {conversation_id: "c-own"}));
    assert.ok(reply.result.messages[0].images.every(image => image.unavailable && !image.transfer_id), JSON.stringify(options));
    assert.equal(f.archived().length, 0);
    assert.equal(f.viewer().viewerClicks, 1);
    assert.equal(f.state().clicks, 0); assert.equal(f.state().selections, 0);
  }
});
test("gallery diagnostics distinguish fixed binding guards without image content or target values", async () => {
  const cases = [
    [{existingViewer: true}, "existing_modal", 0],
    [{groupButtonInForm: true}, "opener_control", 0],
    [{foreignViewer: true}, "existing_modal", 1],
    [{groupNoBackdrop: true}, "modal_ownership", 1],
    [{groupNoStrip: true}, "modal_structure", 1],
    [{groupCountMismatch: true}, "thumbnail_count", 1],
    [{groupHiddenThumb: true}, "thumbnail_visibility", 1],
    [{groupActiveIndex: 1}, "active_thumbnail", 1],
    [{groupExtraNestedImage: true}, "nested_images", 1],
    [{groupMainOutsideCenter: true}, "image_containment", 1],
    [{imageSource: "data:image/jpeg;base64," + Buffer.from(variantJPEG).toString("base64")}, "source_match", 1],
    [{viewerMessageDetached: true}, "post_identity_binding", 1],
    [{viewerReplaced: true}, "post_identity_binding", 1],
    [{groupSelectionChanged: true}, "thumbnails_changed", 1],
    [{groupSourceChanged: true}, "images_changed", 1],
    [{viewerCloseDisabled: true}, "close_control", 1],
  ];
  for (const [options, guard, clickCount] of cases) {
    const f = fixture({archiveImage: true, groupCount: 4, clock: true, ...options});
    const reply = await f.run(command("get_conversation", {conversation_id: "c-own"}));
    for (const image of reply.result.messages[0].images) {
      assert.equal(image.unavailable, true);
      const diagnostic = image.error.image_structure;
      assert.equal(diagnostic.stage, "viewer_binding");
      assert.equal(diagnostic.viewer_guard, guard, JSON.stringify(options));
      assert.ok(Object.keys(diagnostic).every(key => ["stage", "viewer_guard", "view_verified", "image_count", "container_count", "readiness_timeout"].includes(key)));
      assert.doesNotMatch(JSON.stringify(diagnostic), /PRIVATE|SECRET|https:|base64|11111111|c-own|12345|67890|[a-f0-9]{64}/);
    }
    assert.equal(f.viewer().viewerClicks, clickCount);
    assert.equal(f.viewer().viewerCloses, 0);
    assert.equal(f.archived().length, 0);
    assert.equal(f.state().clicks, 0); assert.equal(f.state().selections, 0);
  }
});
test("gallery selection supports WebKit variable borders while requiring the exact inline and computed frame", async () => {
  const ready = fixture({archiveImage: true, groupCount: 4, groupCSSVariable: true});
  const accepted = await ready.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.ok(accepted.result.messages[0].images.every(image => image.transfer_id && !image.unavailable));
  assert.deepEqual(ready.viewer(), {viewerClicks: 1, viewerCloses: 1, viewerOpen: false});
  const cases = [
    {groupNoInlineBorder: true, groupForeignCSS: true},
    {groupComputedBoxSizing: "content-box"}, {groupMultipleActive: true}, {groupActiveIndex: 1}, {groupComputedError: true},
    ...["Top", "Right", "Bottom", "Left"].flatMap(side => [
      {groupComputedOverride: {[`border${side}Width`]: "1px"}},
      {groupComputedOverride: {[`border${side}Style`]: "dashed"}},
    ]),
  ];
  for (const options of cases) {
    const f = fixture({archiveImage: true, groupCount: 4, groupCSSVariable: true, ...options});
    const reply = await f.run(command("get_conversation", {conversation_id: "c-own"}));
    assert.ok(reply.result.messages[0].images.every(image => image.unavailable && image.error.image_structure.viewer_guard === "active_thumbnail"), JSON.stringify(options));
    assert.equal(f.viewer().viewerClicks, 1); assert.equal(f.viewer().viewerCloses, 0);
    assert.equal(f.archived().length, 0); assert.equal(f.state().clicks, 0);
  }
  const changed = fixture({archiveImage: true, groupCount: 4, groupCSSVariable: true, groupComputedChanged: true});
  const refused = await changed.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.ok(refused.result.messages[0].images.every(image => image.unavailable && image.error.image_structure.viewer_guard === "thumbnails_changed"));
  assert.equal(changed.viewer().viewerClicks, 1); assert.equal(changed.archived().length, 0);
});
test("a changed account aborts a group before any bytes are archived", async () => {
  const f = fixture({archiveImage: true, groupCount: 4, switchAccount: true});
  const reply = await f.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.equal(reply.error.code, "account_mismatch");
  assert.equal(f.archived().length, 0);
  assert.equal(f.viewer().viewerClicks, 1);
  assert.equal(f.state().clicks, 0);
});
test("group loading is bounded and only accepts fully loaded unchanged thumbnails", async () => {
  const ready = fixture({archiveImage: true, groupCount: 4, fraudOverlay: true, groupThumbDelayed: true, clock: true});
  const reply = await ready.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.ok(reply.result.messages[0].images.every(image => image.transfer_id));
  assert.ok(ready.waiting().elapsed >= 500 && ready.waiting().elapsed < 10000);
  const missing = fixture({archiveImage: true, groupCount: 4, fraudOverlay: true, noDOMImage: true, clock: true, timerMinimum: 3000});
  const unavailable = await missing.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.ok(unavailable.result.messages[0].images.every(image => image.unavailable));
  assert.ok(missing.waiting().waits <= 4);
  assert.equal(missing.viewer().viewerClicks, 1);
});
test("equal image contents remain separate real attachment slots", async () => {
  const f = fixture({archiveImage: true, groupCount: 5, groupSameBytes: true});
  const reply = await f.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.equal(new Set(reply.result.messages[0].images.map(image => image.remote_id)).size, 5);
  assert.equal(new Set(reply.result.messages[0].images.map(image => image.transfer_id)).size, 5);
  assert.ok(f.archived().every(chunk => chunk.sha256 === hash(JPEG)));
  assert.equal(f.viewer().viewerCloses, 1);
});
test("read-only group support does not relax single-image send receipt guards", async () => {
  const f = fixture({imageMode: true, confirmed: true, multipleDTOImages: true, groupCount: 5});
  const reply = await f.run(imageConfirmation());
  assert.equal(reply.error.code, "outcome_unknown");
  assert.equal(f.viewer().viewerClicks, 0);
  assert.equal(f.archived().length, 0);
  assert.equal(f.state().clicks, 0);
});
test("group deadline prevents opening and late account responses cannot complete an archive", async () => {
  const beforeOpen = fixture({archiveImage: true, groupCount: 4, clock: true, groupBeforeOpenAdvance: 10001});
  const stopped = await beforeOpen.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.ok(stopped.result.messages[0].images.every(image => image.unavailable && image.error.image_structure.stage === "viewer_ready"));
  assert.equal(beforeOpen.viewer().viewerClicks, 0);
  assert.equal(beforeOpen.archived().length, 0);
  const late = fixture({archiveImage: true, groupCount: 4, clock: true, beforeSecondProfile: async () => late.advance(10001)});
  const reply = await late.run(command("get_conversation", {conversation_id: "c-own"}));
  assert.ok(reply.result.messages[0].images.every(image => image.unavailable && image.error.image_structure.stage === "viewer_ready"));
  assert.equal(late.viewer().viewerClicks, 1);
  assert.equal(late.viewer().viewerCloses, 0);
  assert.equal(late.archived().length, 0);
  assert.equal(late.state().clicks, 0);
});
