"use client";

/** Карточка компании по данным research: почему сейчас, досье, разбивка оценки
 *  по сигналам, контакты, попытки и форма отказа. Всё, что пойдёт в первое
 *  сообщение, оператор видит до того, как писать. */

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  CHANNEL_LABELS,
  SIGNAL_LABELS,
  channelLink,
  fetchCityNames,
  fetchThreads,
  type CompanyResearch,
  type Contact,
  type ResearchHook,
  type ThreadSummary,
} from "@/app/api";
import { day, moment, researchStatus } from "@/app/outcomes";
import RefusalForm from "@/components/RefusalForm";

export default function LeadDossier({
  card,
  actions,
  onRefused,
}: {
  card: CompanyResearch;
  actions: React.ReactNode;
  onRefused: () => void;
}) {
  const { dossier } = card;
  const [cities, setCities] = useState<Record<string, string>>({});
  useEffect(() => {
    fetchCityNames().then(setCities).catch(() => undefined);
  }, []);
  return (
    <article className="card">
      <h2>{card.name}</h2>
      <p className="card-sub">
        {[dossier?.what_they_do, cities[card.city] ?? card.city].filter(Boolean).join(" · ")}
        {" · оценка "}
        <span className="mono">{card.score.toFixed(1)}</span>
        {card.researched_at && ` · исследована ${moment(card.researched_at)}`}
        {card.model && <span className="mono"> · {card.model}</span>}
        {card.versions > 1 && ` · версий досье ${card.versions}`}
      </p>

      <div className="lead-actions">
        {card.refused && <span className="tone is-stop">в отказах</span>}
        {!card.refused && !card.lead && <span className="tone is-idle">не лид</span>}
        {actions}
      </div>
      {!card.lead && !card.refused && (
        <p className="note">
          Лид — досье с подтверждённой зацепкой, канал для WhatsApp и ни одного отказа.
        </p>
      )}

      {card.why_now && (
        <div className="why">
          <p>{card.why_now.angle}</p>
          <blockquote>«{card.why_now.quote}»</blockquote>
          <HookSource hook={card.why_now} />
        </div>
      )}

      <h3>Досье</h3>
      {dossier ? (
        <div className="dossier">
          <p>{dossier.summary}</p>
          <p>
            <b>Как заходить:</b> {dossier.approach}
          </p>
          {dossier.decision_maker_hint && (
            <p>
              <b>Кто решает:</b> {dossier.decision_maker_hint}
            </p>
          )}
          <h4>Зацепки</h4>
          <ul className="signals">
            {dossier.hooks.map((hook, index) => (
              <li key={index}>
                <span>{hook.angle}</span>
                <span className="signal-quote">«{hook.quote}»</span>
                <span className="signal-quote">
                  <HookSource hook={hook} />
                </span>
              </li>
            ))}
          </ul>
          {dossier.hooks.length === 0 && <p className="note">Подтверждённых зацепок нет.</p>}
          {dossier.pains.length > 0 && (
            <>
              <h4>Боли</h4>
              <ul className="signals">
                {dossier.pains.map((pain, index) => (
                  <li key={index}>
                    <span>{pain.statement}</span>
                    <span className={`tone is-${pain.severity === "видно явно" ? "attention" : "idle"}`}>
                      {pain.severity}
                    </span>
                    {pain.evidence.map((line, n) => (
                      <span key={n} className="signal-quote">
                        {line}
                      </span>
                    ))}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      ) : (
        <p className="note">Досье нет: компанию ещё не исследовали или попытки не удались.</p>
      )}

      <h3>Разбивка оценки</h3>
      <div className="settings-table">
        <table className="table">
          <thead>
            <tr>
              <th>сигнал</th>
              <th>вес</th>
              <th>возраст</th>
              <th>вклад</th>
              <th>цитата</th>
              <th>источник</th>
            </tr>
          </thead>
          <tbody>
            {card.breakdown.map((part) => (
              <tr key={part.signal}>
                <td>{SIGNAL_LABELS[part.signal] ?? part.signal}</td>
                <td className="mono">{part.weight}</td>
                <td className="mono">{part.age_days} дн.</td>
                <td className="mono">+{part.contribution.toFixed(2)}</td>
                <td>«{part.quote}»</td>
                <td>
                  <a href={part.url} target="_blank" rel="noreferrer">
                    {hostname(part.url)}
                  </a>
                  {` · ${day(part.observed_at)}`}
                </td>
              </tr>
            ))}
            {card.breakdown.length === 0 && (
              <tr>
                <td colSpan={6} className="note">
                  Сигналов нет — оценка нулевая.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <h3>Контакты</h3>
      <div className="channels">
        {card.contacts.map((contact) => (
          <ContactRow
            key={`${contact.kind}:${contact.handle}`}
            contact={contact}
            primary={contact.kind === card.channel?.kind && contact.handle === card.channel?.handle}
            refused={card.refused}
          />
        ))}
        {card.contacts.length === 0 && <p className="note">Контактов нет.</p>}
      </div>

      <h3>Попытки</h3>
      {card.attempts.length > 0 ? (
        <div className="settings-table">
          <table className="table stack-table">
            <thead>
              <tr>
                <th>партия</th>
                <th>статус</th>
                <th>попытка</th>
                <th>причина</th>
                <th>отброшено цитат</th>
                <th>закончена</th>
              </tr>
            </thead>
            <tbody>
              {card.attempts.map((attempt) => {
                const status = researchStatus(attempt.status);
                return (
                  <tr key={attempt.batch_id}>
                    <td data-label="партия" className="mono">{attempt.batch_id}</td>
                    <td data-label="статус">
                      <span className={`tone is-${status.tone}`}>{status.label}</span>
                    </td>
                    <td data-label="попытка" className="mono">{attempt.attempt ?? "—"}</td>
                    <td data-label="причина">{attempt.error ?? "—"}</td>
                    <td data-label="отброшено цитат" className="mono">{attempt.rejected_quotes}</td>
                    <td data-label="закончена" className="mono">{moment(attempt.finished_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="note">Компанию ещё не брали в партию.</p>
      )}

      <ThreadSummaryRow companyId={card.company_id} />

      {!card.refused && (
        <RefusalForm channel={card.channel ?? card.contacts[0] ?? null} onDone={onRefused} />
      )}
    </article>
  );
}

function HookSource({ hook }: { hook: ResearchHook }) {
  return (
    <span className="tag">
      <a href={hook.url} target="_blank" rel="noreferrer">
        {hook.source === "reviews" ? "отзыв 2GIS" : hostname(hook.url)}
      </a>
      {` · ${day(hook.observed_at)}`}
    </span>
  );
}

/** primary — канал, которым система предлагает писать в WhatsApp. */
function ContactRow({
  contact,
  primary,
  refused,
}: {
  contact: Contact;
  primary: boolean;
  refused: boolean;
}) {
  const [copied, setCopied] = useState(false);

  return (
    <div className={`channel${primary ? " is-primary" : ""}${refused ? " is-suppressed" : ""}`}>
      <span className="channel-kind">{CHANNEL_LABELS[contact.kind] ?? contact.kind}</span>
      <span className="handle mono">{contact.handle}</span>
      <button
        className="ghost"
        onClick={() => {
          navigator.clipboard.writeText(contact.handle);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        }}
      >
        {copied ? "скопировано" : "копировать"}
      </button>
      {!refused && (
        <a className="ghost" href={channelLink(contact)} target="_blank" rel="noreferrer">
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
          <b className="mono">{thread.replies}</b>. <Link href="/threads">Открыть диалог →</Link>
        </p>
      ) : (
        <p className="note">
          Переписки ещё не было. <Link href="/threads">Открыть «Диалоги» →</Link>
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
