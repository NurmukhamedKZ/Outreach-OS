"use client";

/** Партия исследования: кнопка, прогресс опросом API и итог. SSE не используем
 *  (#4): состояние партии целиком в её строке, опроса раз в 3 секунды хватает. */

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchBatch, researchError, startBatch, stopBatch, type Batch } from "@/app/api";
import { batchStatus, moment, researchStatus } from "@/app/outcomes";

const RUNNING_MS = 3000;
const IDLE_MS = 30000;

export function isRunning(batch: Batch | null | undefined) {
  return batch?.status === "collecting" || batch?.status === "researching";
}

/** Текущая партия с опросом: чаще, пока идёт. `onFinished` зовётся, когда
 *  идущая партия закончилась, — по нему перечитывают лидов или карточку. */
export function useBatch(onFinished: () => void) {
  const [batch, setBatch] = useState<Batch | null | undefined>(undefined);
  const [failure, setFailure] = useState<string | null>(null);
  const finished = useRef(onFinished);
  const wasRunning = useRef(false);
  finished.current = onFinished;

  const adopt = useCallback((next: Batch | null) => {
    setBatch(next);
    setFailure(null);
    if (wasRunning.current && !isRunning(next)) finished.current();
    wasRunning.current = isRunning(next);
  }, []);

  const running = isRunning(batch);
  useEffect(() => {
    const load = () => fetchBatch().then(adopt).catch((error) => setFailure(researchError(error)));
    load();
    const timer = setInterval(load, running ? RUNNING_MS : IDLE_MS);
    return () => clearInterval(timer);
  }, [running, adopt]);

  return { batch, adopt, failure };
}

export default function BatchPanel({ onFinished }: { onFinished: () => void }) {
  const { batch, adopt, failure } = useBatch(onFinished);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = (call: () => Promise<Batch>) => {
    setBusy(true);
    setError(null);
    call()
      .then(adopt)
      .catch((failure) => setError(researchError(failure)))
      .finally(() => setBusy(false));
  };

  if (batch === undefined) {
    return <section className="card batch">{failure ?? "Загрузка…"}</section>;
  }

  const running = isRunning(batch);
  const failed = batch?.companies.filter((c) => c.status === "failed") ?? [];
  const rejected = batch?.companies.reduce((sum, c) => sum + c.rejected_quotes, 0) ?? 0;
  const status = batch && batchStatus(batch.status);

  return (
    <section className="card batch" aria-label="Партия исследования">
      <div className="batch-head">
        {running ? (
          <button
            className="btn-secondary"
            onClick={() => act(stopBatch)}
            disabled={busy || batch!.stop_requested}
          >
            {batch!.stop_requested ? "Останавливается…" : "Остановить"}
          </button>
        ) : (
          <button className="btn" onClick={() => act(startBatch)} disabled={busy}>
            {batch?.status === "interrupted" ? "Продолжить партию" : "Исследовать партию"}
          </button>
        )}
        {batch && status && (
          <span className="batch-summary">
            <span className={`tone is-${status.tone}`}>
              {running ? "Партия " : "Последняя партия "}
              {status.label}
            </span>
            <span className="counters">
              <span>компаний <b>{batch.size}</b></span>
              <span>ждут <b>{batch.counts.pending}</b></span>
              <span>исследуются <b>{batch.counts.researching}</b></span>
              <span>готово <b>{batch.counts.done}</b></span>
              <span>неудач <b>{batch.counts.failed}</b></span>
              <span>отброшено цитат <b>{rejected}</b></span>
            </span>
          </span>
        )}
      </div>

      {batch?.status === "interrupted" && (
        <p className="note">
          Партию прервал перезапуск бэкенда. «Продолжить партию» доделает её с тех компаний, что
          не успели.
        </p>
      )}
      {!batch && (
        <p className="note">
          Партия собирает выбранные сегменты, если они не собраны, и исследует компании по
          приоритету. Размер партии и модель — в «Настройках».
        </p>
      )}
      {batch && !running && (
        <p className="note">
          {batch.status === "stopped" ? "Остановлена" : "Закончилась"} {moment(batch.finished_at)}.
        </p>
      )}
      {batch?.error && (
        <p className="error-line" role="alert">
          Партия остановилась сама: {batch.error}
        </p>
      )}
      {error && (
        <p className="error-line" role="alert">
          {error}
        </p>
      )}
      {failure && <p className="error-line">{failure}</p>}

      {failed.length > 0 && (
        <details className="batch-failures">
          <summary>Неудачи — {failed.length}</summary>
          <ul>
            {failed.map((company) => (
              <li key={company.company_id}>
                <b>{company.name}</b>
                {company.attempt && ` · попытка ${company.attempt}`}
                {" — "}
                {company.error ?? researchStatus(company.status).label}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
