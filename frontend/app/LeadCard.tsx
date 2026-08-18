"use client";

import { useEffect, useState } from "react";
import {
  CHANNEL_LABELS,
  SIGNAL_LABELS,
  addIncoming,
  channelLink,
  fetchConversation,
  fetchLead,
  markSent,
  refuse,
  requestDraft,
  type Channel,
  type Conversation,
  type LeadDetail,
} from "./api";

export default function LeadCard({
  companyId,
  onRefused,
}: {
  companyId: string;
  onRefused: () => void;
}) {
  const [lead, setLead] = useState<LeadDetail | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let stale = false;
    fetchLead(companyId).then((data) => !stale && setLead(data));
    return () => {
      stale = true;
    };
  }, [companyId, revision]);

  if (!lead) return <p className="placeholder">Загрузка…</p>;

  return (
    <article className="card">
      <h2>{lead.name}</h2>
      <p className="card-sub">
        {[lead.industry, lead.city, lead.domain].filter(Boolean).join(" · ")}
        {" · intent "}
        <span className="mono">{lead.intent_score.toFixed(1)}</span>
        {" · fit "}
        <span className="mono">{lead.fit_score.toFixed(1)}</span>
      </p>

      <div className="why">
        <p>{lead.why_now}</p>
        {lead.quote && <blockquote>{lead.quote}</blockquote>}
        <span className="tag">
          {lead.from_model ? "обоснование от модели" : "обоснование по шаблону из сигналов"}
        </span>
      </div>

      <h3>Каналы</h3>
      <div className="channels">
        {lead.channels.map((channel) => (
          <ChannelRow
            key={`${channel.kind}:${channel.handle}`}
            channel={channel}
            primary={
              channel.kind === lead.channel?.kind && channel.handle === lead.channel?.handle
            }
          />
        ))}
      </div>

      <h3>Сигналы</h3>
      <ul className="signals">
        {lead.signals.map((signal, index) => (
          <li key={`${signal.type}:${index}`}>
            <span>
              {SIGNAL_LABELS[signal.type] ?? signal.type}
              {signal.url && (
                <>
                  {" · "}
                  <a href={signal.url} target="_blank" rel="noreferrer">
                    источник
                  </a>
                </>
              )}
            </span>
            <span className="weight mono">
              +{signal.weight.toFixed(1)} · {signal.observed_at.slice(0, 10)}
            </span>
            {signal.quote && <span className="signal-quote">«{signal.quote}»</span>}
          </li>
        ))}
        {lead.signals.length === 0 && <li>Ни одного — компания попала в выдачу по fit.</li>}
      </ul>

      {lead.sources.length > 0 && (
        <>
          <h3>Откуда</h3>
          <div className="sources">
            {lead.sources.map((url) => (
              <a key={url} href={url} target="_blank" rel="noreferrer">
                {new URL(url).hostname}
              </a>
            ))}
          </div>
        </>
      )}

      <Thread companyId={companyId} />

      <RefusalForm
        channel={lead.channel}
        onDone={() => {
          setRevision((n) => n + 1);
          onRefused();
        }}
      />
    </article>
  );
}

/** primary — канал, которым система предлагает писать: первый по приоритету
 *  WhatsApp → телефон → почта, не попавший в suppression. Остальные показаны
 *  как запасные, потому что решает всё равно человек. */
function ChannelRow({ channel, primary }: { channel: Channel; primary: boolean }) {
  const [copied, setCopied] = useState(false);

  return (
    <div
      className={`channel${primary ? " is-primary" : ""}${
        channel.suppressed ? " is-suppressed" : ""
      }`}
    >
      <span className="channel-kind">{CHANNEL_LABELS[channel.kind] ?? channel.kind}</span>
      <span className="handle mono">{channel.handle}</span>
      <button
        className="ghost"
        onClick={() => {
          navigator.clipboard.writeText(channel.handle);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        }}
      >
        {copied ? "скопировано" : "копировать"}
      </button>
      {!channel.suppressed && (
        <a className="ghost" href={channelLink(channel)} target="_blank" rel="noreferrer">
          открыть
        </a>
      )}
    </div>
  );
}

