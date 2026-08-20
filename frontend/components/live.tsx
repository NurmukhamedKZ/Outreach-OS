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
import { fetchStats, subscribeEvents, type DashboardStats, type Job } from "@/app/api";

const LOG_CAP = 400;

type LiveState = {
  stats: DashboardStats | null;
  jobs: Job[];
  active: Job | null;
  log: string[];
  connected: boolean;
  /** Тикает на каждом событии refresh: пересборка базы меняет данные, и
   * страницы со списками перечитывают их, не дожидаясь F5. */
  refreshTick: number;
};

const LiveContext = createContext<LiveState>({
  stats: null,
  jobs: [],
  active: null,
  log: [],
  connected: false,
  refreshTick: 0,
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
        activeId.current =
          job.status === "queued" || job.status === "running" ? job.id : activeId.current;
        setState((prev) => {
          const jobs = [job, ...prev.jobs.filter((j) => j.id !== job.id)].slice(0, 20);
          const stillActive = jobs.find(isActive) ?? null;
          return {
            ...prev,
            jobs,
            active: stillActive,
            log: stillActive?.id === prev.active?.id ? prev.log : [],
          };
        });
      } else if (data.type === "log" && data.job_id === activeId.current) {
        setState((prev) => ({ ...prev, log: [...prev.log, ...data.lines].slice(-LOG_CAP) }));
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

  const value = useMemo(() => state, [state]);
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

function isActive(job: Job) {
  return job.status === "queued" || job.status === "running";
}
