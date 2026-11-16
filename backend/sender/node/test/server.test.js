// server.js as it behaves today, beyond the contract: the number at the trust
// boundary, idempotency, the unlinked session put aside, reconnects, the webhook.

import assert from "node:assert/strict";
import { mkdir, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";
import { CONNECTION_LOST, LOGGED_OUT, NOW, bringQr, flush, harness } from "./harness.js";

const OURS = "+77000000001";
const LEAD = "+77010000001";
const send = (extra = {}) => ({ number: OURS, to: LEAD, text: "привет", key: "outbox-1", ...extra });

// --- the number --------------------------------------------------------------------

for (const route of ["/send", "/check", "/pair", "/qr"]) {
  for (const number of [
    "77000000001",
    "+7 700 000 00 01",
    "../x",
    "+77000000001/../../x",
    "+770000000", // nine digits
    "+7700000000012345", // sixteen digits
  ]) {
    test(`${route} refuses ${JSON.stringify(number)} before any socket or directory`, async (t) => {
      const h = await harness(t);
      const { status, body } = await h.call("POST", route, { ...send(), number });
      assert.equal(status, 422);
      assert.equal(body.code, "numberInvalid");
      assert.equal(typeof body.error, "string");
      assert.deepEqual([h.sockets.length, h.auth.length], [0, 0]);
    });
  }
}

// Not fixed by #55 (index.js behaves as before): a body without a number reaches
// path.join and is a 500, and so is a number that is no string; see the PR.
for (const route of ["/send", "/check", "/pair", "/qr"]) {
  for (const number of [undefined, ["+77000000001"]]) {
    test(`${route} refuses a number ${JSON.stringify(number)} with a 422`, { todo: "#55 PR" }, async (t) => {
      const h = await harness(t);
      const { status, body } = await h.call("POST", route, { ...send(), number });
      assert.deepEqual([status, body.code], [422, "numberInvalid"]);
    });
  }
}

for (const number of ["+7700000000", "+770000000001234"]) {
  test(`${number.length - 1} digits after the plus are a number`, async (t) => {
    const h = await harness(t);
    const { status } = await h.call("POST", "/check", { number, to: LEAD });
    assert.equal(status, 200);
  });
}

test("the session of a number lives in its own directory under sessions", async (t) => {
  const h = await harness(t);
  await h.call("POST", "/check", { number: OURS, to: LEAD });
  assert.deepEqual(h.auth, [path.join(h.sessions, OURS)]);
  assert.deepEqual(h.last().options.auth, { dir: path.join(h.sessions, OURS) });
});

// --- /send -------------------------------------------------------------------------

test("the credentials Baileys updates are saved to the session of the number", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  await h.last().emit("creds.update", { me: "fresh" });
  assert.deepEqual(h.saved, [{ dir: path.join(h.sessions, OURS), creds: { me: "fresh" } }]);
});

test("send puts the text to the lead's jid and returns the provider id", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  const { status, body } = await h.call("POST", "/send", send());
  assert.equal(status, 200);
  assert.deepEqual(body, { ok: true, sent: true, provider_id: "3EB01" });
  assert.deepEqual(h.last().sent, [{ jid: "77010000001@s.whatsapp.net", content: { text: "привет" } }]);
});

test("a repeat with the same key returns the first provider id and sends nothing", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  const first = await h.call("POST", "/send", send());
  const again = await h.call("POST", "/send", send({ text: "другое" }));
  assert.equal(again.body.provider_id, first.body.provider_id);
  assert.equal(again.body.sent, true);
  assert.equal(h.last().sent.length, 1);
});

test("send without a key sends every time", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  await h.call("POST", "/send", send({ key: undefined }));
  await h.call("POST", "/send", send({ key: undefined }));
  assert.equal(h.last().sent.length, 2);
});

test("send on a logged out session is sent false without touching the socket", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "loggedOut");
  const { status, body } = await h.call("POST", "/send", send());
  assert.equal(status, 200);
  assert.deepEqual(body, { ok: false, sent: false, error: "loggedOut" });
  assert.deepEqual([h.sockets.length, h.last().sent.length], [1, 0]);
});

test("a failure on a socket that is not up is sent false: the frame never left", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "socketDown");
  const { status, body } = await h.call("POST", "/send", send());
  assert.equal(status, 200);
  assert.deepEqual(body, { ok: false, sent: false, error: "Error: Connection Closed" });
});

test("a failure on a connected socket is a 500: the fate of the send is unknown", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  h.last().failure = new Error("Timed Out");
  const { status, body } = await h.call("POST", "/send", send());
  assert.equal(status, 500);
  assert.deepEqual(body, { code: "nodeError", error: "Timed Out" });
});

