"use client";

/** Песочница: я играю лида, продукт работает по-настоящему.

 Слева лента с судьбой каждого сообщения, справа пульт: часы, аварии, прогоны.
 Ничего своего страница не решает — все состояния приезжают с бэкенда.
 */

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  activateSandboxRun,
  createSandboxRun,
  fetchActivity,
  fetchSandboxChat,
  fetchSandboxFaults,
  fetchSandboxRuns,
  fetchSender,
  moveSandboxClock,
  queueMessage,
  requestDraft,
  sendSandboxIncoming,
  setAutopilot,
  setSandboxFaults,
  type ActivityEvent,
  type SandboxBubble,
  type SandboxChat,
  type SandboxFaults,
  type SandboxRun,
  type SenderStatus,
} from "../api";
import { useLive } from "@/components/live";

const WHEN = new Intl.DateTimeFormat("ru", {
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

const KIND_LABELS: Record<SandboxBubble["kind"], string> = {
  draft: "черновик",
  queued: "в очереди",
  sent: "отправлено",
  incoming: "",
};

const SHIFTS = [
  { preset: "jitter", label: "+15 мин" },
  { preset: "hour", label: "+1 час" },
  { preset: "day", label: "+1 день" },
  { preset: "window", label: "к открытию окна" },
];

const SWITCHES: { field: keyof SandboxFaults; label: string; options: string[] }[] = [
  { field: "send", label: "Отправка", options: ["ok", "not_sent", "unknown"] },
  { field: "delivery", label: "Доставка", options: ["delivered", "read", "silent"] },
  { field: "number", label: "Номер", options: ["connected", "loggedOut", "stalled"] },
];

export default function SandboxPage() {
  const { refreshTick } = useLive();
  const [runs, setRuns] = useState<SandboxRun[]>([]);
  const [chat, setChat] = useState<SandboxChat | null>(null);
  const [faults, setFaults] = useState<SandboxFaults | null>(null);
  const [company, setCompany] = useState("");
  const [warmed, setWarmed] = useState(true);
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [pool, setPool] = useState<SenderStatus | null>(null);
  const [sandboxNow, setSandboxNow] = useState<string | null>(null);
  const [journal, setJournal] = useState<ActivityEvent[]>([]);

  const reload = useCallback(() => {
    fetchSandboxRuns().then((data) => setRuns(data.runs)).catch(report);
    fetchSandboxFaults().then(setFaults).catch(report);
    // 409 — это не ошибка, а состояние «сначала черновик». Всё остальное
    // (500, обрыв) обязано доехать до баннера: молчащая лента выглядела бы
    // ровно так же, как пустая.
    fetchSandboxChat()
      .then(setChat)
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 409) {
          setChat(null);
          return;
        }
        report(error as Error);
      });
    fetchSender().then(setPool).catch(report);
    fetchActivity(20).then((data) => setJournal(data.events)).catch(report);
  }, []);

  function report(error: Error) {
    setFailure(error.message);
  }

  useEffect(reload, [reload, refreshTick]);

  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setFailure(null);
    try {
      await action();
      reload();
    } catch (error) {
      report(error as Error);
    } finally {
      setBusy(false);
    }
  }

  const active = runs.find((run) => run.active) ?? null;
  // Ставится последний непоставленный черновик: очередь принимает текст, а
  // какой именно — единственный вопрос, на который лента уже отвечает.
  const draft = chat?.bubbles.filter((bubble) => bubble.kind === "draft").at(-1) ?? null;

  return (
    <>
      <header className="page-head">
        <h1>Песочница</h1>
        <span className="page-sub">
          Я лид, продукт настоящий: транспорт подменён, часы двигаются кнопкой.
        </span>
      </header>

      {failure && <div className="failure">{failure}</div>}

      <div className="sandbox-runs">
        <select
          className="input"
          value={active?.run_id ?? ""}
          onChange={(event) => act(() => activateSandboxRun(event.target.value))}
        >
          <option value="" disabled>
            прогон не выбран
          </option>
          {runs.map((run) => (
            <option key={run.run_id} value={run.run_id}>
              {run.company_id} · {WHEN.format(new Date(run.created_at))}
              {run.warmed ? "" : " · номер новый"}
            </option>
          ))}
        </select>
        <input
          className="input"
          placeholder="company_id из выдачи"
          value={company}
          onChange={(event) => setCompany(event.target.value)}
        />
        <label className="sandbox-check">
          <input
            type="checkbox"
            checked={warmed}
            onChange={(event) => setWarmed(event.target.checked)}
          />
          номер прогрет
        </label>
        <button
          className="btn"
          disabled={busy || !company}
          onClick={() => act(() => createSandboxRun(company, warmed))}
        >
          Новый прогон
        </button>
      </div>

      <div className="split">
        <div className="detail">
          <ol className="thread-log">
            {chat?.bubbles.map((bubble) => (
              <li
                key={bubble.message_id}
                className={bubble.role === "incoming" ? "thread-reply" : ""}
              >
                <span className="thread-who">
                  {bubble.role === "incoming" ? "лид" : "мы"}
                  {KIND_LABELS[bubble.kind] && (
                    <span className="message-kind"> · {KIND_LABELS[bubble.kind]}</span>
                  )}
                </span>
                <p className="row-text">{bubble.text}</p>
                <span className="sandbox-fate mono">
                  {WHEN.format(new Date(bubble.at))}
                  {bubble.fate && ` · ${fateOf(bubble.fate)}`}
                </span>
              </li>
            ))}
            {chat === null && (
              <li className="placeholder">
                Треда ещё нет. Создайте прогон и нажмите «Черновик первого письма».
              </li>
            )}
          </ol>

          <div className="composer">
            <textarea
              className="input"
              rows={3}
              placeholder="ответить как лид (пусто = медиа без текста)"
              value={reply}
              onChange={(event) => setReply(event.target.value)}
            />
            <div className="composer-actions">
              <button
                className="btn"
                disabled={busy || chat === null}
                onClick={() =>
                  act(async () => {
                    await sendSandboxIncoming(reply);
                    setReply("");
                  })
                }
              >
                Отправить как лид
              </button>
              <button
                className="btn-secondary"
                disabled={busy || !active}
                onClick={() => act(() => requestDraft(active!.company_id, "first"))}
              >
                Черновик первого письма
              </button>
              <button
                className="btn-secondary"
                disabled={busy || !draft || !chat}
                onClick={() => act(() => queueMessage(chat!.thread_id, draft!.text))}
              >
                Поставить в очередь
              </button>
            </div>
          </div>
        </div>

        <aside className="roster">
          <section className="card">
            <h2 className="card-sub">Прогон</h2>
            {chat ? (
              <ul className="sandbox-facts">
                <li>
                  Тред <span className="mono">{chat.thread_id}</span>
                </li>
                <li>Этап: {chat.stage}</li>
                <li>Статус: {chat.status}</li>
                <li>Касаний: {chat.touch_no}</li>
                <li>
                  Следующее касание:{" "}
                  {chat.next_touch_at
                    ? WHEN.format(new Date(chat.next_touch_at))
                    : "не запланировано"}
                </li>
              </ul>
            ) : (
              <p className="placeholder">Тред не открыт.</p>
            )}
          </section>

          <section className="card">
            <h2 className="card-sub">Часы</h2>
            <p className="note mono">
              сдвиг: {shiftOf(active?.offset_seconds ?? 0)}
              {sandboxNow && ` · сейчас ${WHEN.format(new Date(sandboxNow))}`}
            </p>
            <div className="controls">
              {SHIFTS.map(({ preset, label }) => (
                <button
                  key={preset}
                  className="btn-quiet"
                  disabled={busy || !active}
                  onClick={() =>
                    act(async () => {
                      const moved = await moveSandboxClock(preset);
                      setSandboxNow(moved.now);
                    })
                  }
                >
                  {label}
                </button>
              ))}
            </div>
          </section>

          <section className="card">
            <h2 className="card-sub">Аварии</h2>
            {faults &&
              SWITCHES.map(({ field, label, options }) => (
                <label key={field} className="sandbox-switch">
                  {label}
                  <select
                    className="input"
                    value={String(faults[field])}
                    onChange={(event) =>
                      act(() => setSandboxFaults({ [field]: event.target.value }))
                    }
                  >
                    {options.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
            {faults && (
              <label className="sandbox-check">
                <input
                  type="checkbox"
                  checked={faults.has_whatsapp}
                  onChange={(event) =>
                    act(() => setSandboxFaults({ has_whatsapp: event.target.checked }))
                  }
                />
                у лида есть WhatsApp
              </label>
            )}
          </section>

          <section className="card">
            <h2 className="card-sub">Пул</h2>
            <ul className="sandbox-facts">
              {pool?.numbers.map((number) => (
                <li key={number.number}>
                  <span className="mono">{number.number}</span> · {number.status} ·
                  день {number.day} ({number.phase}) · {number.sent_today} из{" "}
                  {number.daily_limit}
                </li>
              ))}
              {pool?.numbers.length === 0 && (
                <li className="placeholder">Номеров в прогоне нет.</li>
              )}
            </ul>
          </section>

          <section className="card">
            <h2 className="card-sub">Журнал</h2>
            <ul className="sandbox-facts">
              {journal.map((event) => (
                <li key={`${event.actor}-${event.at}-${event.outcome}`}>
                  <span className="mono">{WHEN.format(new Date(event.last_at))}</span>{" "}
                  {event.actor} · {event.outcome}
                  {event.repeats > 1 && ` ×${event.repeats}`}
                </li>
              ))}
              {journal.length === 0 && (
                <li className="placeholder">Демоны ещё не отчитывались.</li>
              )}
            </ul>
          </section>

          <section className="card">
            <h2 className="card-sub">Автопилот</h2>
            <div className="controls">
              {(["off", "replies", "full"] as const).map((mode) => (
                <button
                  key={mode}
                  className={pool?.autopilot === mode ? "btn" : "btn-quiet"}
                  disabled={busy}
                  onClick={() => act(() => setAutopilot(mode))}
                >
                  {mode}
                </button>
              ))}
            </div>
            <p className="note">
              Тот же файл-переключатель, что в бою: второй копии kill switch нет.
            </p>
          </section>
        </aside>
      </div>
    </>
  );
}

/** Сдвиг часов словами. Округление до часов прятало бы пресет «+15 мин»:
 *  три нажатия подряд показывали бы «0 ч», и кнопка выглядела бы сломанной. */
function shiftOf(seconds: number): string {
  if (seconds === 0) return "нет";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return [days && `${days} д`, hours && `${hours} ч`, minutes && `${minutes} мин`]
    .filter(Boolean)
    .join(" ");
}

/** Судьба строки очереди словами. Словарь бэкенда — статусы outbox. */
function fateOf(fate: NonNullable<SandboxBubble["fate"]>): string {
  if (fate.read_at) return "прочитано";
  if (fate.delivered_at) return "доставлено";
  if (fate.status === "failed") return `не ушло, попыток ${fate.attempts}`;
  if (fate.status === "stuck") return "stuck — разбирает человек";
  if (fate.status === "cancelled") return "отменено";
  if (fate.status === "sent") return "отправлено, доставки нет";
  return `в очереди с ${WHEN.format(new Date(fate.send_after))}`;
}
