"use client";

/** Подсветка контрола по ссылке из гайда: `?hl=<data-hl>` находит настоящую
 * кнопку на странице, прокручивает к ней и мигает рамкой.
 *
 * Опрос вместо одного querySelector: половина контролов появляется после
 * ответа бэкенда, и разовый поиск на mount промахивался бы мимо них.
 */

import { useEffect } from "react";
import { usePathname, useSearchParams } from "next/navigation";

const DEADLINE_MS = 3000;
const POLL_MS = 150;
const FLASH_MS = 2600;

export default function Highlighter() {
  const target = useSearchParams().get("hl");
  const pathname = usePathname();

  useEffect(() => {
    if (!target) return;
    const started = Date.now();
    let flashed: Element | null = null;

    const timer = setInterval(() => {
      const node = document.querySelector(`[data-hl="${CSS.escape(target)}"]`);
      if (node) {
        clearInterval(timer);
        flashed = node;
        node.scrollIntoView({ behavior: "smooth", block: "center" });
        node.classList.add("is-hl");
        setTimeout(() => node.classList.remove("is-hl"), FLASH_MS);
      } else if (Date.now() - started > DEADLINE_MS) {
        clearInterval(timer);
      }
    }, POLL_MS);

    return () => {
      clearInterval(timer);
      flashed?.classList.remove("is-hl");
    };
  }, [target, pathname]);

  return null;
}
