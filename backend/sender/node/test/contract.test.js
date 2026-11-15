// The cases of contract.json against the real server.js. The fake Node of the
// sandbox answers the same file (backend/sandbox/tests/test_node.py): a change of
// the answer on either side fails one of the two.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { bringQr, harness } from "./harness.js";

const contract = JSON.parse(await readFile(new URL("./contract.json", import.meta.url), "utf8"));
const { number, events } = contract;

// The body as Python reads it: only the fields of `expected`, a missing one is null.
const read = (expected, actual) =>
  expected !== null && typeof expected === "object"
    ? Object.fromEntries(Object.keys(expected).map((key) => [key, read(expected[key], actual?.[key])]))
    : (actual ?? null);

for (const c of contract.cases) {
  test(c.name, async (t) => {
    const h = await harness(t);
    h.onSocket = bringQr;
    await h.given(number, c.given);

    const { status, body } = await h.call(c.request.method, c.request.path, c.request.body);

    assert.equal(status, c.status);
    assert.deepEqual(read(c.body ?? {}, body), c.body ?? {});
    for (const field of c.strings ?? []) {
      assert.ok(typeof body[field] === "string" && body[field], `${field}: ${body[field]}`);
    }
  });
}

test("a socket that opens is the connection event of the contract", async (t) => {
  const h = await harness(t);
  await h.given(number, "connected");
  assert.deepEqual(h.events.at(-1).body, events.connection);
});

test("a reply of the lead is the incoming event of the contract", async (t) => {
  const h = await harness(t);
  await h.given(number, "connected");
  await h.last().emit("messages.upsert", {
    type: "notify",
    messages: [
      {
        key: { remoteJid: events.incoming.from, id: events.incoming.provider_id, fromMe: false },
        message: { conversation: events.incoming.text },
      },
    ],
  });
  assert.deepEqual(h.events.at(-1).body, events.incoming);
});

test("a delivery receipt is the status event of the contract", async (t) => {
  const h = await harness(t);
  await h.given(number, "connected");
  await h.last().emit("messages.update", [
    { key: { id: events.status.provider_id }, update: { status: events.status.status } },
  ]);
  assert.deepEqual(h.events.at(-1).body, events.status);
});
