"use client";

/** «Процессы»: всё, что система делает сама, — на одном экране.

 Полоска демонов отвечает на вопрос «кто жив», лента — «кто что делал».
 Первый экран поднимается запросом, дальше лента растёт из SSE (событие
 activity), склеенная с запросом и дедуплицированная по ключу строки журнала.
 */

import { useEffect, useMemo, useState } from "react";
import {
  fetchActivity,
  fetchCatalogue,
  type ActivityEvent,
  type ActivityWorker,
  type PipelineCatalogue,
} from "../api";
import { JobMonitor } from "@/components/JobMonitor";
import { OperationsPanel } from "@/components/OperationsPanel";
import { RunsHistory } from "@/components/RunsHistory";
import { useLive } from "@/components/live";

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
  jobs: "Воркер джоб",
  webhook: "Вебхук",
};

export default function ActivityPage() {
  const { activityTail } = useLive();
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [workers, setWorkers] = useState<ActivityWorker[]>([]);
  const [actor, setActor] = useState("");
  const [catalogue, setCatalogue] = useState<PipelineCatalogue | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    fetchCatalogue().then(setCatalogue).catch(() => undefined);
  }, []);

  useEffect(() => {
    fetchActivity(200, actor || undefined)
      .then((data) => {
        setEvents(data.events);
        setWorkers(data.workers);
        setFailure(null);
      })
      .catch((error: Error) => setFailure(error.message));
  }, [actor]);

  // Лента = живое из SSE + первый экран запросом. Дедуп по ключу строки
  // журнала: событие успевает приехать дважды — своим ходом и внутри ответа.
  const feed = useMemo(() => {
    const seen = new Set<string>();
    return [...activityTail, ...events].filter((event) => {
      if (actor && event.actor !== actor) return false;
      const key = `${event.actor}|${event.outcome}|${event.subject}|${event.last_at}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [activityTail, events, actor]);

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

      <JobMonitor />

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
          {feed.length === 0 && <p className="note">событий пока нет</p>}
          {feed.map((event, index) => (
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

      <OperationsPanel operations={catalogue?.operations ?? []} />
      <div style={{ marginTop: 18 }}>
        <RunsHistory />
      </div>
    </>
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
