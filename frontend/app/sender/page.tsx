"use client";

/** Система 3: отправка. Пул номеров с днём прогрева и остатком дневного
 * лимита, очередь исходящих и kill switch автопилота. Содержимое по-прежнему
 * целиком приезжает с бэкенда (GET /api/sender), страница не знает ни
 * календаря прогрева, ни порогов.
 */

import { useCallback, useEffect, useState } from "react";
import {
  fetchQueue,
  fetchSender,
  liftQuarantine,
  markWarmed,
  qrNumber,
  registerNumber,
  setAutopilot,
  type QueueRow,
  type SenderStatus,
} from "../api";
import { useLive } from "@/components/live";

const PHASE_LABEL: Record<string, string> = {
  socket_delay: "сокет не привязан",
  passive: "только входящие",
  internal: "внутренний прогрев",
  cold: "боевые касания",
};

const AUTOPILOT_LABEL = {
  off: "Стоп",
  replies: "Только ответы",
  full: "Полный автопилот",
} as const;

const WHEN = new Intl.DateTimeFormat("ru", {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

export default function Sender() {
  const { refreshTick } = useLive();
  const [status, setStatus] = useState<SenderStatus | null>(null);
  const [recent, setRecent] = useState<QueueRow[]>([]);
  const [qr, setQr] = useState<string | null>(null);
  const [number, setNumber] = useState("");
  const [skipWarmup, setSkipWarmup] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    fetchSender().then(setStatus).catch(() => undefined);
  }, []);

  useEffect(reload, [reload, refreshTick]);

  useEffect(() => {
    fetchQueue().then((data) => setRecent(data.recent)).catch(() => undefined);
  }, [refreshTick]);

  /** Дубликат номера и лежащий Node приезжают сюда ошибкой запроса: без этого
   * кнопка молча ничего не делает, а в консоли висит unhandled rejection. */
  const report = (failure: unknown) => setError(String(failure));

  return (
    <>
      <header className="page-head">
        <h1>Отправка</h1>
        <span className="page-sub">
          система 3 · автопилот: {status?.autopilot ?? "…"}
        </span>
      </header>

      <section className="card">
        <h2>Очередь</h2>
        <div className="counters">
          <span>
            в очереди <b>{status?.queue.queued ?? 0}</b>
          </span>
          <span>
            отправлено сегодня <b>{status?.queue.sent_today ?? 0}</b>
          </span>
          <span className={status && status.queue.overdue > 0 ? "has-replies" : ""}>
            созрели, но стоят <b>{status?.queue.overdue ?? 0}</b>
          </span>
          <span className={status && status.threads.waiting > 0 ? "has-replies" : ""}>
            ждут ответа <b>{status?.threads.waiting ?? 0}</b>
          </span>
          <span className={status && status.threads.escalated > 0 ? "has-replies" : ""}>
            эскалировано <b>{status?.threads.escalated ?? 0}</b>
          </span>
          <span>
            пульс воркера{" "}
            <b>{status?.heartbeat ? WHEN.format(new Date(status.heartbeat)) : "—"}</b>
          </span>
        </div>
        <div className="autopilot">
          {(["off", "replies", "full"] as const).map((mode) => (
            <button
              key={mode}
              className="btn"
              aria-current={status?.autopilot === mode}
              onClick={() => {
                setError(null);
                setAutopilot(mode).then(reload).catch(report);
              }}
            >
              {AUTOPILOT_LABEL[mode]}
            </button>
          ))}
        </div>
        {recent.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>Сообщение</th><th>Вид</th><th>Номер</th><th>Статус</th><th>Не раньше</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((row) => (
                <tr key={row.outbox_id}>
                  <td className="mono">{row.thread_id ?? "прогрев"}</td>
                  <td>{row.kind}</td>
                  <td className="mono">{row.our_number}</td>
                  <td>{row.status}</td>
                  <td>{WHEN.format(new Date(row.send_after))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h2>Пул номеров</h2>
        <table className="table">
          <thead>
            <tr>
              <th>Номер</th><th>Статус</th><th>День</th><th>Фаза</th>
              <th>Сегодня</th><th>Осталось</th><th></th>
            </tr>
          </thead>
          <tbody>
            {(status?.numbers ?? []).map((row) => (
              <tr key={row.number}>
                <td className="mono">{row.number}</td>
                <td>{row.status}</td>
                <td>{row.day}</td>
                <td>{PHASE_LABEL[row.phase] ?? row.phase}</td>
                <td>{row.sent_today} / {row.daily_limit}</td>
                <td>{row.capacity}</td>
                <td>
                  {row.status === "quarantined" && (
                    <button
                      className="btn"
                      onClick={() => {
                        setError(null);
                        liftQuarantine(row.number).then(reload).catch(report);
                      }}
                    >
                      Снять карантин
                    </button>
                  )}
                  <button
                    className="btn"
                    onClick={() => {
                      setError(null);
                      setNumber(row.number);
                      qrNumber(row.number).then((body) => setQr(body.qr)).catch(report);
                    }}
                  >
                    QR
                  </button>
                  {!row.skip_warmup && (
                    <button
                      className="btn"
                      onClick={() => {
                        setError(null);
                        markWarmed(row.number).then(reload).catch(report);
                      }}
                    >
                      Уже прогрет
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h2>Прогрев</h2>
        <table className="table">
          <thead>
            <tr>
              <th>Фаза</th><th>Дни</th><th>Лимит в день</th>
            </tr>
          </thead>
          <tbody>
            {(status?.warmup_calendar ?? []).map((row) => (
              <tr key={row.phase}>
                <td>{PHASE_LABEL[row.phase] ?? row.phase}</td>
                <td>{row.days}</td>
                <td>{row.daily_limit}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {status && status.warmup_log.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th>Когда</th><th>Кто → кому</th><th>Статус</th>
              </tr>
            </thead>
            <tbody>
              {status.warmup_log.map((row) => (
                <tr key={row.outbox_id}>
                  <td>{WHEN.format(new Date(row.updated_at))}</td>
                  <td className="mono">{row.our_number} → {row.recipient}</td>
                  <td>{row.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h2>Подключить номер</h2>
        <input
          className="input mono"
          placeholder="+77001112233"
          value={number}
          onChange={(event) => setNumber(event.target.value)}
        />
        <label>
          <input
            type="checkbox"
            checked={skipWarmup}
            onChange={(event) => setSkipWarmup(event.target.checked)}
          />
          {" "}уже прогрет — сразу боевой, без рампы
        </label>
        <button
          className="btn"
          onClick={() => {
            setError(null);
            registerNumber(number, skipWarmup).then(reload).catch(report);
          }}
          disabled={!number}
        >
          Добавить в пул
        </button>
        <button
          className="btn"
          onClick={() => {
            setError(null);
            qrNumber(number).then((body) => setQr(body.qr)).catch(report);
          }}
          disabled={!number}
        >
          Показать QR
        </button>
        {error && <p className="error-line">{error}</p>}
        {qr && (
          <p>
            WhatsApp → Связанные устройства → Привязать устройство → отсканировать:
            <br />
            <img src={qr} alt="QR-код привязки WhatsApp" width={264} height={264} />
          </p>
        )}
      </section>
    </>
  );
}
