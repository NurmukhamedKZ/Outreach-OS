/** Стенд на время прогона (#54): свой и свежий — стёртые данные и демо-мир заново.
 *  Поднимает и гасит `scripts/stand` (#51), адреса — из `.stand/stand.json`. */

import { spawnSync } from "node:child_process";
import { readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { chromium } from "@playwright/test";

const ROOT = path.resolve(__dirname, "..", "..");
const STAND = path.join(ROOT, "scripts", "stand");

function stand(command: string) {
  return spawnSync(STAND, [command], { cwd: ROOT, stdio: ["ignore", "inherit", "inherit"] }).status;
}

/** Адрес дашборда поднятого стенда этого дерева. */
export function dashboard(): string {
  const state = JSON.parse(readFileSync(path.join(ROOT, ".stand", "stand.json"), "utf8"));
  return `http://127.0.0.1:${state.frontend.port}`;
}

export default async function setup() {
  // Браузер ставится отдельно от npm ci; без него упал бы каждый тест, и уже после подъёма стенда.
  await chromium
    .launch()
    .then((browser) => browser.close())
    .catch(() => {
      throw new Error("нет браузера Playwright: cd frontend && npx playwright install chromium");
    });
  // Поднятый стенд — чья-то ручная сессия: гасить и стирать её молча нельзя.
  if (spawnSync(STAND, ["status"], { cwd: ROOT, stdio: "ignore" }).status === 0) {
    throw new Error("стенд этого дерева уже поднят — e2e поднимает свой: scripts/stand down");
  }
  // Полуживой стенд (упал один процесс) гасится до того, как стираются его данные.
  stand("down");
  // Тот же мир на каждом прогоне: на собранных данных стенд мир не пересобирает.
  rmSync(path.join(ROOT, ".stand", "data"), { recursive: true, force: true });
  if (stand("up") !== 0) {
    stand("down");
    throw new Error("стенд не поднялся — причина выше; логи в .stand/");
  }
  return () => {
    if (stand("down") !== 0) console.error("стенд не погас: scripts/stand down");
  };
}
