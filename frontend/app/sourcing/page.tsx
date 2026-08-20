"use client";

/** Система 1: сбор и обогащение лидов. Слева выдача с фильтрами, справа
 * карточка лида. Событие refresh из SSE перечитывает список: после пересборки
 * базы цифры и состав выдачи меняются сами, без F5.
 */

import { useCallback, useEffect, useState } from "react";
import { fetchCatalogue, fetchLeads, type Lead, type Pipeline, type Stats } from "../api";
import LeadCard from "../LeadCard";
import { JobMonitor, PipelineActions } from "@/components/JobMonitor";
import { useLive } from "@/components/live";

// Фиксированные шаги плюс «все» с числом из stats.available: сколько лидов
// доступно на самом деле, знает только бэкенд.
const LIMITS = [30, 60, 100];

export default function Sourcing() {
  const { refreshTick } = useLive();
  const [leads, setLeads] = useState<Lead[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [city, setCity] = useState("");
  const [limit, setLimit] = useState(LIMITS[0]);
  const [failure, setFailure] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  // Отказ убирает канал, а вместе с ним иногда и сам лид: список обязан
  // перечитаться, иначе оператор напишет тому, кому только что запретил.
  const reload = useCallback(() => setRevision((n) => n + 1), []);

  useEffect(() => {
    fetchCatalogue()
      .then((c) => setPipelines(c.pipelines.filter((p) => p.kind !== "write")))
      .catch(() => undefined);
  }, []);

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
  }, [limit, city, revision, refreshTick]);

  if (failure) {
    return (
      <div className="failure">
        <b>Бэкенд недоступен.</b> {failure}
        <br />
        Запустить: <span className="mono">uv run --env-file .env uvicorn api:app --port 8787</span>
      </div>
    );
  }

  return (
    <>
      <header className="page-head">
        <h1>Сбор лидов</h1>
        {stats && (
          <div className="counters">
            <span>
              компаний <b>{stats.companies}</b>
            </span>
            <span>
              с сигналами <b>{stats.with_intent}</b>
            </span>
            <span>
              с каналом <b>{stats.available}</b>
            </span>
            <span>
              отказов <b>{stats.suppressed}</b>
            </span>
          </div>
        )}
      </header>

      <PipelineActions pipelines={pipelines} />
      <JobMonitor />

      <div className="controls" style={{ margin: "0 0 12px" }}>
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
          {stats && <option value={stats.available}>все {stats.available}</option>}
        </select>
      </div>

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

        <div className="detail">
          {selected ? (
            <LeadCard companyId={selected} onRefused={reload} />
          ) : (
            <p className="placeholder">Выберите компанию слева.</p>
          )}
        </div>
      </div>
    </>
  );
}
