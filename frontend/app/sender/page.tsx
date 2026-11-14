"use client";

/** Доставка: пул номеров с днём прогрева, сокетом и остатком дневного лимита,
 * очередь исходящих с автором строки и переключатель автопилота. Содержимое
 * целиком приезжает с бэкенда (GET /api/sender), страница не знает ни
 * календаря прогрева, ни правил режима. SSE нет: страница опрашивает API.
 */

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  AUTOPILOT_LABELS,
  cancelRow,
  confirmRow,
  fetchQueue,
  fetchSender,
  liftQuarantine,
  markWarmed,
  pairNumber,
  qrNumber,
  registerNumber,
  setAutopilot,
  type AutopilotMode,
  type QueueRow,
  type SenderNumber,
  type SenderStatus,
} from "../api";

const POLL_MS = 10_000;

const PHASE_LABEL: Record<string, string> = {
  socket_delay: "сокет не привязан",
  passive: "только входящие",
  internal: "внутренний прогрев",
  cold: "боевые касания",
};

const NUMBER_STATUS: Record<SenderNumber["status"], string> = {
  new: "новый",
  warming: "греется",
  active: "боевой",
  quarantined: "карантин",
  banned: "бан",
};

const KIND_LABEL: Record<string, string> = {
  cold: "первое письмо",
  followup: "касание",
  reply: "ответ",
  warmup: "прогрев",
};

const ROW_STATUS: Record<string, string> = {
  pending: "ждёт",
  sending: "уходит",
  sent: "ушло",
  failed: "не ушло",
  cancelled: "отменено",
  stuck: "судьба неизвестна",
};

const AUTHOR_LABEL: Record<QueueRow["author"], string> = {
  operator: "оператор",
  automat: "автомат",
};

const ERROR_TEXT: Record<string, string> = {
  "errors.sessionLoggedOut": "Сессия номера отвязана в WhatsApp — привяжите номер заново: QR или код.",
  "errors.transportFailed": "WhatsApp-сервис (Node) не ответил — проверьте, что он запущен.",
  "errors.numberExists": "Этот номер уже в пуле.",
  "errors.phoneInvalid": "Это не похоже на номер телефона.",
  "errors.rowNotWaiting": "Строка уже не ждёт: ушла, отменена или подтверждена.",
};

