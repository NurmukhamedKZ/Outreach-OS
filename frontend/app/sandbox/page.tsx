"use client";

/** Песочница: оператор играет лида, продукт работает по-настоящему.

 Экран ведёт по шагам, а не выкладывает кнопки рядом: порядок «прогон →
 черновик → очередь → доставка → ответ» знает только тот, кто писал систему,
 и держать его в голове оператора значит объяснять заново каждый раз.

 Ни одно состояние здесь не придумано фронтом: этап, статус, судьба строки
 очереди и фаза прогрева приезжают с бэкенда. Фронт переводит их на русский —
 словари ниже, — но не решает, что они значат.
 */

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  activateSandboxRun,
  createSandboxRun,
  fetchActivity,
  fetchLeads,
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
  type Lead,
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

/** Сколько лидов предлагать в выборе: прогон делается по одному, и список
 *  длиннее полусотни оператор всё равно не просматривает. */
const LEADS_IN_PICKER = 50;

// ---- Словари: как называется по-русски то, что бэкенд хранит словом ----

const KIND_LABELS: Record<SandboxBubble["kind"], string> = {
  draft: "черновик — ещё не отправлено",
  queued: "подтверждено, ждёт отправки",
  sent: "ушло лиду",
  incoming: "",
};

const STAGE_LABELS: Record<string, string> = {
  contact: "знакомство",
  probing: "выясняем задачу",
  offer: "предложение",
  closing: "договариваемся о созвоне",
};

const THREAD_STATUS_LABELS: Record<string, string> = {
  queued: "письмо ушло, ответа нет",
  active: "живой диалог",
  exhausted: "касания кончились",
  escalated: "передан человеку",
  unreachable: "не дозвониться",
  closed_refused: "лид отказался",
  closed_junk: "не наш клиент",
  blocked_channel: "нет свободного номера",
};

const NUMBER_STATUS_LABELS: Record<string, string> = {
  new: "новый",
  warming: "прогревается",
  active: "боевой",
  quarantined: "карантин",
  banned: "забанен",
};

const PHASE_LABELS: Record<string, string> = {
  socket_delay: "первые сутки молчит",
  passive: "только принимает входящие",
  internal: "пишет своим номерам",
  cold: "пишет лидам",
};

// Тумблеры аварий: подпись говорит не как режим называется, а что случится.
const SWITCHES: {
  field: keyof SandboxFaults;
  label: string;
  options: { value: string; text: string }[];
}[] = [
  {
    field: "send",
    label: "Отправка",
    options: [
      { value: "ok", text: "уходит" },
      { value: "not_sent", text: "не уходит — ретрай 1/5/30 мин, потом отказ" },
      { value: "unknown", text: "неизвестно — тред уйдёт человеку" },
    ],
  },
  {
    field: "delivery",
    label: "Доставка",
    options: [
      { value: "delivered", text: "доставлено" },
      { value: "read", text: "прочитано" },
      { value: "silent", text: "статуса нет — тред застрянет в «ответа нет»" },
    ],
  },
  {
    field: "number",
    label: "Наш номер",
    options: [
      { value: "connected", text: "на связи" },
      { value: "loggedOut", text: "разлогинен — монитор уведёт в карантин" },
      { value: "stalled", text: "сокет не поднимается" },
    ],
  },
];

const SHIFTS = [
  { preset: "jitter", label: "+15 мин", why: "пережить джиттер перед отправкой" },
  { preset: "hour", label: "+1 час", why: "просто вперёд" },
  { preset: "day", label: "+1 день", why: "дожить до follow-up" },
  { preset: "window", label: "к открытию окна", why: "если сейчас не 10:00–18:00" },
];

// ---- Шаги. Текущий вычисляется из состояния, а не запоминается кнопками ----

const STEPS = [
  { key: "run", title: "Прогон", hint: "Выберите лида из выдачи и начните прогон." },
  {
    key: "draft",
    title: "Черновик",
    hint: "Агент напишет первое письмо по досье лида. Это платный вызов модели.",
  },
  {
    key: "queue",
    title: "Очередь",
    hint: "Прочитайте текст и поставьте в очередь — дальше решают гейты.",
  },
  {
    key: "delivery",
    title: "Доставка",
    hint: "Воркер просыпается раз в 20 секунд. Если окно закрыто или ждёт джиттер — двиньте часы стенда.",
  },
  {
    key: "reply",
    title: "Ответ лида",
    hint: "Напишите то, что ответил бы лид. Дальше отвечает агент-продавец.",
  },
];

