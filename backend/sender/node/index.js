// The entry point: the real Baileys, pino, qrcode and fetch behind server.js, the
// settings from the environment, the port.

import path from "node:path";
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
} from "baileys";
import pino from "pino";
import QRCode from "qrcode";
import { createServer } from "./server.js";

const PORT = Number(process.env.SENDER_NODE_PORT ?? 8788);
const log = pino({ level: "info" });

createServer({
  makeSocket: makeWASocket,
  authState: useMultiFileAuthState,
  loggedOutCode: DisconnectReason.loggedOut,
  toDataURL: (text) => QRCode.toDataURL(text),
  fetch,
  log,
  sessions: path.resolve("sessions"),
  pythonUrl: process.env.SENDER_PYTHON_URL ?? "http://127.0.0.1:8787",
  webhookSecret: process.env.SENDER_WEBHOOK_SECRET ?? "",
}).listen(PORT, () => log.info({ port: PORT }, "sender-node слушает"));
