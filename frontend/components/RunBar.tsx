"use client";

/** Узкая полоска активной джобы вверху каждого экрана. Пока что-то идёт —
 * оно видно отовсюду; остановить можно прямо отсюда, лог — по ссылке на
 * «Процессы». Пусто, когда очередь свободна: спокойный дашборд без задачи
 * не должен выглядеть сломанным.
 */

import Link from "next/link";
import { SpinnerIcon, StopCircleIcon } from "@phosphor-icons/react";
import { cancelJob } from "@/app/api";
import { useLive } from "./live";

export default function RunBar() {
  const { active } = useLive();
  if (!active) return null;

  return (
    <div className="runbar">
      <span className="runbar-title">
        {active.status === "running" ? <SpinnerIcon className="spin" size={14} /> : null}
        {active.title}
      </span>
      <span className="runbar-step mono">
        {active.status === "running" ? `готово ${active.step} из ${active.step_count}` : active.status}
      </span>
      <Link href="/activity" className="runbar-link">
        лог
      </Link>
      <button className="btn btn-quiet" onClick={() => cancelJob(active.id).catch(() => undefined)}>
        <StopCircleIcon size={14} /> прервать
      </button>
    </div>
  );
}
