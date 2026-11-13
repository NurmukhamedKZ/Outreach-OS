"use client";

/** «Процессы»: всё, что система делает сама, — на одном экране.

 Полоска демонов отвечает на вопрос «кто жив», лента — «кто что делал». Журнал
 и пульс перечитываются раз в 5 секунд, пока вкладка видна.
 */

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  fetchActivity,
  fetchBackup,
  runBackup,
  type ActivityEvent,
  type ActivityWorker,
  type BackupStatus,
} from "../api";
import { usePoll } from "@/components/usePoll";

const POLL_MS = 5_000;

const WHEN = new Intl.DateTimeFormat("ru", {
  hour: "2-digit",
  minute: "2-digit",
});

const WHEN_DAY = new Intl.DateTimeFormat("ru", {
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

// Имена акторов — словарь бэкенда (activity.record), подписи — чистая
// презентация; порог молчания приезжает с бэкенда и здесь не повторяется.
const ACTOR_LABELS: Record<string, string> = {
  "sender.tick": "Тик воркера",
  "sender.warmup": "Прогрев номеров",
  "sender.monitor": "Монитор здоровья",
  webhook: "Вебхук",
  backup: "Бэкап",
};

export default function ActivityPage() {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [workers, setWorkers] = useState<ActivityWorker[]>([]);
  const [actor, setActor] = useState("");
  const [failure, setFailure] = useState<string | null>(null);

  const load = useCallback(() => {
    fetchActivity(200, actor || undefined)
      .then((data) => {
        setEvents(data.events);
        setWorkers(data.workers);
        setFailure(null);
      })
      .catch((error: Error) => setFailure(error.message));
  }, [actor]);
  usePoll(load, POLL_MS);

  if (failure) {
    return (
      <div className="failure">
        <b>Бэкенд недоступен.</b> {failure}
      </div>
    );
  }

  return (
    <>
      <header className="page-head">
        <h1>Процессы</h1>
        <span className="page-sub">
          {workers.length > 0
            ? `${workers.filter(isAlive).length} из ${workers.length} демонов работают`
            : "демонов ещё не видели — тик воркера идёт раз в 20 секунд"}
        </span>
      </header>

      <section className="daemons card">
        {workers.length === 0 && <p className="note">журнал пуст</p>}
        {workers.map((worker) => (
          <div key={worker.actor} className="daemon">
            <span className={`daemon-dot${isAlive(worker) ? " is-on" : ""}`} />
            <span className="daemon-name">{ACTOR_LABELS[worker.actor] ?? worker.actor}</span>
            <span className="daemon-last mono">
              {worker.last_at ? WHEN_DAY.format(new Date(worker.last_at)) : "никогда"}
            </span>
            <span className="daemon-note">
              {isAlive(worker) ? "пишет" : "молчит"}
            </span>
          </div>
        ))}
      </section>

      <BackupRow />

      <div className="controls" style={{ margin: "16px 0 12px" }}>
        <select value={actor} onChange={(event) => setActor(event.target.value)}>
          <option value="">все демоны</option>
          {Object.keys(ACTOR_LABELS).map((name) => (
            <option key={name} value={name}>
              {ACTOR_LABELS[name]}
            </option>
          ))}
        </select>
      </div>

      <section className="card">
        <h3>Журнал фоновой работы</h3>
        <div className="feed">
          {events.length === 0 && <p className="note">событий пока нет</p>}
          {events.map((event, index) => (
            <div key={`${event.actor}-${event.last_at}-${event.outcome}-${event.subject}-${index}`} className="feed-row">
              <span className="feed-when mono" title={event.last_at}>
                {isToday(event.last_at) ? WHEN.format(new Date(event.last_at)) : WHEN_DAY.format(new Date(event.last_at))}
              </span>
              <span className="feed-actor">{ACTOR_LABELS[event.actor] ?? event.actor}</span>
              <span className="feed-outcome">{event.outcome}</span>
              {event.subject && <span className="feed-subject mono">{event.subject}</span>}
              {event.repeats > 1 && <span className="feed-repeats mono">×{event.repeats}</span>}
              {event.detail && <span className="feed-detail mono">{event.detail}</span>}
            </div>
          ))}
        </div>
      </section>
    </>
  );
}

/** Бэкап одной строкой: когда был последний удачный, куда и сколько снимков. */
function BackupRow() {
  const [backup, setBackup] = useState<BackupStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = () => fetchBackup().then(setBackup).catch((e: Error) => setError(e.message));
  useEffect(() => {
    reload();
  }, []);

  const run = () => {
    setBusy(true);
    setError(null);
    runBackup()
      .catch((e: Error) =>
        setError(e instanceof ApiError && e.code === "errors.backupRunning" ? "бэкап уже идёт" : e.message),
      )
      .then(reload)
      .finally(() => setBusy(false));
  };

  return (
    <section className="card backup-row">
      <span className="backup-text">
        <span>
          <b>Бэкап</b>{" "}
          {backup === null
          ? "…"
          : !backup.enabled
            ? "выключен: нет каталога iCloud Drive"
            : backup.last_at
              ? `последний ${WHEN_DAY.format(new Date(backup.last_at))} · снимков ${backup.snapshots}`
              : "ещё не было"}
        </span>
        {backup && <span className="backup-target mono">{backup.target}</span>}
        {error && <span className="backup-error">{error}</span>}
      </span>
      <button
        className="btn"
        onClick={run}
        disabled={busy || backup === null || !backup.enabled || backup.running}
      >
        {busy || backup?.running ? "Идёт бэкап…" : "Сделать бэкап"}
      </button>
    </section>
  );
}

function isAlive(worker: ActivityWorker) {
  if (!worker.last_at) return false;
  return Date.now() - Date.parse(worker.last_at) < worker.silent_after_seconds * 1000;
}

function isToday(stamp: string) {
  const moment = new Date(stamp);
  const now = new Date();
  return moment.toDateString() === now.toDateString();
}
