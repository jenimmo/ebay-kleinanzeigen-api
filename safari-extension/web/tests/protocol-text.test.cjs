"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const P = require("../protocol.js");

test("receipt equality permits only the observed CRLF versus LF transport difference", () => {
  for (const [a, b] of [
    ["Grüße\r\nmit Bild: äöüß", "Grüße\nmit Bild: äöüß"],
    ["Grüße\n\r\nEnde\r\n", "Grüße\n\nEnde\n"],
    ["Unverändert\r", "Unverändert\r"],
    ["", ""],
  ]) {
    assert.equal(P.messageTextEqual(a, b), true);
    assert.equal(P.messageTextEqual(b, a), true);
  }
  for (const [a, b] of [
    ["Grüße\rEnde", "Grüße\nEnde"], [" Grüße", "Grüße"], ["Grüße ", "Grüße"],
    ["Grüße\n", "Grüße"], ["Grüße\n\nEnde", "Grüße\nEnde"],
    ["Grüße\tEnde", "Grüße Ende"], ["Grüße  Ende", "Grüße Ende"],
    ["Grüße", "grüße"], ["Grüße", "Grüße\u200b"], ["ä", "a\u0308"],
    ["Grüße\r\r\nEnde", "Grüße\nEnde"], [null, ""], [123, "123"], [{}, "[object Object]"],
  ]) assert.equal(P.messageTextEqual(a, b), false);
});

test("first-contact receipts retain server ID, peer, time and payload guards with CRLF", () => {
  const timestamp = new Date(Date.now() - 1000).toISOString();
  const command = {user_id: "111", args: {
    target: {target_kind: "listing", target_id: "1234500001", listing_id: "1234500001", account_user_id: "111", recipient_user_id: "222"},
    conversation_id: "c-own", expected_message_id: "m-new", text: "Grüße\nNur eigener Test", not_before: timestamp, baseline_message_ids: ["m-old"],
  }};
  const message = {message_id: "m-new", sender_user_id: "111", direction: "outbound", text: "Grüße\r\nNur eigener Test", images: [], created_at: timestamp};
  const snapshot = {conversation_id: "c-own", recipient_user_id: "222", messages: [message]};
  assert.equal(P.confirmationReceipt(snapshot, command).message_id, "m-new");
  assert.equal(snapshot.messages[0].text, "Grüße\r\nNur eigener Test");
  for (const change of [
    {message_id: "m-old"}, {message_id: "m-other"}, {sender_user_id: "222"}, {direction: "inbound"},
    {text: "Grüße\r\nNur eigener Test "}, {text: "Grüße Nur eigener Test"}, {images: [{remote_id: "image"}]},
    {created_at: new Date(Date.parse(timestamp) - 1).toISOString()},
  ]) assert.equal(P.confirmationReceipt({...snapshot, messages: [{...message, ...change}]}, command), null);
  assert.throws(() => P.confirmationReceipt({...snapshot, recipient_user_id: "333"}, command), error => error.code === "outcome_unknown");
  assert.throws(() => P.confirmationReceipt({...snapshot, messages: [message, message]}, command), error => error.code === "outcome_unknown");
});