test("a failed send keeps no key: the retry sends", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  h.last().failure = new Error("Timed Out");
  await h.call("POST", "/send", send());
  h.last().failure = null;
  const { body } = await h.call("POST", "/send", send());
  assert.deepEqual([body.sent, h.last().sent.length], [true, 1]);
});

test("an unknown type is sent false", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  const { status, body } = await h.call("POST", "/send", send({ type: "video" }));
  assert.equal(status, 200);
  assert.deepEqual(body, { ok: false, sent: false, error: "unknown type video" });
  assert.equal(h.last().sent.length, 0);
});

test("send on a number without a socket opens one", async (t) => {
  const h = await harness(t);
  const { body } = await h.call("POST", "/send", send());
  assert.deepEqual([body.sent, h.sockets.length], [true, 1]);
});

// Not fixed by #55: socketOf checks, then awaits the auth state, so two requests
// at once on a number without a socket open two sockets on one session; see the PR.
test("two requests at once on a number without a socket open one socket", { todo: "#55 PR" }, async (t) => {
  const h = await harness(t);
  await Promise.all([
    h.call("POST", "/send", send()),
    h.call("POST", "/check", { number: OURS, to: LEAD }),
  ]);
  assert.equal(h.sockets.length, 1);
});

// --- /check ------------------------------------------------------------------------

for (const hasWhatsApp of [true, false]) {
  test(`check answers has_whatsapp ${hasWhatsApp}`, async (t) => {
    const h = await harness(t);
    await h.given(OURS, "connected");
    h.last().hasWhatsApp = hasWhatsApp;
    const { status, body } = await h.call("POST", "/check", { number: OURS, to: LEAD });
    assert.equal(status, 200);
    assert.deepEqual(body, { has_whatsapp: hasWhatsApp });
  });
}

test("check on a logged out session is a 409 with its code", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "loggedOut");
  const { status, body } = await h.call("POST", "/check", { number: OURS, to: LEAD });
  assert.equal(status, 409);
  assert.deepEqual(body, { code: "sessionLoggedOut", error: "сессия отвязана" });
});

// --- /pair and /qr on an unlinked session ------------------------------------------

async function unlinked(t) {
  const h = await harness(t);
  await mkdir(path.join(h.sessions, OURS));
  await writeFile(path.join(h.sessions, OURS, "creds.json"), "{}");
  h.onSocket = bringQr;
  await h.given(OURS, "loggedOut");
  return h;
}

const aside = `${OURS}.bak.${Math.floor(NOW / 1000)}`;

for (const route of ["/pair", "/qr"]) {
  test(`${route} on a logged out session puts the session aside and opens a fresh socket`, async (t) => {
    const h = await unlinked(t);
    const dead = h.last();

    const { status } = await h.call("POST", route, { number: OURS });

    assert.equal(status, 200);
    assert.deepEqual(await readdir(h.sessions), [aside]);
    assert.deepEqual(await readdir(path.join(h.sessions, aside)), ["creds.json"]);
    assert.equal(dead.ended, true);
    assert.equal(h.sockets.length, 2);
    assert.deepEqual(h.auth.at(-1), path.join(h.sessions, OURS));
  });
}

test("pair asks the fresh socket for a code by the digits of the number", async (t) => {
  const h = await unlinked(t);
  const { body } = await h.call("POST", "/pair", { number: OURS });
  assert.deepEqual(body, { code: "ABCD1234" });
  assert.deepEqual(h.last().pairings, ["77000000001"]);
});

test("qr returns the QR of the fresh socket as a data url", async (t) => {
  const h = await unlinked(t);
  const { body } = await h.call("POST", "/qr", { number: OURS });
  assert.deepEqual(body, { qr: "data:image/png;base64,QR-DATA" });
});

test("a dead socket that throws on end is still put aside", async (t) => {
  const h = await unlinked(t);
  h.last().end = () => {
    throw new Error("Connection Closed");
  };
  const { status } = await h.call("POST", "/qr", { number: OURS });
  assert.deepEqual([status, h.sockets.length], [200, 2]);
});

test("an unlinked session without a directory still gets a fresh socket", async (t) => {
  const h = await harness(t);
  h.onSocket = bringQr;
  await h.given(OURS, "loggedOut");
  const { status } = await h.call("POST", "/qr", { number: OURS });
  assert.deepEqual([status, h.sockets.length], [200, 2]);
  assert.deepEqual(await readdir(h.sessions), []);
});

