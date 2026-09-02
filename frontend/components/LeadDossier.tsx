"use client";

/** Полное досье лида: почему сейчас, разбор скора, досье модели, каналы,
 *  сигналы, сырьё и оплаченные ответы модели. Всё, что пошло или может пойти
 *  в промпт, оператор видит до того, как писать.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  CHANNEL_LABELS,
  SIGNAL_LABELS,
  channelLink,
  fetchThreads,
  type Channel,
  type LeadDetail,
  type ThreadSummary,
} from "@/app/api";
import RefusalForm from "@/components/RefusalForm";

export default function LeadDossier({
  lead,
  onRefused,
}: {
  lead: LeadDetail;
  onRefused: () => void;
}) {
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

      <h3>Разбор скора</h3>
      <table className="table">
        <thead>
          <tr>
            <th>правило</th>
            <th>вклад</th>
            <th>цитата</th>
            <th>источник</th>
          </tr>
        </thead>
        <tbody>
          {lead.breakdown.map((part, index) => (
            <tr key={index}>
              <td>{part.rule ?? part.signal ?? "…"}</td>
              <td className="mono">+{part.contribution.toFixed(1)}</td>
              <td>{part.quote && `«${part.quote}»`}</td>
              <td>
                {part.url && (
                  <a href={part.url} target="_blank" rel="noreferrer">
                    {hostname(part.url)}
                  </a>
                )}
                {part.observed_at ? ` · ${part.observed_at.slice(0, 10)}` : ""}
              </td>
            </tr>
          ))}
          {lead.breakdown.length === 0 && (
            <tr>
              <td colSpan={4} className="note">
                Разбора нет — компания попала по фиту.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <h3>Досье модели</h3>
      {lead.dossier ? (
        <div className="dossier">
          {lead.dossier.summary && (
            <p>
              <b>Чем занимается:</b> {lead.dossier.summary}
            </p>
          )}
          {lead.dossier.approach && (
            <p>
              <b>Как заходить:</b> {lead.dossier.approach}
            </p>
          )}
          {lead.dossier.decision_maker && (
            <p>
              <b>Кто решает:</b> {lead.dossier.decision_maker}
            </p>
          )}
          <ul className="signals">
            {lead.dossier.hooks.map((hook, index) => (
              <li key={index}>
                <span>{hook.angle}</span>
                <span className="weight mono">
                  {hook.source ?? ""}
                  {hook.observed_at ? ` · ${hook.observed_at.slice(0, 10)}` : ""}
                </span>
                <span className="signal-quote">«{hook.quote}»</span>
                {hook.url && (
                  <span className="signal-quote">
                    <a href={hook.url} target="_blank" rel="noreferrer">
                      {hostname(hook.url)}
                    </a>
                  </span>
                )}
              </li>
            ))}
          </ul>
          {lead.dossier.hooks.length === 0 && <p className="note">Зацепок нет.</p>}
        </div>
      ) : (
        <p className="note">Досье модели для этой компании нет.</p>
      )}

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
        {lead.channels.length === 0 && <p className="note">Каналов не осталось.</p>}
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

      <h3>Сырьё</h3>
      <table className="table">
        <thead>
          <tr>
            <th>url</th>
            <th>финальный</th>
            <th>статус</th>
            <th>скачано</th>
          </tr>
        </thead>
        <tbody>
          {lead.fetches.map((fetch, index) => (
            <tr key={index}>
              <td>
                <a href={fetch.url} target="_blank" rel="noreferrer">
                  {hostname(fetch.url)}
                </a>
              </td>
              <td>{fetch.final_url && fetch.final_url !== fetch.url ? hostname(fetch.final_url) : "—"}</td>
              <td className="mono">{fetch.status ?? "…"}</td>
              <td className="mono">{fetch.fetched_at ? fetch.fetched_at.slice(0, 16) : "…"}</td>
            </tr>
          ))}
          {lead.fetches.length === 0 && (
            <tr>
              <td colSpan={4} className="note">
                Скачанных страниц нет.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <h3>Ответы модели</h3>
      {lead.llm_answers.map((answer, index) => (
        <details key={index} className="prompt-details">
          <summary>
            {answer.kind} · {answer.model}
          </summary>
          <div className="prompt-block mono">
            <div className="prompt-pair">
              <span className="prompt-role">prompt</span>
              <pre>{answer.prompt}</pre>
            </div>
            <div className="prompt-pair">
              <span className="prompt-role">answer</span>
              <pre>{answer.answer}</pre>
            </div>
          </div>
        </details>
      ))}
      {lead.llm_answers.length === 0 && (
        <p className="note">За эту компанию модель ещё не отвечала.</p>
      )}

      <ThreadSummaryRow companyId={lead.company_id} />

      <RefusalForm channel={lead.channel} onDone={onRefused} />
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

/** Переписка в карточке — сводка и ссылка на «Диалоги»: сам диалог ведётся
 *  там, а не в двух местах. Композера здесь нет намеренно. */
function ThreadSummaryRow({ companyId }: { companyId: string }) {
  const [thread, setThread] = useState<ThreadSummary | null>(null);

  useEffect(() => {
    let stale = false;
    fetchThreads()
      .then((data) =>
        !stale && setThread(data.threads.find((row) => row.company_id === companyId) ?? null),
      )
      .catch(() => undefined);
    return () => {
      stale = true;
    };
  }, [companyId]);

  return (
    <section className="thread">
      <h3>Переписка</h3>
      {thread ? (
        <p className="note">
          Отправлено <b className="mono">{thread.sent}</b>, ответов от лида{" "}
          <b className="mono">{thread.replies}</b>.{" "}
          <Link href="/threads">Открыть диалог →</Link>
        </p>
      ) : (
        <p className="note">
          Переписки ещё не было. <Link href="/threads">Открыть «Диалоги» →</Link>{" "}
          или напишите первой строкой из «Холодных».
        </p>
      )}
    </section>
  );
}

function hostname(url: string) {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}