const WHEN = new Intl.DateTimeFormat("ru", {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

type Pairing = { number: string; qr?: string; code?: string };

export default function Sender() {
  const [status, setStatus] = useState<SenderStatus | null>(null);
  const [queue, setQueue] = useState<QueueRow[]>([]);
  const [recent, setRecent] = useState<QueueRow[]>([]);
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [number, setNumber] = useState("");
  const [skipWarmup, setSkipWarmup] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    fetchSender().then(setStatus).catch(() => undefined);
    fetchQueue()
      .then((data) => {
        setQueue(data.queue);
        // A pending row is shown once, under «Ждут отправки».
        const waiting = new Set(data.queue.map((row) => row.outbox_id));
        setRecent(data.recent.filter((row) => !waiting.has(row.outbox_id)));
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    reload();
    const timer = setInterval(reload, POLL_MS);
    return () => clearInterval(timer);
  }, [reload]);

  /** Отказ кнопки приезжает словами: без этого она молча ничего не делает. */
  const report = (failure: unknown) => {
    const code = failure instanceof ApiError ? failure.code : undefined;
    setError((code && ERROR_TEXT[code]) || (failure instanceof Error ? failure.message : String(failure)));
  };

  const act = (work: () => Promise<unknown>) => {
    setError(null);
    work().then(reload).catch(report);
  };

  const showQr = (target: string) =>
    act(() => qrNumber(target).then((body) => setPairing({ number: target, qr: body.qr })));
  const showCode = (target: string) =>
    act(() => pairNumber(target).then((body) => setPairing({ number: target, code: body.code })));

  return (
    <>
      <header className="page-head">
        <h1>Отправка</h1>
        <span className="page-sub">
          автопилот: {status ? AUTOPILOT_LABELS[status.autopilot] ?? status.autopilot : "…"}
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
          {(Object.entries(AUTOPILOT_LABELS) as [AutopilotMode, string][]).map(([mode, label]) => (
            <button
              key={mode}
              className={status?.autopilot === mode ? "btn" : "btn-secondary"}
              aria-pressed={status?.autopilot === mode}
              onClick={() => act(() => setAutopilot(mode))}
            >
              {label}
            </button>
          ))}
        </div>
        <p className="note">
          «Стоп» держит всю очередь, кроме прогрева. «Ручной» отправляет только то, что вы
          поставили сами; строки, которые автомат поставил раньше, ждут «Отправить». «Только
          ответы» сам отправляет ответы лидам, «Полный автопилот» — ещё и первые письма и
          касания. Режим — та же настройка, что в «Настройках» → «Отправка».
        </p>
        {error && <p className="error-line" role="alert">{error}</p>}

        <h3>Ждут отправки</h3>
        {queue.length === 0 ? (
          <p className="page-sub">Очередь пуста.</p>
        ) : (
          <QueueTable rows={queue} act={act} />
        )}
        {recent.length > 0 && (
          <>
            <h3>Последние</h3>
            <QueueTable rows={recent} />
          </>
        )}
      </section>

      <section className="card">
        <h2>Пул номеров</h2>
        <div className="settings-table">
          <table className="table stack-table">
            <thead>
              <tr>
                <th>Номер</th><th>Статус</th><th>День</th><th>Фаза</th>
                <th>Сегодня</th><th>Осталось</th><th></th>
              </tr>
            </thead>
            <tbody>
              {(status?.numbers ?? []).map((row) => (
                <tr key={row.number}>
                  <td data-label="Номер" className="mono">{row.number}</td>
                  <td data-label="Статус">
                    {NUMBER_STATUS[row.status] ?? row.status}
                    {row.session === "loggedOut" && (
                      <div>
                        <span className="tone is-stop">отвязан — привяжите заново</span>
                      </div>
                    )}
                    {row.note && <div className="page-sub">{row.note}</div>}
                  </td>
                  <td data-label="День">{row.day}</td>
                  <td data-label="Фаза">{PHASE_LABEL[row.phase] ?? row.phase}</td>
                  <td data-label="Сегодня">{row.sent_today} / {row.daily_limit}</td>
                  <td data-label="Осталось">{row.capacity}</td>
                  <td>
                    <div className="row-actions">
                      {row.status === "quarantined" && (
                        <button className="btn" onClick={() => act(() => liftQuarantine(row.number))}>
                          Снять карантин
                        </button>
                      )}
                      <button className="btn-secondary" onClick={() => showQr(row.number)}>
                        QR
                      </button>
                      <button className="btn-secondary" onClick={() => showCode(row.number)}>
                        Код
                      </button>
                      {!row.skip_warmup && (
                        <button className="btn-secondary" onClick={() => act(() => markWarmed(row.number))}>
                          Уже прогрет
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {pairing && <PairingHint pairing={pairing} />}
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
                  <td>{ROW_STATUS[row.status] ?? row.status}</td>
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
          aria-label="Номер"
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
        <div className="row-actions">
          <button
            className="btn"
            onClick={() => act(() => registerNumber(number, skipWarmup))}
            disabled={!number}
          >
            Добавить в пул
          </button>
        </div>
      </section>
    </>
  );
}

/** Строки очереди; с `act` — с кнопками: «Отправить» у ждущей строки автомата,
 *  «Отменить» у любой ждущей. */
function QueueTable({ rows, act }: { rows: QueueRow[]; act?: (work: () => Promise<unknown>) => void }) {
  return (
    <div className="settings-table">
      <table className="table stack-table">
        <thead>
          <tr>
            <th>Тред</th><th>Вид</th><th>Автор</th><th>Номер</th><th>Статус</th><th>Не раньше</th>
            {act && <th></th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.outbox_id}>
              <td data-label="Тред" className="mono">{row.thread_id ?? "прогрев"}</td>
              <td data-label="Вид">{KIND_LABEL[row.kind] ?? row.kind}</td>
              <td data-label="Автор">{AUTHOR_LABEL[row.author] ?? row.author}</td>
              <td data-label="Номер" className="mono">{row.our_number}</td>
              <td data-label="Статус">
                {ROW_STATUS[row.status] ?? row.status}
                {row.error && <div className="page-sub">{row.error}</div>}
              </td>
              <td data-label="Не раньше">{WHEN.format(new Date(row.send_after))}</td>
              {act && (
                <td>
                  {row.status === "pending" && (
                    <div className="row-actions">
                      {row.author === "automat" && (
                        <button className="btn" onClick={() => act(() => confirmRow(row.outbox_id))}>
                          Отправить
                        </button>
                      )}
                      <button className="btn-secondary" onClick={() => act(() => cancelRow(row.outbox_id))}>
                        Отменить
                      </button>
                    </div>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PairingHint({ pairing }: { pairing: Pairing }) {
  return (
    <div className="pairing">
      <p>
        <span className="mono">{pairing.number}</span>: WhatsApp → Связанные устройства →
        Привязать устройство →{" "}
        {pairing.qr ? "отсканировать:" : "Привязать по номеру телефона и ввести код:"}
      </p>
      {pairing.qr && (
        <img src={pairing.qr} alt="QR-код привязки WhatsApp" width={264} height={264} />
      )}
      {pairing.code && <p className="pairing-code mono">{pairing.code}</p>}
    </div>
  );
}
