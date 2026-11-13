"use client";

/** Лиды модуля research: досье с подтверждённой зацепкой, канал для WhatsApp,
 *  без отказа. Порядок и оценку считает бэкенд; список перечитывается, когда
 *  партия закончилась. */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  CHANNEL_LABELS,
  fetchResearchLeads,
  fetchCityNames,
  researchError,
  type ResearchLead,
} from "../api";
import { day } from "../outcomes";
import BatchPanel from "@/components/BatchPanel";

export default function Leads() {
  const [leads, setLeads] = useState<ResearchLead[] | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  // Лид несёт id города 2GIS; название — из справочника модуля companies.
  const [cities, setCities] = useState<Record<string, string>>({});

  useEffect(() => {
    fetchCityNames().then(setCities).catch(() => undefined);
  }, []);

  const load = useCallback(() => {
    fetchResearchLeads()
      .then((data) => {
        setLeads(data);
        setFailure(null);
      })
      .catch((error) => setFailure(researchError(error)));
  }, []);

  useEffect(load, [load]);

  return (
    <>
      <header className="page-head">
        <h1>Лиды</h1>
        {leads && (
          <div className="counters">
            <span>
              лидов <b>{leads.length}</b>
            </span>
          </div>
        )}
      </header>

      <BatchPanel onFinished={load} />

      {failure && (
        <div className="failure">
          <b>Лиды не загрузились.</b> {failure}
        </div>
      )}

      <div className="roster">
        {leads?.map((lead, index) => (
          <div key={lead.company_id} className="row lead-row">
            <span className="rank mono">{index + 1}</span>
            <span className="row-text">
              <Link href={`/leads/${encodeURIComponent(lead.company_id)}`} className="row-name">
                {lead.name}
              </Link>
              <span className="row-sub mono">
                {cities[lead.city] ?? lead.city} ·{" "}
                {CHANNEL_LABELS[lead.channel.kind] ?? lead.channel.kind} {lead.channel.handle}
              </span>
              <span className="lead-why">
                {lead.why_now.angle}
                <blockquote>«{lead.why_now.quote}»</blockquote>
                <span className="row-sub">
                  <a href={lead.why_now.url} target="_blank" rel="noreferrer">
                    {lead.why_now.source === "reviews" ? "отзыв 2GIS" : "сайт"}
                  </a>
                  {" · "}
                  {day(lead.why_now.observed_at)}
                </span>
              </span>
            </span>
            <span className="intent mono" title="оценка">
              {lead.score.toFixed(1)}
            </span>
          </div>
        ))}
        {leads?.length === 0 && (
          <p className="placeholder">
            Лидов пока нет: запустите партию — лидом становится компания с подтверждённой
            зацепкой и каналом для WhatsApp.
          </p>
        )}
      </div>
    </>
  );
}
