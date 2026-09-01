"use client";

/** Лиды: выдача с рабочим каналом и обоснованием. Строки — ссылки на
 *  /leads/[id]; пересборка базы перечитывает список сама (событие refresh).
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { fetchCatalogue, fetchLeads, type Lead, type Pipeline, type Stats } from "../api";
import { PipelineActions } from "@/components/JobMonitor";
import { useLive } from "@/components/live";

// Фиксированные шаги плюс «все» с числом из stats.available: сколько лидов
// доступно на самом деле, знает только бэкенд.
const LIMITS = [30, 60, 100];

export default function Leads() {
  const { refreshTick } = useLive();
  const [leads, setLeads] = useState<Lead[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [city, setCity] = useState("");
  const [limit, setLimit] = useState(LIMITS[0]);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    fetchCatalogue()
      .then((c) =>
        setPipelines(c.pipelines.filter((p) => p.kind === "discover" || p.kind === "classify")),
      )
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
  }, [limit, city, refreshTick]);

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
        <h1>Лиды</h1>
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

      <div className="roster">
        {leads.map((lead, index) => (
          <Link
            key={lead.company_id}
            href={`/leads/${encodeURIComponent(lead.company_id)}`}
            className="row"
          >
            <span className="rank mono">{index + 1}</span>
            <span className="row-text">
              <span className="row-name">{lead.name}</span>
              <span className="row-sub mono">
                {lead.city} · {lead.channel?.handle}
              </span>
            </span>
            <span className="intent mono">{lead.intent_score.toFixed(1)}</span>
          </Link>
        ))}
        {leads.length === 0 && (
          <p className="placeholder">Ни одного лида с рабочим каналом.</p>
        )}
      </div>
    </>
  );
}