/** Переписка с лидом. Черновик правится прямо здесь: в историю треда попадает
 *  то, что оператор реально отправил, — иначе следующий ход агента строился бы
 *  на сообщении, которого лид не получал. */
function Thread({ companyId }: { companyId: string }) {
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [text, setText] = useState("");
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    fetchConversation(companyId)
      .then((data) => !stale && apply(data))
      .catch((error) => !stale && setFailure((error as Error).message));
    return () => {
      stale = true;
    };
  }, [companyId]);

  function apply(data: Conversation) {
    setConversation(data);
    setText(data.draft?.draft_text ?? "");
  }

  async function run(action: () => Promise<Conversation>) {
    setBusy(true);
    setFailure(null);
    try {
      apply(await action());
    } catch (error) {
      setFailure((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!conversation) return <p className="placeholder">Переписка: загрузка…</p>;

  // Ход выбирается по последней реплике: после ответа лида нужен ответ, после
  // нашего сообщения — новый повод. Молчание отличается от диалога только этим.
  const last = conversation.messages[conversation.messages.length - 1];
  const kind = !last ? "first" : last.role === "incoming" ? "reply" : "followup";

  return (
    <section className="thread">
      <h3>Переписка · {conversation.thread_id}</h3>

      <ol className="thread-log">
        {conversation.messages.map((message, index) => (
          <li key={index} className={message.role}>
            <span className="thread-who">{message.role === "outgoing" ? "мы" : "они"}</span>
            <span>{message.text}</span>
          </li>
        ))}
        {conversation.messages.length === 0 && <li className="placeholder">Ещё не писали.</li>}
      </ol>

      {conversation.draft ? (
        <div className="thread-draft">
          <textarea value={text} onChange={(event) => setText(event.target.value)} rows={6} />
          <p className="note">
            Угол: <span className="mono">{conversation.draft.angle}</span>. Правьте текст здесь —
            в историю уйдёт отправленный вариант, исходный черновик сохранится рядом.
          </p>
          <button disabled={busy || !text.trim()} onClick={() => run(() => markSent(companyId, text))}>
            Отправлено
          </button>
        </div>
      ) : (
        <button disabled={busy} onClick={() => run(() => requestDraft(companyId, kind))}>
          {kind === "first"
            ? "Черновик первого сообщения"
            : kind === "reply"
              ? "Черновик ответа"
              : "Черновик с новым поводом"}
        </button>
      )}

      {conversation.messages.length > 0 && (
        <form
          className="thread-reply"
          onSubmit={(event) => {
            event.preventDefault();
            run(() => addIncoming(companyId, reply)).then(() => setReply(""));
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

      {conversation.stop && (
        <p className="note">Агент советует не писать: нового повода в данных нет.</p>
      )}
      {failure && <p className="failure">{failure}</p>}
    </section>
  );
}

/** Отказ — единственное, что возвращается в систему от человека (PRD F21).
 *  Причина обязательна: список никогда не очищается, и через полгода объяснить
 *  запись будет некому. */
function RefusalForm({ channel, onDone }: { channel: Channel | null; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  if (!channel) return null;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSending(true);
    setFailure(null);
    try {
      await refuse(channel!.handle, reason.trim());
      setReason("");
      onDone();
    } catch (error) {
      setFailure((error as Error).message);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="refusal">
      <form onSubmit={submit}>
        <input
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder={`Причина отказа для ${channel.handle}`}
          minLength={3}
          required
        />
        <button type="submit" disabled={sending || reason.trim().length < 3}>
          Больше не писать
        </button>
      </form>
      <p className="note">
        Запись уходит в <code className="mono">state.suppression</code> и переживает
        пересборку базы. Отменить нельзя — список не очищается.
      </p>
      {failure && <p className="failure">{failure}</p>}
    </div>
  );
}
