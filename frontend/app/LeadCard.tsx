"use client";

import { useEffect, useState } from "react";
import {
  CHANNEL_LABELS,
  SIGNAL_LABELS,
  channelLink,
  fetchLead,
  refuse,
  type Channel,
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
        Запись уходит в <code className="mono">suppression.csv</code> и переживает пересборку
        базы. Отменить нельзя — список не очищается.
      </p>
      {failure && <p className="failure">{failure}</p>}
    </div>
  );
}
