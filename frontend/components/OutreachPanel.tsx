"use client";

/** «Черновики топ-N»: кнопка, прогресс опросом API и итог — по образцу партии
 *  исследования (BatchPanel). Состояние прогона целиком в его строке, SSE не нужен. */

import { useEffect, useRef, useState } from "react";
import {
  conversationError,
  fetchOutreach,
  startOutreach,
  stopOutreach,
  type OutreachRun,
} from "@/app/api";
import { moment, outreachStatus } from "@/app/outcomes";

const RUNNING_MS = 3000;
const IDLE_MS = 30000;

/** `onProgress` зовётся, когда прогон написал ещё одного лида или закончился, —
 *  по нему страница перечитывает черновики, не дожидаясь конца прогона. */
export default function OutreachPanel({ onProgress }: { onProgress: () => void }) {
  const [run, setRun] = useState<OutreachRun | null | undefined>(undefined);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const progress = useRef(onProgress);
  const seen = useRef<string | null>(null);
  // Ответ опроса, ушедшего до запуска или остановки, затёр бы новое состояние.
  const latest = useRef(0);
  useEffect(() => {
    progress.current = onProgress;
  });

  const adopt = (next: OutreachRun | null) => {
    setRun(next);
    setFailure(null);
    const mark = next && `${next.run_id}:${next.drafted}:${next.skipped}:${next.status}`;
    if (seen.current !== null && mark !== seen.current) progress.current();
    seen.current = mark;
  };

  const running = run?.status === "running";
  useEffect(() => {
    const load = () => {
      const request = ++latest.current;
      fetchOutreach()
        .then((next) => request === latest.current && adopt(next))
        .catch((failure) => setFailure(conversationError(failure)));
    };
    load();
    const timer = setInterval(load, running ? RUNNING_MS : IDLE_MS);
    return () => clearInterval(timer);
  }, [running]);

  const act = (call: () => Promise<OutreachRun>) => {
    const request = ++latest.current;
    setBusy(true);
    setError(null);
    call()
      .then((next) => request === latest.current && adopt(next))
      .catch((failure) => setError(conversationError(failure)))
      .finally(() => setBusy(false));
  };

  if (run === undefined) {
    return <section className="card batch">{failure ?? "Загрузка…"}</section>;
  }

  const status = run && outreachStatus(run.status);

  return (
    <section className="card batch" aria-label="Черновики топ-N">
      <div className="batch-head">
        {running ? (
          <button
            className="btn-secondary"
            onClick={() => act(stopOutreach)}
            disabled={busy || run!.stop_requested}
          >
            {run!.stop_requested ? "Останавливается…" : "Остановить"}
          </button>
        ) : (
          <button className="btn" onClick={() => act(startOutreach)} disabled={busy}>
            Черновики топ-N
          </button>
        )}
        {run && status && (
          <span className="batch-summary">
            <span className={`tone is-${status.tone}`}>
              {running ? "Прогон " : "Последний прогон "}
              {status.label}
            </span>
            <span className="counters">
              <span>лидов <b>{run.size}</b></span>
              <span>взято <b>{run.taken}</b></span>
              <span>черновиков <b>{run.drafted}</b></span>
              <span>не писать <b>{run.skipped}</b></span>
            </span>
          </span>
        )}
      </div>

      {!run && (
        <p className="note">
          Прогон берёт новых лидов по убыванию оценки и пишет каждому первое письмо. Сколько
          лидов и какая модель — в «Настройках», раздел «Переписка».
        </p>
      )}
      {run && run.skipped > 0 && !running && (
        <p className="note">
          «Не писать» — модель не нашла в досье нового повода; тред открыт, черновика нет.
        </p>
      )}
      {run?.finished_at && !running && (
        <p className="note">
          {run.status === "stopped" ? "Остановлен" : "Закончился"} {moment(run.finished_at)}.
        </p>
      )}
      {run?.error && (
        <p className="error-line" role="alert">
          Прогон остановился сам: {run.error}
        </p>
      )}
      {error && (
        <p className="error-line" role="alert">
          {error}
        </p>
      )}
      {failure && <p className="error-line">{failure}</p>}
    </section>
  );
}
