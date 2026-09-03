"use client";

/** Активная джоба: шаги, счётчик, живой лог. Тело «прозрачности» продукта —
 * посетитель видит не спиннер, а названные шаги и настоящий вывод процесса.
 * Лог тёмный по дизайн-системе (code-block), скроллится сам.
 */

import { useEffect, useRef, useState } from "react";
import { CheckCircleIcon, SpinnerIcon, StopCircleIcon } from "@phosphor-icons/react";
import { cancelJob, startPipeline, type JobStep, type Pipeline } from "@/app/api";
import { useLive } from "./live";

export function PipelineActions({ pipelines }: { pipelines: Pipeline[] }) {
  const { active } = useLive();
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  async function run(kind: string) {
    setBusy(kind);
    setFailure(null);
    try {
      await startPipeline(kind);
    } catch (error) {
      setFailure((error as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="pipeline-actions">
      {pipelines.map((pipeline) => (
        <button
          key={pipeline.kind}
          className="btn"
          disabled={!!active || busy === pipeline.kind}
          onClick={() => run(pipeline.kind)}
          title={pipeline.steps.join(" → ")}
        >
          {busy === pipeline.kind ? "ставится в очередь…" : pipeline.title}
        </button>
      ))}
      {failure && <p className="form-error">{failure}</p>}
      {active && <p className="hint">Очередь занята: {active.title}.</p>}
    </div>
  );
}

export function JobMonitor() {
  const { active, log } = useLive();
  const logRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [log]);

  if (!active) return null;

  return (
    <section className="jobMonitor card">
      <header className="jobMonitor-head">
        <span className="jobMonitor-title">
          {active.status === "running" ? <SpinnerIcon className="spin" size={16} /> : null}
          {active.title}
        </span>
        <span className="jobMonitor-state">{statusLabel(active)}</span>
        {active.status !== "done" && (
          <button className="btn btn-quiet" onClick={() => cancelJob(active.id).catch(() => undefined)}>
            <StopCircleIcon size={14} /> прервать
          </button>
        )}
      </header>

      <ol className="jobSteps">
        {active.steps.map((step) => (
          <li key={`${step.stage}-${step.lane}-${step.name}`} className={stepClass(step)}>
            {step.status === "done" ? (
              <CheckCircleIcon size={14} weight="fill" />
            ) : (
              <span className="step-index" />
            )}
            {step.name}
          </li>
        ))}
      </ol>

      {active.steps
        .filter((step) => step.status === "running" && step.progress?.total)
        .map((step) => (
          <div className="jobProgress" key={`${step.stage}-${step.lane}-${step.name}`}>
            <div className="jobProgress-track">
              <div
                className="jobProgress-fill"
                style={{
                  width: `${Math.round(((step.progress?.current ?? 0) / (step.progress?.total ?? 1)) * 100)}%`,
                }}
              />
            </div>
            <span className="mono">
              {step.progress?.current ?? 0} / {step.progress?.total}
              {step.progress?.label ? ` · ${step.progress.label}` : ""}
            </span>
          </div>
        ))}

      {log.length > 0 && (
        <pre className="log-stream mono" ref={logRef}>
          {log.join("\n")}
        </pre>
      )}
    </section>
  );
}

function stepClass(step: JobStep) {
  if (step.status === "done") return "is-done";
  if (step.status === "running") return "is-current";
  if (step.status === "failed" || step.status === "cancelled") return "is-failed";
  return "";
}

function statusLabel(job: { status: string; step: number; step_count: number }) {
  if (job.status === "running") return `готово ${job.step} из ${job.step_count}`;
  return (
    { queued: "в очереди", done: "готово", failed: "не прошёл", cancelled: "прервано" } as Record<string, string>
  )[job.status] ?? job.status;
}