test("two qr at once on a logged out session open one socket", async (t) => {
  const h = await unlinked(t);
  const answers = await Promise.all([
    h.call("POST", "/qr", { number: OURS }),
    h.call("POST", "/qr", { number: OURS }),
  ]);
  assert.deepEqual(answers.map((a) => a.status), [200, 200]);
  assert.equal(h.sockets.length, 2);
  assert.deepEqual(await readdir(h.sessions), [aside]);
});

test("qr on a connected session keeps the session", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  h.last().emit("connection.update", { qr: "LATE" });
  await flush();
  const { status } = await h.call("POST", "/qr", { number: OURS });
  assert.deepEqual([status, h.sockets.length], [200, 1]);
});

test("the close of the replaced socket does not touch the fresh one", async (t) => {
  const h = await unlinked(t);
  const dead = h.sockets[0];
  await h.call("POST", "/qr", { number: OURS });
  const sent = h.events.length;

  await dead.close(CONNECTION_LOST);
  await h.tick(10 * 60 * 1000);

  assert.equal(h.sockets.length, 2);
  assert.equal(h.events.length, sent);
  const { body } = await h.call("GET", "/health");
  assert.equal(body[OURS].state, "connecting");
});

async function qrTimesOut(h) {
  let answer;
  h.call("POST", "/qr", { number: OURS }).then((a) => (answer = a));
  for (let waited = 0; waited < 14900; waited += 100) await h.tick(100);
  assert.equal(answer, undefined);
  for (let waited = 0; !answer && waited < 1000; waited += 100) await h.tick(100);
  assert.equal(answer.status, 504);
  assert.deepEqual(answer.body, { code: "qrTimeout", error: "QR не пришёл вовремя" });
}

test("a QR that does not come is a 504 after fifteen seconds", async (t) => {
  await qrTimesOut(await harness(t));
});

test("a socket that opened drops its QR: it is linked already", async (t) => {
  const h = await harness(t);
  h.onSocket = bringQr;
  await h.given(OURS, "connected");
  await qrTimesOut(h);
});

// --- the connection ----------------------------------------------------------------

const states = (h) => h.events.filter((e) => e.body.kind === "connection").map((e) => e.body.state);

test("open is connected and every change sends a connection event", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  await h.last().close(CONNECTION_LOST);
  await h.tick(5000);
  await h.last().open();
  assert.deepEqual(states(h), ["connected", "reconnecting", "connected"]);
  assert.deepEqual(h.events[0].body, { kind: "connection", number: OURS, state: "connected" });
});

test("a logged out close does not reconnect", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "loggedOut");
  await h.tick(24 * 60 * 60 * 1000);
  assert.equal(h.sockets.length, 1);
  assert.deepEqual(states(h), ["connected", "loggedOut"]);
});

test("reconnects back off twice longer each time up to five minutes, then stall", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  const delays = [];
  for (let attempt = 1; attempt <= 12; attempt++) {
    await h.last().close(CONNECTION_LOST);
    const made = h.sockets.length;
    delays.push(await h.waitFor(() => h.sockets.length > made));
  }
  assert.deepEqual(delays, [5, 10, 20, 40, 80, 160, 300, 300, 300, 300, 300, 300]);

  await h.last().close(CONNECTION_LOST);
  await h.tick(24 * 60 * 60 * 1000);
  assert.equal(h.sockets.length, 13);
  const { body } = await h.call("GET", "/health");
  assert.equal(body[OURS].state, "stalled");
  assert.equal(states(h).at(-1), "stalled");
});

test("open resets the backoff", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  await h.last().close(CONNECTION_LOST);
  await h.tick(5000);
  await h.last().close(CONNECTION_LOST);
  await h.tick(10000);
  await h.last().open();
  await h.last().close(CONNECTION_LOST);
  await h.tick(4999);
  assert.equal(h.sockets.length, 3);
  await h.tick(1);
  assert.equal(h.sockets.length, 4);
});

test("the reconnect counter counts the day, not the uptime", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  await h.last().close(CONNECTION_LOST);
  await h.tick(5000);
  await h.last().close(CONNECTION_LOST);
  assert.equal((await h.call("GET", "/health")).body[OURS].reconnects, 2);

  await h.tick(24 * 60 * 60 * 1000);
  await h.last().close(CONNECTION_LOST);
  assert.equal((await h.call("GET", "/health")).body[OURS].reconnects, 1);
});

// --- /health -----------------------------------------------------------------------

