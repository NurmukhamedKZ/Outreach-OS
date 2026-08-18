"use client";

import { useCallback, useEffect, useState } from "react";
import { activateRun, fetchRuns, type Run } from "@/app/api";
import { useLive } from "./live";

export function RunsHistory() {
  // refreshTick приходит по SSE, когда джоба закончилась или выдачу переключили:
  // без него список прогонов устаревает ровно в тот момент, когда он и нужен.
  const { refreshTick } = useLive();
  const [runs, setRuns] = useState<Run[]>([]);
  const [message, setMessage] = useState<string | null>(null);

  const reload = useCallback(() => {
    fetchRuns().then((data) => setRuns(data.runs)).catch(() => undefined);
  }, []);

  useEffect(reload, [reload, refreshTick]);

  async function rollback(runId: number) {
    setMessage(null);
    try {
      await activateRun(runId);
      setMessage(`выдача переключена на прогон ${runId}`);
    } catch (error) {
      setMessage((error as Error).message);
    }
    reload();
  }

  return (
    <section className="card">
      <h3>История прогонов</h3>
      <div className="jobs-list">
        {runs.map((run) => (
          <div key={run.run_id} className="jobs-row">
            <span className="jobs-title">
              прогон {run.run_id}{run.note ? ` · ${run.note}` : ""}
            </span>
            <span className="jobs-when mono">
              {run.finished_at ? run.finished_at.slice(0, 19) : "брошен"}
            </span>
            <button
              className="btn btn-quiet"
              disabled={!run.finished_at}
              title={run.finished_at ? undefined : "прогон не завершён — публиковать нечего"}
              onClick={() => rollback(run.run_id)}
            >
              вернуть выдачу
            </button>
          </div>
        ))}
        {runs.length === 0 && <p className="note">Прогонов ещё не было.</p>}
      </div>
      {message && <p className="hint">{message}</p>}
    </section>
  );
}