export default function SandboxPage() {
  const { refreshTick } = useLive();
  const [runs, setRuns] = useState<SandboxRun[]>([]);
  const [leads, setLeads] = useState<Lead[]>([]);
  const [chat, setChat] = useState<SandboxChat | null>(null);
  const [faults, setFaults] = useState<SandboxFaults | null>(null);
  const [company, setCompany] = useState("");
  const [warmed, setWarmed] = useState(true);
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [pool, setPool] = useState<SenderStatus | null>(null);
  const [journal, setJournal] = useState<ActivityEvent[]>([]);
  const [sandboxNow, setSandboxNow] = useState<string | null>(null);

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

  // Выдача читается один раз: пока идёт прогон, она не меняется.
  useEffect(() => {
    fetchLeads(LEADS_IN_PICKER, "")
      .then((data) => setLeads(data.leads))
      .catch(report);
  }, []);

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
  const outgoing = chat?.bubbles.filter((bubble) => bubble.role === "outgoing") ?? [];
  const last = outgoing.at(-1) ?? null;
  const draft = last?.kind === "draft" ? last : null;
  const step = stepOf(active, chat, last);

  return (
    <>
      <header className="page-head">
        <h1>Песочница</h1>
        <span className="page-sub">
          Прогон продукта на себе: вы отвечаете за лида, всё остальное настоящее —
          агент, гейты, очередь и прогрев. В WhatsApp не уходит ничего.
        </span>
      </header>

      {failure && <div className="failure">{failure}</div>}

      <ol className="sandbox-steps">
        {STEPS.map((item, index) => (
          <li
            key={item.key}
            className="sandbox-step"
            aria-current={index === step ? "step" : undefined}
            data-done={index < step ? "yes" : undefined}
          >
            <span className="sandbox-step-no">{index + 1}</span>
            <span className="sandbox-step-title">{item.title}</span>
          </li>
        ))}
      </ol>
      <p className="sandbox-hint">{STEPS[step].hint}</p>

      <section className="sandbox-setup">
        <label className="sandbox-field">
          <span className="sandbox-field-label">Лид для прогона</span>
          <select
            className="input"
            value={company}
            onChange={(event) => setCompany(event.target.value)}
          >
            <option value="">— выберите компанию из выдачи —</option>
            {leads.map((lead) => (
              <option key={lead.company_id} value={lead.company_id}>
                {lead.name} · {lead.city} · интент {lead.intent_score.toFixed(1)}
              </option>
            ))}
          </select>
        </label>

        <label className="sandbox-field">
          <span className="sandbox-field-label">Наш номер</span>
          <select
            className="input"
            value={warmed ? "warmed" : "new"}
            onChange={(event) => setWarmed(event.target.value === "warmed")}
          >
            <option value="warmed">прогретый — можно писать сразу</option>
            <option value="new">новый — сначала пройдёт прогрев</option>
          </select>
        </label>

        <button
          className="btn"
          disabled={busy || !company}
          onClick={() => act(() => createSandboxRun(company, warmed))}
        >
          Начать прогон
        </button>

        <label className="sandbox-field">
          <span className="sandbox-field-label">Прошлые прогоны</span>
          <select
            className="input"
            value={active?.run_id ?? ""}
            onChange={(event) => act(() => activateSandboxRun(event.target.value))}
          >
            <option value="" disabled>
              {runs.length ? "— выберите прогон —" : "прогонов ещё нет"}
            </option>
            {runs.map((run) => (
              <option key={run.run_id} value={run.run_id}>
                {nameOf(leads, run.company_id)} · {WHEN.format(new Date(run.created_at))}
                {run.warmed ? "" : " · номер новый"}
              </option>
            ))}
          </select>
        </label>
      </section>

      <div className="split split-chat">
        <div className="detail">
          <header className="cold-head">
            <b className="row-name">Чат с лидом</b>
            {chat && <span className="ghost mono">{chat.thread_id}</span>}
          </header>

          <ol className="thread-log">
            {chat?.bubbles.map((bubble) => (
              <li
                key={bubble.message_id}
                className={bubble.role === "incoming" ? "thread-reply" : ""}
              >
                <span className="thread-who">
                  {bubble.role === "incoming" ? "лид (это вы)" : "мы"}
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
                {active
                  ? "Переписки ещё нет — напишите первое письмо кнопкой ниже."
                  : "Прогон не начат. Выберите лида выше."}
              </li>
            )}
            {chat !== null && chat.bubbles.length === 0 && (
              <li className="placeholder">Переписки ещё нет.</li>
            )}
          </ol>

          <div className="composer">
            <div className="composer-actions">
              <button
                className={step === 1 ? "btn" : "btn-secondary"}
                disabled={busy || !active || step > 1}
                onClick={() => act(() => requestDraft(active!.company_id, "first"))}
              >
                Написать первое письмо
              </button>
              <button
                className={step === 2 ? "btn" : "btn-secondary"}
                disabled={busy || !draft || !chat}
                onClick={() => act(() => queueMessage(chat!.thread_id, draft!.text))}
              >
                Поставить в очередь
              </button>
            </div>

            <label className="sandbox-field">
              <span className="sandbox-field-label">
                Ваш ответ в роли лида
                <span className="note"> — пустое поле означает голосовое или фото</span>
              </span>
              <textarea
                className="input"
                rows={3}
                placeholder="например: сколько это стоит?"
                value={reply}
                onChange={(event) => setReply(event.target.value)}
              />
            </label>
            <div className="composer-actions">
              <button
                className={step === 4 ? "btn" : "btn-secondary"}
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
            </div>
          </div>
        </div>

        <aside className="roster">
          <section className="card">
            <h2 className="card-sub">Что с тредом</h2>
            {chat ? (
              <ul className="sandbox-facts">
                <li>
                  Этап: <b>{STAGE_LABELS[chat.stage] ?? chat.stage}</b>
                </li>
                <li>
                  Состояние: <b>{THREAD_STATUS_LABELS[chat.status] ?? chat.status}</b>
                </li>
                <li>Касаний сделано: {chat.touch_no}</li>
                <li>
                  Следующее касание:{" "}
                  {chat.next_touch_at
                    ? WHEN.format(new Date(chat.next_touch_at))
                    : "не запланировано"}
                </li>
              </ul>
            ) : (
              <p className="placeholder">Тред откроется вместе с первым письмом.</p>
            )}
          </section>

          <section className="card">
            <h2 className="card-sub">Часы стенда</h2>
            <p className="note">
              Двигают время всей системе: окно 10:00–18:00, джиттер перед отправкой
              и follow-up через 3 и 7 дней иначе пришлось бы ждать по-настоящему.
            </p>
            <p className="sandbox-clock">
              {sandboxNow ? (
                <span className="mono">{WHEN.format(new Date(sandboxNow))}</span>
              ) : (
                "время как в жизни"
              )}
              <span className="note"> · сдвиг {shiftOf(active?.offset_seconds ?? 0)}</span>
            </p>
            <div className="sandbox-shifts">
              {SHIFTS.map(({ preset, label, why }) => (
                <button
                  key={preset}
                  className="btn-quiet"
                  disabled={busy || !active}
                  title={why}
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
            <h2 className="card-sub">Сломать нарочно</h2>
            <p className="note">
              Так проверяются аварийные ветки: ретраи, «разбирает человек»,
              карантин номера.
            </p>
            {faults &&
              SWITCHES.map(({ field, label, options }) => (
                <label key={field} className="sandbox-switch">
                  <span className="sandbox-field-label">{label}</span>
                  <select
                    className="input"
                    value={String(faults[field])}
                    onChange={(event) =>
                      act(() => setSandboxFaults({ [field]: event.target.value }))
                    }
                  >
                    {options.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.text}
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
            <h2 className="card-sub">Наш номер</h2>
            <ul className="sandbox-facts">
              {pool?.numbers.map((number) => (
                <li key={number.number}>
                  <span className="mono">{number.number}</span> ·{" "}
                  {NUMBER_STATUS_LABELS[number.status] ?? number.status} ·{" "}
                  {PHASE_LABELS[number.phase] ?? number.phase}, день {number.day} ·
                  сегодня {number.sent_today} из {number.daily_limit}
                </li>
              ))}
              {pool !== null && pool.numbers.length === 0 && (
                <li className="placeholder">Номер появится вместе с прогоном.</li>
              )}
            </ul>
          </section>

          <section className="card">
            <h2 className="card-sub">Что делает система</h2>
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
            <p className="note">
              Тот же переключатель, что в бою. «Выключен» — очередь стоит,
              отправляет только кнопка.
            </p>
            <div className="sandbox-shifts">
              {(
                [
                  ["off", "выключен"],
                  ["replies", "только ответы"],
                  ["full", "полный"],
                ] as const
              ).map(([mode, label]) => (
                <button
                  key={mode}
                  className={pool?.autopilot === mode ? "btn" : "btn-quiet"}
                  disabled={busy}
                  onClick={() => act(() => setAutopilot(mode))}
                >
                  {label}
                </button>
              ))}
            </div>
          </section>
        </aside>
      </div>
    </>
  );
}

/** Где оператор находится прямо сейчас. Считается из данных, а не хранится:
 *  шаг, запомненный кнопкой, разъехался бы с реальностью после перезагрузки. */
function stepOf(
  run: SandboxRun | null,
  chat: SandboxChat | null,
  last: SandboxBubble | null,
): number {
  if (!run) return 0;
  if (!chat || last === null) return 1;
  if (last.kind === "draft") return 2;
  if (last.kind === "queued" || !last.fate?.delivered_at) return 3;
  return 4;
}

/** Имя компании вместо идентификатора. Пока выдача не приехала — сам
 *  идентификатор: пустая строка выглядела бы как сломанный прогон. */
function nameOf(leads: Lead[], companyId: string): string {
  return leads.find((lead) => lead.company_id === companyId)?.name ?? companyId;
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
  if (fate.status === "stuck") return "неизвестно — разбирает человек";
  if (fate.status === "cancelled") return "отменено";
  if (fate.status === "sent") return "ушло, подтверждения доставки нет";
  return `ждёт отправки с ${WHEN.format(new Date(fate.send_after))}`;
}
