"use client";

import { useEffect, useState } from "react";
import { activateRun, fetchRuns, type Run } from "@/app/api";

export function RunsHistory() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    fetchRuns().then((data) => setRuns(data.runs)).catch(() => undefined);
  }, []);

  async function rollback(runId: number) {
    setMessage(null);
    try {
      await activateRun(runId);
      setMessage(`выдача переключена на прогон ${runId}`);
    } catch (error) {
      setMessage((error as Error).message);
    }
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
              {run.finished_at ? run.finished_at.slice(0, 19) : "не завершён"}
            </span>
            <button className="btn btn-quiet" onClick={() => rollback(run.run_id)}>
              вернуть выдачу
            </button>
          </div>
        ))}
      </div>
      {message && <p className="hint">{message}</p>}
    </section>
  );
}