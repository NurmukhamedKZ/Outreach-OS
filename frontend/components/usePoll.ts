"use client";

import { useEffect } from "react";

/** Зовёт `load` сразу и раз в `ms`, пока вкладка видна; вернулась — сразу.
 *  Живых стримов у дашборда нет: страница сама спрашивает API. */
export function usePoll(load: () => void, ms: number) {
  useEffect(() => {
    load();
    const tick = () => {
      if (document.visibilityState === "visible") load();
    };
    const timer = setInterval(tick, ms);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [load, ms]);
}
