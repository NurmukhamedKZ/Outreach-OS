"use client";

/** Живые данные всего приложения из одного SSE-соединения.

Продукт продаётся прозрачностью: счётчики, статусы джоб и строки лога
прилетают сами, без опроса. Провайдер один на дерево, кладёт в контекст
снапшот (при подключении), апсерт джоб по событию job, хвост лога активной
джобы по событию log, а на refresh перечитывает счётчики.

Соединение рвётся — показываем честный индикатор в сайдбаре; EventSource
переподключится сам и снова принесёт снапшот целиком.
*/

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { fetchActivity, fetchJob, fetchStats, subscribeEvents,
  type ActivityEvent, type DashboardStats, type Job } from "@/app/api";

const LOG_CAP = 400;
const ACTIVITY_CAP = 200;

type LiveState = {
  stats: DashboardStats | null;
  jobs: Job[];
  active: Job | null;
  log: string[];
  connected: boolean;
  /** Тикает на каждом событии refresh: пересборка базы меняет данные, и
   * страницы со списками перечитывают их, не дожидаясь F5. */
  refreshTick: number;
  /** Лента журнала фоновой работы: живое приходит по SSE, первый экран — запросом. */
  activityTail: ActivityEvent[];
};

const LiveContext = createContext<LiveState>({
  stats: null,
  jobs: [],
  active: null,
  log: [],
  connected: false,
  refreshTick: 0,
  activityTail: [],
});

export function useLive() {
  return useContext(LiveContext);
}

export function LiveProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<LiveState>({
    stats: null,
    jobs: [],
    active: null,
    log: [],
    connected: false,
    refreshTick: 0,
    activityTail: [],
  });
  const activeId = useRef<number | null>(null);

  const refreshStats = useCallback(() => {
    fetchStats()
      .then((stats) => setState((prev) => ({ ...prev, stats })))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    refreshStats();
    const unsubscribe = subscribeEvents((event) => {
      const data = JSON.parse((event as MessageEvent).data);
      if (data.type === "snapshot") {
        const stats = data.stats as DashboardStats;
        activeId.current = stats.jobs.active?.id ?? null;
        setState((prev) => ({
          ...prev,
          stats,
          jobs: stats.jobs.recent,
          active: stats.jobs.active,
          log: [],
        }));
      } else if (data.type === "job") {
        const job = data.job as Job;
        setState((prev) => {
          const jobs = [job, ...prev.jobs.filter((j) => j.id !== job.id)].slice(0, 20);
          // Самая старая из живых, а не первая по порядку в массиве: воркер
          // исполняет по одной, строго по id, и вторая поставленная джоба не
          // обгоняет первую только потому, что её событие приехало последним.
          const running = jobs.filter(isActive);
          const stillActive = running.length
            ? running.reduce((oldest, candidate) => (candidate.id < oldest.id ? candidate : oldest))
            : null;
          // Ref обновляется здесь же, из того же stillActive, а не отдельной
          // веткой на приехавшем job: раньше кольцо джоб решало, кто активен,
          // а activeId.current — отдельным условием на самом job, и они могли
          // разойтись, если в очереди одновременно оказывалось больше одной
          // джобы (несколько вкладок дашборда, например) — тогда лог активной
          // джобы переставал долавливаться, хотя шаги шли исправно.
          activeId.current = stillActive?.id ?? null;
          return {
            ...prev,
            jobs,
            active: stillActive,
            log: stillActive?.id === prev.active?.id ? prev.log : [],
          };
        });
      } else if (data.type === "log" && data.job_id === activeId.current) {
        setState((prev) => ({ ...prev, log: [...prev.log, ...data.lines].slice(-LOG_CAP) }));
      } else if (data.type === "activity") {
        setState((prev) => ({
          ...prev,
          activityTail: [data.event as ActivityEvent, ...prev.activityTail].slice(0, ACTIVITY_CAP),
        }));
      } else if (data.type === "refresh") {
        refreshStats();
        setState((prev) => ({ ...prev, refreshTick: prev.refreshTick + 1 }));
      }
    });
    setState((prev) => ({ ...prev, connected: true }));
    return () => {
      unsubscribe();
      setState((prev) => ({ ...prev, connected: false }));
    };
  }, [refreshStats]);

  // Хвост лога добирается GET /api/jobs/{id}?offset= при смене активной джобы —
  // подстраховка на случай, когда инкрементальные события log потерялись
  // (реконнект SSE, обгон события "job") и живой стрим сам себя не починит.
  const activeJobId = state.active?.id ?? null;
  useEffect(() => {
    if (activeJobId === null) return;
    fetchJob(activeJobId, 0)
      .then((tail) => {
        setState((prev) =>
          prev.active?.id === activeJobId
            ? { ...prev, log: tail.lines.slice(-LOG_CAP) }
            : prev
        );
      })
      .catch(() => undefined);
  }, [activeJobId]);

  const value = useMemo(() => state, [state]);
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

function isActive(job: Job) {
  return job.status === "queued" || job.status === "running";
}
