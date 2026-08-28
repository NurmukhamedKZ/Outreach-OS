// Тупая труба к WhatsApp: сокеты Baileys и четыре ручки. Здесь нет ни лидов,
// ни тредов, ни лимитов, ни расписания — всё это решает Python, потому что
// эту часть нельзя протестировать без живого WhatsApp.

import http from "node:http";
import path from "node:path";
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
} from "baileys";
import pino from "pino";

const PORT = Number(process.env.SENDER_NODE_PORT ?? 8788);
const PYTHON_URL = process.env.SENDER_PYTHON_URL ?? "http://127.0.0.1:8787";
const SESSIONS = path.resolve("sessions");
const WEBHOOK_RETRY_MS = 5000;
const WEBHOOK_LOG_INTERVAL_MS = 60000;

const log = pino({ level: "info" });
const sockets = new Map();   // number -> {sock, state, reconnects}
let lastWebhookLog = 0;

const jid = (number) => `${number.replace(/\D/g, "")}@s.whatsapp.net`;

async function connect(number) {
  const { state, saveCreds } = await useMultiFileAuthState(path.join(SESSIONS, number));
  const sock = makeWASocket({ auth: state, logger: log.child({ number }) });
  const entry = sockets.get(number) ?? { reconnects: 0 };
  sockets.set(number, { ...entry, sock, state: "connecting" });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    const current = sockets.get(number);
    if (update.connection === "open") {
      sockets.set(number, { ...current, state: "connected" });
    }
    if (update.connection === "close") {
      const code = update.lastDisconnect?.error?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;
      sockets.set(number, {
        ...current,
        state: loggedOut ? "loggedOut" : "reconnecting",
        reconnects: current.reconnects + (loggedOut ? 0 : 1),
      });
      // loggedOut не переподключается: сессия мертва, номер требует телефона.
      if (!loggedOut) setTimeout(() => connect(number), WEBHOOK_RETRY_MS);
    }
    await notify({ kind: "connection", number, state: sockets.get(number).state });
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const message of messages) {
      if (message.key.fromMe) continue;
      await notify({
        kind: "incoming",
        number,
        from: message.key.remoteJid,
        provider_id: message.key.id,
        text: message.message?.conversation
          ?? message.message?.extendedTextMessage?.text
          ?? "",
      });
    }
  });

  sock.ev.on("messages.update", async (updates) => {
    for (const update of updates) {
      await notify({
        kind: "status",
        number,
        provider_id: update.key.id,
        status: update.update?.status ?? null,
      });
    }
  });

  return sock;
}

// Вебхук повторяется, пока Python не ответит 2xx: событие, потерянное
// из-за перезапуска бэкенда, — это номер, навсегда оставшийся без метрики.
// Лог — один раз в минуту, а не на каждый несостоявшийся повтор: до того,
// как в Python приедет ручка /api/sender/webhook, каждый повтор падал бы
// 404-строкой и утопил бы реальные события.
async function notify(payload) {
  try {
    const response = await fetch(`${PYTHON_URL}/api/sender/webhook`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  } catch (error) {
    const now = Date.now();
    if (now - lastWebhookLog > WEBHOOK_LOG_INTERVAL_MS) {
      log.warn({ error: String(error), payload }, "вебхук не доставлен, повтор");
      lastWebhookLog = now;
    }
    setTimeout(() => notify(payload), WEBHOOK_RETRY_MS);
  }
}

async function socketOf(number) {
  if (!sockets.get(number)?.sock) await connect(number);
  return sockets.get(number).sock;
}

const routes = {
  // type=text — единственный вид боевой отправки; audio/image нужны прогреву,
  // которому требуется разнообразный контент. Медиа приезжает как путь к файлу
  // внутри node/media/, потому что Python не должен гонять байты через ручку.
  "POST /send": async ({ number, to, text, type = "text", key }) => {
    const sock = await socketOf(number);
    const content = {
      text: { text },
      image: { image: { url: text }, caption: "" },
      audio: { audio: { url: text }, ptt: true },
    }[type];
    if (!content) return { ok: false, sent: false, error: `unknown type ${type}` };
    try {
      const sent = await sock.sendMessage(jid(to), content);
      log.info({ number, to, key }, "отправлено");
      return { ok: true, sent: true, provider_id: sent.key.id };
    } catch (error) {
      return { ok: false, sent: false, error: String(error) };
    }
  },
  "POST /check": async ({ number, to }) => {
    const sock = await socketOf(number);
    const [found] = await sock.onWhatsApp(jid(to));
    return { has_whatsapp: Boolean(found?.exists) };
  },
  "POST /pair": async ({ number }) => {
    const sock = await socketOf(number);
    return { code: await sock.requestPairingCode(number.replace(/\D/g, "")) };
  },
};

function healthReport() {
  const report = {};
  for (const [number, entry] of sockets) {
    report[number] = { state: entry.state, reconnects: entry.reconnects };
  }
  return report;
}

http.createServer(async (request, response) => {
  const route = `${request.method} ${request.url}`;
  const reply = (code, body) => {
    response.writeHead(code, { "content-type": "application/json" });
    response.end(JSON.stringify(body));
  };
  if (route === "GET /health") return reply(200, healthReport());
  const handler = routes[route];
  if (!handler) return reply(404, { error: "no such route" });
  try {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    reply(200, await handler(JSON.parse(Buffer.concat(chunks).toString() || "{}")));
  } catch (error) {
    log.error({ error: String(error), route }, "ручка упала");
    reply(500, { error: String(error) });
  }
}).listen(PORT, () => log.info({ port: PORT }, "sender-node слушает"));
