"use client";

/** «Диалоги»: инбокс тредов + переписка. Порядок приходит с бэкенда
 *  (эскалированные и ждущие ответа — сверху), страница его не пересортировывает.
 *  Черновик правится композером из «Холодных»: у наших сообщений видно, чем было
 *  касание (cold / followup / reply) и что автомат — только его собственность.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  addIncoming,
  channelLink,
  fetchConversation,
  fetchPrompt,
  fetchQueue,
  fetchThreads,
  requestDraft,
  setThreadOutcome,
  type Conversation,
  type QueueRow,
  type ThreadSummary,
} from "../api";
import MessageComposer from "@/components/MessageComposer";
import { useLive } from "@/components/live";

const WHEN = new Intl.DateTimeFormat("ru", {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});

type PromptInfo = { hasPrompt: boolean; model: string | null };

export default function Threads() {
  const { refreshTick } = useLive();
  const [threads, setThreads] = useState<ThreadSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [queue, setQueue] = useState<QueueRow[]>([]);
  const [promptInfo, setPromptInfo] = useState<PromptInfo | null>(null);
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    fetchThreads()
      .then((data) => !stale && setThreads(data.threads))
      .catch((error: Error) => !stale && setFailure(error.message));
    return () => {
      stale = true;
    };
  }, [refreshTick]);

  useEffect(() => {
    if (!selected) {
      setConversation(null);
      return;
    }
    let stale = false;
    setConversation(null);
    (async () => {
      const conv = await fetchConversation(selected);
      if (stale) return;
      setConversation(conv);
      const queued = await fetchQueue(conv.thread_id);
      if (stale) return;
      setQueue(queued.queue);
      // hasPrompt решает, что покажет композер под <details>: промпт или
      // честное «написан до того, как промпт начали сохранять».
      if (conv.draft) {
        const stored = await fetchPrompt(selected, conv.draft.message_id);
        if (!stale) setPromptInfo({ hasPrompt: stored.prompt !== null, model: stored.model });
      } else {
        setPromptInfo(null);
      }
    })().catch((error: Error) => !stale && setFailure(error.message));
    return () => {
      stale = true;
    };
  }, [selected]);

  const summary = threads?.find((row) => row.company_id === selected) ?? null;
  const last = conversation?.messages[conversation.messages.length - 1];
  const draftKind = !last ? "first" : last.role === "incoming" ? "reply" : "followup";
  const pending = queue.find((row) => row.message_id === conversation?.draft?.message_id);

  async function run(action: () => Promise<Conversation>) {
    setBusy(true);
    setFailure(null);
    try {
      const next = await action();
      setConversation(next);
    } catch (error) {
      setFailure((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function markOutcome(outcome: "meeting_agreed" | "meeting_held" | "lost") {
    if (!selected) return;
    setBusy(true);
    setFailure(null);
    try {
      await setThreadOutcome(selected, outcome);
      const conv = await fetchConversation(selected);
      setConversation(conv);
    } catch (error) {
      setFailure((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <header className="page-head">
        <h1>Диалоги</h1>
        <span className="page-sub">
          {threads ? `${threads.length} тредов` : "загружаем…"}
        </span>
      </header>

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
                <span className="inbox-sub mono">{thread.thread_id}</span>
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
              Тредов ещё нет. Первые черновики появляются после «Черновики топ-N»
              на странице «Холодные».
            </p>
          )}
          {threads === null && !failure && <p className="placeholder">Инбокс загружается…</p>}
        </div>

        <div className="detail">
          {selected && conversation ? (
            <>
              <header className="cold-head">
                <b className="row-name">{summary?.company_name ?? selected}</b>
                <a
                  className="ghost mono"
                  href={channelLink({ kind: "whatsapp", handle: conversation.thread_id })}
                  target="_blank"
                  rel="noreferrer"
                >
                  {conversation.thread_id}
                </a>
                <Link href={`/leads/${encodeURIComponent(selected)}`}>карточка лида</Link>
              </header>

              <ol className="thread-log">
                {conversation.messages.map((message, index) => (
                  <li key={`${message.message_id ?? index}`} className={message.role}>
                    <span className="thread-who">
                      {message.role === "outgoing" ? "мы" : "они"}
                    </span>
                    <span>
                      {message.text}
                      {message.role === "incoming" ? null : message.kind === "followup" ? (
                        <span className="message-kind"> · follow-up — отправлено автоматом</span>
                      ) : message.kind === "reply" ? (
                        <span className="message-kind"> · ответ — отправлено автоматом</span>
                      ) : null}
                    </span>
                  </li>
                ))}
                {pending && (
                  <li className="queued">
                    <span className="thread-who">мы</span>
                    <span>
                      {conversation.draft?.draft_text ?? ""}
                      <span className="message-kind">
                        {" "}
                        · в очереди с {pending.our_number} не раньше{" "}
                        {WHEN.format(new Date(pending.send_after))}
                      </span>
                    </span>
                  </li>
                )}
                {conversation.messages.length === 0 && !pending && (
                  <li className="placeholder">Ещё не писали.</li>
                )}
              </ol>

              {conversation.draft ? (
                <MessageComposer
                  companyId={selected}
                  messageId={conversation.draft.message_id}
                  threadId={conversation.thread_id}
                  text={conversation.draft.draft_text}
                  angle={conversation.draft.angle}
                  model={promptInfo?.model ?? null}
                  hasPrompt={promptInfo?.hasPrompt ?? false}
                  draftKind={draftKind}
                  onQueued={() =>
                    fetchQueue(conversation.thread_id).then((q) => setQueue(q.queue))
                  }
                  onRegenerate={() =>
                    fetchConversation(selected).then((conv) => setConversation(conv))
                  }
                />
              ) : (
                <button
                  className="btn"
                  disabled={busy}
                  onClick={() => run(() => requestDraft(selected, draftKind))}
                >
                  {firstLabel(draftKind, conversation)}
                </button>
              )}

              {conversation.messages.length > 0 && (
                <form
                  className="thread-reply"
                  onSubmit={(event) => {
                    event.preventDefault();
                    run(() => addIncoming(selected, reply)).then(() => setReply(""));
                  }}
                >
                  <input
                    value={reply}
                    onChange={(event) => setReply(event.target.value)}
                    placeholder="Ответ лида — вставить как есть"
                  />
                  <button type="submit" disabled={busy || !reply.trim()}>
                    Записать ответ
                  </button>
                </form>
              )}

              <div style={{ display: "flex", gap: "8px", marginTop: "12px" }}>
                <button className="btn" disabled={busy} onClick={() => markOutcome("meeting_agreed")}>
                  Согласился на созвон
                </button>
                <button className="btn" disabled={busy} onClick={() => markOutcome("meeting_held")}>
                  Созвон состоялся
                </button>
                <button className="btn" disabled={busy} onClick={() => markOutcome("lost")}>
                  Не сложилось
                </button>
              </div>

              {conversation.stop && (
                <p className="note">Агент советует не писать: нового повода в данных нет.</p>
              )}
            </>
          ) : (
            <p className="placeholder">Выберите тред слева.</p>
          )}
        </div>
      </div>
    </>
  );
}

function firstLabel(kind: string, conversation: Conversation) {
  if (conversation.messages.length === 0) return "Черновик первого сообщения";
  return kind === "reply" ? "Черновик ответа" : "Черновик с новым поводом";
}
