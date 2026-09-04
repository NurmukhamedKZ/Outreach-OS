"use client";

/** «Холодные»: конвейер проверки первых писем. Оператор идёт по черновикам,
 *  правит, отправляет или отказывает — выделение само переходит к следующему.
 *  Порядок приходит с бэкенда (по убыванию скора), страница его не пересчитывает.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeftIcon, ArrowRightIcon } from "@phosphor-icons/react";
import { channelLink, fetchCatalogue, fetchColdDrafts, type ColdDraft, type Pipeline } from "../api";
import MessageComposer from "@/components/MessageComposer";
import RefusalForm from "@/components/RefusalForm";
import { PipelineActions } from "@/components/JobMonitor";
import { useLive } from "@/components/live";

export default function Cold() {
  const { refreshTick } = useLive();
  const [drafts, setDrafts] = useState<ColdDraft[] | null>(null);
  const [index, setIndex] = useState(0);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [revision, setRevision] = useState(0);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    fetchCatalogue()
      .then((c) => setPipelines(c.pipelines.filter((p) => p.kind === "write")))
      .catch(() => undefined);
  }, []);

  const reload = useCallback(() => setRevision((n) => n + 1), []);

  useEffect(() => {
    let stale = false;
    fetchColdDrafts()
      .then((data) => {
        if (stale) return;
        setDrafts(data.drafts);
        setFailure(null);
        // Выделение после отпуска/отказа не должно уходить за конец списка.
        setIndex((prev) => Math.max(0, Math.min(prev, data.drafts.length - 1)));
      })
      .catch((error: Error) => !stale && setFailure(error.message));
    return () => {
      stale = true;
    };
  }, [revision, refreshTick]);

  const draft = drafts?.[index] ?? null;
  const step = () => {
    if (!drafts?.length) return;
    setIndex((prev) => (prev + 1) % drafts.length);
  };

  return (
    <>
      <header className="page-head">
        <h1>Холодные</h1>
        <span className="page-sub">
          {drafts ? `${drafts.length} ждут проверки` : "черновиков нет"}
        </span>
      </header>

      <PipelineActions pipelines={pipelines} />
      {failure && <div className="failure">{failure}</div>}

      <div className="split">
        <div className="roster">
          {(drafts ?? []).map((entry, position) => (
            <button
              key={`${entry.thread_id}-${entry.message_id}`}
              className="row"
              aria-current={position === index}
              onClick={() => setIndex(position)}
            >
              <span className="rank mono">{position + 1}</span>
              <span className="row-text">
                <span className="row-name">{entry.company_name}</span>
                <span className="row-sub mono">
                  {entry.city} · {entry.thread_id}
                </span>
              </span>
              <span className="intent mono">{entry.intent_score.toFixed(1)}</span>
            </button>
          ))}
          {drafts !== null && drafts.length === 0 && (
            <p className="placeholder">
              Тредов без отправленных сообщений нет. Первые черновики появляются после
              «Черновики топ-N».
            </p>
          )}
          {drafts === null && !failure && <p className="placeholder">Загружаем очередь…</p>}
        </div>

        <div className="detail">
          {draft ? (
            <>
              <div className="cold-head">
                <Link href={`/leads/${encodeURIComponent(draft.company_id)}`}>
                  карточка {draft.company_name}
                </Link>
                <a
                  className="ghost mono"
                  href={channelLink({ kind: "whatsapp", handle: draft.thread_id })}
                  target="_blank"
                  rel="noreferrer"
                >
                  {draft.thread_id}
                </a>
                <span className="cold-position mono">
                  {index + 1} из {drafts!.length}
                </span>
                <button className="ghost" onClick={() => setIndex((i) => Math.max(0, i - 1))}>
                  <ArrowLeftIcon size={14} /> назад
                </button>
                <button className="ghost" onClick={step}>
                  пропустить <ArrowRightIcon size={14} />
                </button>
              </div>

              <MessageComposer
                companyId={draft.company_id}
                messageId={draft.message_id}
                threadId={draft.thread_id}
                text={draft.draft_text}
                angle={draft.angle}
                model={draft.model}
                hasPrompt={draft.has_prompt}
                draftKind="first"
                onQueued={step}
                onRegenerate={reload}
              />

              <RefusalForm
                channel={{ kind: "whatsapp", handle: draft.thread_id }}
                onDone={() => {
                  setIndex((i) => Math.max(0, i));
                  reload();
                  step();
                }}
              />
            </>
          ) : (
            <p className="placeholder">Выберите черновик слева.</p>
          )}
        </div>
      </div>
    </>
  );
}
