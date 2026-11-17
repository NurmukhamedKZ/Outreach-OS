/** Демо-мир стенда глазами e2e: имена компаний по роли и наши номера.
 *  Источник — `backend/sandbox/world.py` (`FIRMS`, `POOL`) и `sandbox/runs.py`
 *  (`SANDBOX_NUMBER`); поменялся мир — меняется этот файл. */

import type { APIRequestContext, APIResponse } from "@playwright/test";
import { expect } from "@playwright/test";

export const FIRM = {
  answer: "Бухгалтерия Ласточка", // лид прогона мира, живой диалог
  price: "Учёт и Право",
  interested: "Кадры Степи",
  stuck: "Счёт и Сверка",
  held: "Талант Хаб", // строка автомата, которую держит manual
  touches: "Главбух на Час", // лучший по оценке лид
  draft: "Аутсорс Бюро Тенгри", // непроверенный черновик «Черновиков топ-N»
} as const;

export const SANDBOX_NUMBER = "+77000000001";
export const POOL = ["+77990009002", "+77990009003"] as const;

type Lead = { company_id: string; name: string; channel: { handle: string } | null };

/** Лид research по имени: id и канал страницы показывают не везде. */
export async function lead(request: APIRequestContext, name: string): Promise<Lead> {
  const leads: Lead[] = await (await request.get("/api/research/leads")).json();
  const found = leads.find((each) => each.name === name);
  expect(found, `лид «${name}» в выдаче`).toBeDefined();
  return found!;
}

async function ok(response: Promise<APIResponse>) {
  const answer = await response;
  expect(answer.ok(), `${answer.url()} — ${answer.status()} ${await answer.text()}`).toBe(true);
  return answer.json();
}

/** Пульт песочницы — то, чего на страницах нет. */
export const sandbox = {
  tick: (request: APIRequestContext) =>
    ok(request.post("/api/sandbox/tick", { data: { daemon: "tick" } })),
  clock: (request: APIRequestContext, preset: "jitter" | "window") =>
    ok(request.post("/api/sandbox/clock", { data: { preset } })),
  incoming: (request: APIRequestContext, company_id: string, text: string) =>
    ok(request.post("/api/sandbox/incoming", { data: { company_id, text } })),
  queue: (request: APIRequestContext, thread_id: string) =>
    ok(request.post("/api/sender/queue", { data: { thread_id } })),
  thread: (request: APIRequestContext, company_id: string) =>
    ok(request.get(`/api/threads/${encodeURIComponent(company_id)}`)),
};
