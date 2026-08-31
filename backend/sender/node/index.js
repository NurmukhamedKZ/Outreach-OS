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
const WEBHOOK_SECRET = process.env.SENDER_WEBHOOK_SECRET ?? "";
const SESSIONS = path.resolve("sessions");
const WEBHOOK_RETRY_MS = 5000;
const WEBHOOK_LOG_INTERVAL_MS = 60000;
const WEBHOOK_MAX_ATTEMPTS = 12;      // ~час с учётом backoff, потом событие теряется
const RECONNECT_BASE_MS = 5000;
const RECONNECT_MAX_MS = 300000;      // пять минут между попытками — потолок
const RECONNECT_MAX_ATTEMPTS = 12;    // дальше молчим: связь чинит человек

const log = pino({ level: "info" });
// number -> {sock, state, reconnects, day, attempts}
const sockets = new Map();
// key идемпотентности -> provider_id уже отправленного сообщения
const sentKeys = new Map();
let lastWebhookLog = 0;

const today = () => new Date().toISOString().slice(0, 10);

// Счётчик реконнектов суточный, а не за всё время работы процесса: Python
// сравнивает его с health.reconnects_per_day_alert, и накопительный счётчик
// отправил бы в карантин весь пул за один длинный аптайм.
function countReconnect(entry) {
  const day = today();
  return entry.day === day
    ? { day, reconnects: entry.reconnects + 1 }
    : { day, reconnects: 1 };
}

const jid = (number) => `${number.replace(/\D/g, "")}@s.whatsapp.net`;

async function connect(number) {
  const { state, saveCreds } = await useMultiFileAuthState(path.join(SESSIONS, number));
  const sock = makeWASocket({ auth: state, logger: log.child({ number }) });
  const entry = sockets.get(number) ?? { reconnects: 0, day: today(), attempts: 0 };
  sockets.set(number, { ...entry, sock, state: "connecting" });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    const current = sockets.get(number);
    if (update.connection === "open") {
      sockets.set(number, { ...current, state: "connected", attempts: 0 });
    }
    if (update.connection === "close") {
      const code = update.lastDisconnect?.error?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;
      // loggedOut не переподключается: сессия мертва, номер требует телефона.
      if (loggedOut) {
        sockets.set(number, { ...current, state: "loggedOut" });
      } else {
        const attempts = current.attempts + 1;
        const exhausted = attempts > RECONNECT_MAX_ATTEMPTS;
        sockets.set(number, {
          ...current,
          ...countReconnect(current),
          state: exhausted ? "stalled" : "reconnecting",
          attempts,
        });
        // Экспоненциальный backoff с потолком и капом попыток. Плоские пять
        // секунд давали бы ~17k подключений в сутки с одного адреса на номер,
        // которому не ввели pairing code, — само по себе повод для бана.
        if (exhausted) {
          log.error({ number, attempts }, "сокет не поднимается, дальше — руками");
        } else {
          const delay = Math.min(RECONNECT_BASE_MS * 2 ** (attempts - 1), RECONNECT_MAX_MS);
          setTimeout(() => connect(number), delay);
        }
      }
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
async function notify(payload, attempt = 1) {
  try {
    const response = await fetch(`${PYTHON_URL}/api/sender/webhook`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(WEBHOOK_SECRET ? { "x-sender-secret": WEBHOOK_SECRET } : {}),
      },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  } catch (error) {
    const now = Date.now();
    if (now - lastWebhookLog > WEBHOOK_LOG_INTERVAL_MS) {
      log.warn({ error: String(error), payload }, "вебхук не доставлен, повтор");
      lastWebhookLog = now;
    }
    if (attempt >= WEBHOOK_MAX_ATTEMPTS) {
      log.error({ payload }, "вебхук так и не доставлен, событие потеряно");
      return;
    }
    const delay = Math.min(WEBHOOK_RETRY_MS * 2 ** (attempt - 1), RECONNECT_MAX_MS);
    setTimeout(() => notify(payload, attempt + 1), delay);
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
    // Ключ идемпотентности: повтор с тем же key не отправляет второе сообщение,
    // а возвращает id первого. Дубликат в холодном аутриче — прямой повод
    // нажать Report, и защита от него не может жить только на стороне Python.
    if (key && sentKeys.has(key)) {
      log.info({ number, to, key }, "повтор по ключу, второй отправки нет");
      return { ok: true, sent: true, provider_id: sentKeys.get(key) };
    }
    const sock = await socketOf(number);
    const content = {
      text: { text },
      image: { image: { url: text }, caption: "" },
      audio: { audio: { url: text }, ptt: true },
    }[type];
    if (!content) return { ok: false, sent: false, error: `unknown type ${type}` };
    try {
      const sent = await sock.sendMessage(jid(to), content);
      if (key) sentKeys.set(key, sent.key.id);
      log.info({ number, to, key }, "отправлено");
      return { ok: true, sent: true, provider_id: sent.key.id };
    } catch (error) {
      // sent:false — обещание «фрейм в сокет не ушёл», на нём Python строит
      // безопасный ретрай. Дать его можно, только когда сокет и не был поднят;
      // ошибка на живом сокете (например, таймаут подтверждения) означает
      // «неизвестно», и это 500 -> TransportError -> разбирается человеком.
      if (sockets.get(number)?.state !== "connected") {
        return { ok: false, sent: false, error: String(error) };
      }
      throw error;
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
