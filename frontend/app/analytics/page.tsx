"use client";

import { useEffect, useState } from "react";
import { fetchAnalytics, type AnalyticsReport } from "../api";
import { useLive } from "@/components/live";

const DIAGNOSIS_LABELS: Record<string, string> = {
  ok: "Воронка в норме",
  reply_rate_low: "Ответов меньше 2% — чинить текст и оффер",
  icp_mismatch: "Ответы есть, встреч нет — чинить ICP",
};

const STEP_LABELS: Record<string, string> = {
  sent: "Отправлено",
  delivered: "Доставлено",
  replied: "Ответили",
  dialog: "Разговор",
  meeting_agreed: "Согласились",
  meeting_held: "Встреча состоялась",
};

export default function AnalyticsPage() {
  const { refreshTick } = useLive();
  const [report, setReport] = useState<AnalyticsReport | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    fetchAnalytics(30)
      .then((data) => !stale && setReport(data))
      .catch((error: Error) => !stale && setFailure(error.message));
    return () => {
      stale = true;
    };
  }, [refreshTick]);

  if (failure) {
    return (
      <div className="failure">
        <b>Бэкенд недоступен.</b> {failure}
      </div>
    );
  }

  if (!report) {
    return <p className="placeholder">Аналитика загружается…</p>;
  }

  return (
    <>
      <header className="page-head">
        <h1>Аналитика</h1>
        <span className="page-sub">воронка за {report.days} дней</span>
      </header>

      <section className="card" style={{ marginBottom: 16 }}>
        <b>{DIAGNOSIS_LABELS[report.diagnosis] ?? report.diagnosis}</b>
      </section>

      <section className="card" style={{ marginBottom: 16 }}>
        <h3>Воронка</h3>
        <table className="table">
          <thead>
            <tr>
              <th>Шаг</th>
              <th>Абсолют</th>
              <th>Доля отправленных</th>
            </tr>
          </thead>
          <tbody>
            {report.funnel.map((row, index) => {
              // Доля от начала воронки, а не от предыдущего шага: исход треда
              // ставит человек и вправе отметить встречу в треде, который до
              // диалога не дорос, — от предыдущего шага это дало бы больше
              // 100%. От sent такого не бывает: каждый шаг считается среди
              // отправленных.
              const sent = report.funnel[0]?.count ?? 0;
              const pct = index === 0 || !sent
                ? "—"
                : `${Math.round((row.count / sent) * 100)}%`;
              return (
                <tr key={row.step}>
                  <td>{STEP_LABELS[row.step] ?? row.step}</td>
                  <td className="mono">{row.count}</td>
                  <td className="mono">{pct}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section className="card" style={{ marginBottom: 16 }}>
        <h3>Разрез по офферу</h3>
        <BreakdownTable rows={report.by_offer} />
      </section>

      <section className="card" style={{ marginBottom: 16 }}>
        <h3>Разрез по поводу</h3>
        <BreakdownTable rows={report.by_angle} />
      </section>

      <section className="card" style={{ marginBottom: 16 }}>
        <h3>Разрез по сегменту</h3>
        <BreakdownTable rows={report.by_segment} />
      </section>

      <section className="card">
        <p>
          Доля правок оператора: <span className="mono">{Math.round(report.edited_share * 100)}%</span>
        </p>
      </section>
    </>
  );
}

function BreakdownTable({ rows }: { rows: { key: string; sent: number; replied: number; meetings: number }[] }) {
  if (rows.length === 0) {
    return <p className="note">данных пока нет</p>;
  }
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Ключ</th>
          <th>Отправлено</th>
          <th>Ответили</th>
          <th>Встречи</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.key}>
            <td>{row.key}</td>
            <td className="mono">{row.sent}</td>
            <td className="mono">{row.replied}</td>
            <td className="mono">{row.meetings}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