test("health reports state and reconnects of every socket by number", async (t) => {
  const h = await harness(t);
  assert.deepEqual((await h.call("GET", "/health")).body, {});
  await h.given(OURS, "connected");
  await h.given("+77000000002", "socketDown");
  const { status, body } = await h.call("GET", "/health");
  assert.equal(status, 200);
  assert.deepEqual(body, {
    [OURS]: { state: "connected", reconnects: 0 },
    "+77000000002": { state: "reconnecting", reconnects: 1 },
  });
});

// --- the webhook -------------------------------------------------------------------

test("an event goes to the webhook of Python with the secret", async (t) => {
  const h = await harness(t, { webhookSecret: "s3cret" });
  await h.given(OURS, "connected");
  assert.equal(h.events[0].url, "http://python.test/api/sender/webhook");
  assert.deepEqual(h.events[0].headers, {
    "content-type": "application/json",
    "x-sender-secret": "s3cret",
  });
});

test("an empty secret sends no secret header", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  assert.deepEqual(h.events[0].headers, { "content-type": "application/json" });
});

test("an event Python did not take is retried with backoff and dropped after twelve tries", async (t) => {
  const h = await harness(t);
  await h.call("POST", "/check", { number: OURS, to: LEAD });
  h.webhookStatus = 500;
  await h.last().open();
  const delays = [];
  while (delays.length < 11) {
    const tries = h.events.length;
    delays.push(await h.waitFor(() => h.events.length > tries));
  }
  assert.deepEqual(delays, [5, 10, 20, 40, 80, 160, 300, 300, 300, 300, 300]);
  await h.tick(24 * 60 * 60 * 1000);
  assert.equal(h.events.length, 12);
  assert.ok(h.events.every((e) => e.body.state === "connected"));
});

test("an event Python took is sent once", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  await h.tick(24 * 60 * 60 * 1000);
  assert.equal(h.events.length, 1);
});

const upsert = (h, type, messages) => h.last().emit("messages.upsert", { type, messages });
const incoming = (h) => h.events.filter((e) => e.body.kind === "incoming").map((e) => e.body);
const reply = (message, extra = {}) => ({
  key: { remoteJid: "77010000001@s.whatsapp.net", id: "IN1", fromMe: false, ...extra },
  message,
});

test("our own message and a history sync are not incoming", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  await upsert(h, "notify", [reply({ conversation: "наше" }, { fromMe: true })]);
  await upsert(h, "append", [reply({ conversation: "старое" })]);
  assert.deepEqual(incoming(h), []);
});

test("the text of a reply is the conversation, else the extended text, else empty", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  await upsert(h, "notify", [
    reply({ conversation: "простой" }),
    reply({ extendedTextMessage: { text: "со ссылкой" } }),
    reply({ audioMessage: {} }),
    reply(undefined),
  ]);
  assert.deepEqual(
    incoming(h).map((e) => e.text),
    ["простой", "со ссылкой", "", ""],
  );
});

test("a receipt goes as it came, a missing status as null", async (t) => {
  const h = await harness(t);
  await h.given(OURS, "connected");
  await h.last().emit("messages.update", [
    { key: { id: "3EB01" }, update: { status: 4 } },
    { key: { id: "3EB02" }, update: {} },
  ]);
  const receipts = h.events.filter((e) => e.body.kind === "status").map((e) => e.body);
  assert.deepEqual(receipts, [
    { kind: "status", number: OURS, provider_id: "3EB01", status: 4 },
    { kind: "status", number: OURS, provider_id: "3EB02", status: null },
  ]);
});

// --- the rest ----------------------------------------------------------------------

for (const [method, url] of [["POST", "/nope"], ["GET", "/send"], ["POST", "/send?x=1"]]) {
  test(`${method} ${url} is a 404`, async (t) => {
    const h = await harness(t);
    const { status } = await h.call(method, url, {});
    assert.equal(status, 404);
  });
}

test("a body that is not JSON is a 500 with the code", async (t) => {
  const h = await harness(t);
  const { status, body } = await h.call("POST", "/send", "{не json");
  assert.equal(status, 500);
  assert.equal(body.code, "nodeError");
  assert.equal(h.sockets.length, 0);
});

test("answers are JSON", async (t) => {
  const h = await harness(t);
  const { headers } = await h.call("GET", "/health");
  assert.deepEqual(headers, { "content-type": "application/json" });
});

test("two servers share no state", async (t) => {
  const one = await harness(t);
  await one.given(OURS, "connected");
  await one.call("POST", "/send", send());
  t.mock.timers.reset();
  const other = await harness(t);
  assert.deepEqual((await other.call("GET", "/health")).body, {});
  const { body } = await other.call("POST", "/send", send());
  assert.equal(other.last().sent.length, 1, body.provider_id);
});
