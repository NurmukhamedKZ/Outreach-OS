"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { fetchLeads, type Lead, type Stats } from "./api";
import LeadCard from "./LeadCard";

// Фиксированные шаги плюс «все» с числом из stats.available: сколько лидов
// доступно на самом деле, знает только бэкенд, и зашивать это в список нельзя —
// оно меняется с каждой пересборкой.
const LIMITS = [30, 60, 100];

export default function Console() {
  const [leads, setLeads] = useState<Lead[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [city, setCity] = useState("");
  const [limit, setLimit] = useState(LIMITS[0]);
  const [failure, setFailure] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  // Отказ убирает канал, а вместе с ним иногда и сам лид: список обязан
  // перечитаться, иначе оператор напишет тому, кому только что запретил.
  const reload = useCallback(() => setRevision((n) => n + 1), []);

  useEffect(() => {
    let stale = false;
    fetchLeads(limit, city)
      .then((data) => {
        if (stale) return;
        setLeads(data.leads);
        setStats(data.stats);
        setFailure(null);
      })
      .catch((error: Error) => !stale && setFailure(error.message));
    return () => {
      stale = true;
    };
  }, [limit, city, revision]);

  if (failure) {
    return (
      <main className="console">
        <div className="failure">
          <b>Бэкенд недоступен.</b> {failure}
          <br />
          Запустить: <code className="mono">uv run uvicorn api:app --port 8787</code>
        </div>
      </main>
    );
  }

  return (
    <main className="console">
      <header className="masthead">
        <h1>Лиды</h1>
        {stats && (
          <div className="counters">
            <span>
              компаний <b className="mono">{stats.companies}</b>
            </span>
            <span>
              с сигналами <b className="mono">{stats.with_intent}</b>
            </span>
            <span>
              с каналом <b className="mono">{stats.available}</b>
            </span>
            <span>
              в выдаче <b className="mono">{leads.length}</b>
            </span>
            <span>
              отказов <b className="mono">{stats.suppressed}</b>
            </span>
          </div>
        )}
        <div className="controls">
          <Link className="ghost" href="/runs">
            Скрипты
          </Link>
          <select value={city} onChange={(event) => setCity(event.target.value)}>
            <option value="">все города</option>
            {stats?.cities.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
            {LIMITS.filter((value) => !stats || value < stats.available).map((value) => (
              <option key={value} value={value}>
                {value} лидов
              </option>
            ))}
            {stats && (
              <option value={stats.available}>все {stats.available}</option>
            )}
          </select>
        </div>
      </header>

      <div className="split">
        <div className="roster">
          {leads.map((lead, index) => (
            <button
              key={lead.company_id}
              className="row"
              aria-current={lead.company_id === selected}
              onClick={() => setSelected(lead.company_id)}
            >
              <span className="rank mono">{index + 1}</span>
              <span className="row-text">
                <span className="row-name">{lead.name}</span>
                <span className="row-sub mono">
                  {lead.city} · {lead.channel?.handle}
                </span>
              </span>
              <span className="intent mono">{lead.intent_score.toFixed(1)}</span>
            </button>
          ))}
          {leads.length === 0 && (
            <p className="placeholder">Ни одного лида с рабочим каналом.</p>
          )}
        </div>

        {selected ? (
          <LeadCard companyId={selected} onRefused={reload} />
        ) : (
          <p className="placeholder">Выберите компанию слева.</p>
        )}
      </div>
    </main>
  );
}
