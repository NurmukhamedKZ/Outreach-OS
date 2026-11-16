// What server.js gets from outside, faked: Baileys' socket and auth state, the QR
// renderer, fetch to Python and the logger. No package is imported, so the tests
// run without `npm ci`, without the network and without WhatsApp.

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { Readable } from "node:stream";
import { createServer } from "../server.js";

export const LOGGED_OUT = 401; // DisconnectReason.loggedOut of Baileys
export const CONNECTION_LOST = 408; // any other close code: Node reconnects
export const NOW = Date.parse("2026-11-16T09:00:00Z");

const silent = { info() {}, warn() {}, error() {}, child: () => silent };

export class FakeSocket {
  constructor(options) {
    this.options = options;
    this.listeners = {};
    this.sent = [];
    this.pairings = [];
    this.hasWhatsApp = true;
    this.failure = null; // thrown by sendMessage on a live socket
    this.closed = false;
    this.ended = false;
    this.ev = { on: (name, listener) => (this.listeners[name] ??= []).push(listener) };
  }

  // Awaits every listener, so a test sees the update fully handled.
  async emit(name, data) {
    for (const listener of this.listeners[name] ?? []) await listener(data);
  }

  open() {
    return this.emit("connection.update", { connection: "open" });
  }

  close(statusCode) {
    this.closed = true;
    return this.emit("connection.update", {
      connection: "close",
      lastDisconnect: { error: { output: { statusCode } } },
    });
  }

  async sendMessage(jid, content) {
    if (this.closed) throw new Error("Connection Closed");
    if (this.failure) throw this.failure;
    this.sent.push({ jid, content });
    return { key: { id: `3EB0${this.sent.length}` } };
  }

  async onWhatsApp(jid) {
    return [{ jid, exists: this.hasWhatsApp }];
  }

  async requestPairingCode(digits) {
    this.pairings.push(digits);
    return "ABCD1234";
  }

  end() {
    this.ended = true;
  }
}

// Every test gets its own server, sessions directory and mocked clock: setTimeout
// fires only on tick, so backoffs and the QR wait cost no real time.
export async function harness(t, { webhookSecret = "" } = {}) {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: NOW });
  const sessions = await mkdtemp(path.join(tmpdir(), "sender-node-"));
  t.after(() => rm(sessions, { recursive: true, force: true }));
  const h = {
    sessions,
    sockets: [], // every socket made, in order
    auth: [], // the directory of every auth state asked for
    saved: [], // every credentials save: {dir, creds}
    events: [], // every webhook call: {url, headers, body}
    webhookStatus: 200,
    onSocket: () => {},
    onAuth: () => {}, // awaited before the auth state is read: may hold it or throw
    last: () => h.sockets.at(-1),
    // Sockets made and neither closed nor ended: the process may hold one per number.
    open: () => h.sockets.filter((sock) => !sock.closed && !sock.ended),
    // Hold every connect at the auth state, as a slow disk would; the call lets go.
    hold: () => {
      let release;
      const gate = new Promise((resolve) => (release = resolve));
      const before = h.onAuth;
      h.onAuth = () => gate;
      return () => {
        h.onAuth = before;
        release();
      };
    },
    call: (method, url, body) => call(h.server, method, url, body),
    tick: async (ms) => {
      t.mock.timers.tick(ms);
      await flush();
    },
    // Tick a second at a time until `done`; return the seconds waited. A bound,
    // not a hang: a broken backoff must fail the test, not stall npm test.
    waitFor: async (done, limitSeconds = 3600) => {
      for (let waited = 0; waited < limitSeconds; waited++) {
        if (done()) return waited;
        await h.tick(1000);
      }
      throw new Error(`nothing happened in ${limitSeconds} s`);
    },
    // Put the socket of `number` into a state of the contract.
    given: async (number, state) => {
      await h.call("POST", "/check", { number, to: "+77010000001" });
      await h.last().open();
      if (state === "loggedOut") await h.last().close(LOGGED_OUT);
      if (state === "socketDown") await h.last().close(CONNECTION_LOST);
    },
  };
  h.server = createServer({
    makeSocket: (options) => {
      const sock = new FakeSocket(options);
      h.sockets.push(sock);
      h.onSocket(sock);
      return sock;
    },
    authState: async (dir) => {
      h.auth.push(dir);
      await h.onAuth(dir);
      return { state: { dir }, saveCreds: (creds) => h.saved.push({ dir, creds }) };
    },
    loggedOutCode: LOGGED_OUT,
    toDataURL: async (text) => `data:image/png;base64,${text}`,
    fetch: async (url, init) => {
      h.events.push({ url, headers: init.headers, body: JSON.parse(init.body) });
      return { ok: h.webhookStatus < 300, status: h.webhookStatus };
    },
    log: silent,
    sessions,
    pythonUrl: "http://python.test",
    webhookSecret,
  });
  return h;
}

// A new socket of Baileys that is not linked yet brings a QR right away.
export const bringQr = (sock) =>
  queueMicrotask(() => sock.emit("connection.update", { qr: "QR-DATA" }));

export const flush = () => new Promise((resolve) => setImmediate(resolve));

// The request handler of the server, called without a port: no socket is opened.
export function call(server, method, url, body) {
  const text = body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body);
  const request = Object.assign(Readable.from(text ? [Buffer.from(text)] : []), { method, url });
  return new Promise((resolve) => {
    server.emit("request", request, {
      writeHead(status, headers) {
        this.status = status;
        this.headers = headers;
      },
      end(json) {
        resolve({ status: this.status, headers: this.headers, body: JSON.parse(json) });
      },
    });
  });
}
