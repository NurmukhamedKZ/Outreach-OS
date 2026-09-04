/** Подписи и цвет на каждый исход. Единственное место, где статус превращается
 *  в слово и в тон.
 *
 *  Значения — словарь бэкенда (`conversation.STATUSES`, `worker.TICK_OUTCOMES`),
 *  подписи и тон — чистая презентация, ровно как ACTOR_LABELS на «Процессах».
 *  Расхождение списков ловит бэкенд: sender/tests/test_worker.py.
 *
 *  Тонов четыре, и это не палитра, а смысл: `stop` — нужен человек прямо сейчас,
 *  `attention` — само не разрешится, `ok` — идёт как задумано, `idle` — кончилось
 *  и делать нечего. Пятый тон означал бы, что оператору снова надо думать, какой
 *  цвет что значит.
 */

export type Tone = "stop" | "attention" | "ok" | "idle";

export type Outcome = { label: string; tone: Tone };

/** Статус треда: `threads.status`, владелец — система 3. */
const THREAD_STATUS: Record<string, Outcome> = {
  escalated: { label: "ждёт человека", tone: "stop" },
  closed_refused: { label: "отказался", tone: "idle" },
  closed_junk: { label: "не тот человек", tone: "idle" },
  unreachable: { label: "нет WhatsApp", tone: "idle" },
  exhausted: { label: "касания кончились", tone: "attention" },
  blocked_channel: { label: "ищем номер", tone: "attention" },
  active: { label: "живой диалог", tone: "ok" },
  queued: { label: "ждёт отправки", tone: "ok" },
};

/** Исход тика воркера: `worker.TICK_OUTCOMES`, он же outcome в журнале. */
const TICK_OUTCOME: Record<string, Outcome> = {
  escalated: { label: "отдал человеку", tone: "stop" },
  failed: { label: "не отправилось", tone: "stop" },
  stuck: { label: "судьба неизвестна", tone: "stop" },
  retry: { label: "повтор", tone: "attention" },
  rescheduled: { label: "перенесено", tone: "attention" },
  cancelled: { label: "отменено", tone: "attention" },
  crashed: { label: "авария демона", tone: "stop" },
  sent: { label: "отправлено", tone: "ok" },
  answered: { label: "ответил сам", tone: "ok" },
  touched: { label: "касание", tone: "ok" },
  queued: { label: "в очередь", tone: "ok" },
  closed: { label: "тред закрыт", tone: "idle" },
  exhausted: { label: "касания кончились", tone: "idle" },
  taken: { label: "занято другим тиком", tone: "idle" },
  idle: { label: "нечего делать", tone: "idle" },
  incoming: { label: "лид написал", tone: "ok" },
  duplicate: { label: "повтор от транспорта", tone: "idle" },
  refused: { label: "стоп-слово", tone: "idle" },
  healthy: { label: "номера в порядке", tone: "ok" },
  transport_down: { label: "транспорт не отвечает", tone: "stop" },
  started: { label: "запущено", tone: "ok" },
  finished: { label: "готово", tone: "ok" },
};

/** Неизвестное значение показываем как есть и нейтральным тоном: выдуманная
 *  подпись врёт, а сырое слово честно говорит «этого мы не ждали». */
function unknown(value: string): Outcome {
  return { label: value, tone: "idle" };
}

export function threadStatus(status: string | null | undefined): Outcome {
  if (!status) return unknown("статус неизвестен");
  return THREAD_STATUS[status] ?? unknown(status);
}

export function tickOutcome(outcome: string): Outcome {
  return TICK_OUTCOME[outcome] ?? unknown(outcome);
}
