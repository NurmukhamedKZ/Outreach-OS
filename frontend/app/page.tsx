"use client";

/** Обзор: три системы одним экраном и живые процессы под ними.

Посетитель должен понять продукт за полминуты: слева счётчики систем,
внизу панель запуска и монитор джобы с настоящим логом. Ничего не крутится
зря: если джоба идёт, её видно; если нет, видна история последних.
*/

import { useEffect, useState } from "react";
import { PaperPlaneTiltIcon } from "@phosphor-icons/react";
import { fetchCatalogue, type PipelineCatalogue } from "./api";
import { JobMonitor, PipelineActions } from "@/components/JobMonitor";
import { OperationsPanel } from "@/components/OperationsPanel";
import { RunsHistory } from "@/components/RunsHistory";
import { useLive } from "@/components/live";

const WHEN = new Intl.DateTimeFormat("ru", { hour: "2-digit", minute: "2-digit" });

export default function Overview() {
  const { stats, jobs, connected } = useLive();
  const [catalogue, setCatalogue] = useState<PipelineCatalogue | null>(null);

  useEffect(() => {
    fetchCatalogue().then(setCatalogue).catch(() => undefined);
  }, []);

  return (
    <>
      <header className="page-head">
        <h1>Обзор</h1>
        <span className="page-sub">
          {stats
            ? `${stats.sourcing.available} лидов готовы к работе`
            : connected
              ? "загружаем счётчики…"
              : "ждём бэкенд"}
        </span>
      </header>

      {stats ? (
        <div className="metrics">
          <section className="metric-card">
            <div className="metric-head">Сбор лидов</div>
            <div className="metric-row is-total">
              <span>компаний в базе</span>
              <b>{stats.sourcing.companies}</b>
            </div>
            <div className="metric-row">
              <span>с intent-сигналами</span>
              <b>{stats.sourcing.with_intent}</b>
            </div>
            <div className="metric-row">
              <span>с рабочим каналом</span>
              <b>{stats.sourcing.available}</b>
            </div>
            <div className="metric-row">
              <span>отказов (suppression)</span>
              <b>{stats.sourcing.suppressed}</b>
            </div>
          </section>

          <section className="metric-card">
            <div className="metric-head">Персонализация</div>
            <div className="metric-row is-total">
              <span>тредов открыто</span>
              <b>{stats.writer.threads}</b>
            </div>
            <div className="metric-row">
              <span>черновиков ждёт</span>
              <b>{stats.writer.drafts}</b>
            </div>
            <div className="metric-row">
              <span>отправлено сообщений</span>
              <b>{stats.writer.sent}</b>
            </div>
            <div className="metric-row">
              <span>ответов от лидов</span>
              <b>{stats.writer.replies}</b>
            </div>
          </section>

          <section className="metric-card is-soon">
            <div className="metric-head">
              <PaperPlaneTiltIcon size={13} /> Отправка
            </div>
            <span>Домены, прогрев ящиков и расписание рассылки.</span>
            <span className="tag">скоро</span>
          </section>
        </div>
      ) : (
        <div className="metrics">
          <div className="metric-card skeleton" aria-hidden>
            <div className="skeleton-line" style={{ width: "60%" }} />
            <div className="skeleton-line" style={{ width: "90%" }} />
            <div className="skeleton-line" style={{ width: "75%" }} />
          </div>
          <div className="metric-card skeleton" aria-hidden>
            <div className="skeleton-line" style={{ width: "80%" }} />
            <div className="skeleton-line" style={{ width: "55%" }} />
          </div>
          <div className="metric-card skeleton" aria-hidden>
            <div className="skeleton-line" style={{ width: "70%" }} />
            <div className="skeleton-line" style={{ width: "40%" }} />
          </div>
        </div>
      )}

<PipelineActions pipelines={catalogue?.pipelines ?? []} />
      <OperationsPanel operations={catalogue?.operations ?? []} />
      <JobMonitor />

      <section className="card">
        <h3>История запусков</h3>
        <div className="jobs-list">
          {jobs.map((job) => (
            <div key={job.id} className="jobs-row">
              <span>
                <span className="jobs-title">{job.title}</span>
                {job.error ? <span className="jobs-when"> · {job.error}</span> : null}
              </span>
              <span className="jobs-when">{job.finished_at ? WHEN.format(new Date(job.finished_at)) : "…"}</span>
              <span className={`status is-${job.status}`}>
                {job.status === "running"
                  ? `шаг ${job.step + 1} из ${job.step_count}`
                  : STATUS[job.status] ?? job.status}
              </span>
            </div>
          ))}
        </div>
      </section>

      {/* после «Истории запусков» */}
      <RunsHistory />
    </>
  );
}

const STATUS: Record<string, string> = {
  queued: "в очереди",
  done: "готово",
  failed: "не прошёл",
  cancelled: "прервано",
};
