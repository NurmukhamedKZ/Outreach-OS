"use client";

/** Система 2: персонализация. Инбокс открытых тредов + карточка с перепиской.
 * «Черновики топ-N» ставит джобу write: агент напишет первым сообщениям
 * новым компаниям, у которых треда ещё нет.
 */

import { useEffect, useState } from "react";
import { fetchCatalogue, fetchThreads, type Pipeline, type ThreadSummary } from "../api";
import LeadCard from "../LeadCard";
import { JobMonitor, PipelineActions } from "@/components/JobMonitor";
import { useLive } from "@/components/live";

const WHEN = new Intl.DateTimeFormat("ru", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

export default function Writer() {
  const { refreshTick, stats } = useLive();
  const [threads, setThreads] = useState<ThreadSummary[] | null>(null);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    fetchCatalogue()
      .then((c) => setPipelines(c.pipelines.filter((p) => p.kind === "write")))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    let stale = false;
    fetchThreads()
      .then((data) => !stale && setThreads(data.threads))
      .catch((error: Error) => !stale && setFailure(error.message));
    return () => {
      stale = true;
    };
  }, [refreshTick]);

  return (
    <>
      <header className="page-head">
        <h1>Персонализация</h1>
        {stats && (
          <div className="counters">
            <span>
              тредов <b>{stats.writer.threads}</b>
            </span>
            <span>
              черновиков <b>{stats.writer.drafts}</b>
            </span>
            <span>
              отправлено <b>{stats.writer.sent}</b>
            </span>
            <span>
              ответов <b>{stats.writer.replies}</b>
            </span>
          </div>
        )}
      </header>

      <PipelineActions pipelines={pipelines} />
      <JobMonitor />

      {failure && <div className="failure">{failure}</div>}

      <div className="split">
        <div className="roster">
          {(threads ?? []).map((thread) => (
            <button
              key={thread.thread_id}
              className="row inbox-row"
              aria-current={thread.company_id === selected}
              onClick={() => setSelected(thread.company_id)}
            >
              <span className="row-text">
                <span className="inbox-name">{thread.company_name}</span>
                <span className="inbox-sub">
                  {thread.last_at ? WHEN.format(new Date(thread.last_at)) : "ещё не отправляли"}
                  {thread.last_message ? ` · ${thread.last_message}` : ""}
                </span>
              </span>
              <span
                className={`inbox-count mono${thread.replies > 0 ? " has-replies" : ""}`}
                title={`${thread.sent} отправлено, ${thread.replies} ответов, ${thread.drafts} черновиков`}
              >
                {thread.replies > 0 ? `↩ ${thread.replies}` : `→ ${thread.sent}`}
              </span>
            </button>
          ))}
          {threads !== null && threads.length === 0 && (
            <p className="placeholder">
              Тредов ещё нет. Запустите «Черновики топ-N» или откройте тред из карточки лида
              в разделе «Сбор лидов».
            </p>
          )}
          {threads === null && !failure && <p className="placeholder">Инбокс загружается…</p>}
        </div>

        <div className="detail">
          {selected ? (
            <LeadCard companyId={selected} onRefused={() => undefined} />
          ) : (
            <p className="placeholder">Выберите тред слева.</p>
          )}
        </div>
      </div>
    </>
  );
}
