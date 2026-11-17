/** Переписка в новом прогоне песочницы (#54, решение 7): «Черновики топ-N» → отправка
 *  через подменный Node → доставка → ответ продавца → стоп-слово. Файл последний:
 *  он делает активным свой прогон, тесты внутри идут по порядку и опираются друг на друга. */

import type { APIRequestContext } from "@playwright/test";
import { expect, test } from "./fixtures";
import { FIRM, lead, sandbox } from "./world";

test.describe.configure({ mode: "serial" });

type Thread = { status: string; draft: object | null };

const RUN_LEAD = FIRM.touches;
// Два других лида топ-3 нового прогона — для вердиктов продавца.
const VERDICTS = { [FIRM.interested]: "Давайте созвонимся завтра", [FIRM.answer]: "А сколько это стоит?" };

/** Шаг доставки, пока тред не придёт в нужное состояние: ждём состояние, а не исход тика.
 *  Шаг — как `sandbox/demo.py::_next_send`: в окно, за джиттер, тик; каждая отправка с
 *  номера заводит джиттер следующей. */
async function tickUntil(request: APIRequestContext, company_id: string, done: (thread: Thread) => boolean) {
  await expect
    .poll(async () => {
      await sandbox.clock(request, "window");
      await sandbox.clock(request, "jitter");
      await sandbox.tick(request);
      return done(await sandbox.thread(request, company_id));
    })
    .toBe(true);
}

test("прогон по лиду и «Черновики топ-N» на /cold", async ({ page, guard }) => {
  await page.goto("/sandbox");
  const option = page.getByLabel("Лид для прогона").locator("option", { hasText: RUN_LEAD });
  await page.getByLabel("Лид для прогона").selectOption((await option.getAttribute("value"))!);
  // У нового прогона треда ещё нет: чат отвечает 409 «сначала черновик».
  guard.allow("/api/sandbox/chat", 409);
  await page.getByRole("button", { name: "Начать прогон" }).click();
  await expect(page.getByText("Переписки ещё нет — напишите первое письмо кнопкой ниже.")).toBeVisible();
  await page.getByRole("button", { name: "к открытию окна" }).click();
  await expect(page.getByLabel("Прошлые прогоны").locator("option:checked")).toContainText(RUN_LEAD);

  await page.goto("/cold");
  const outreach = page.getByRole("region", { name: "Черновики топ-N" });
  await outreach.getByRole("button", { name: "Черновики топ-N" }).click();
  await expect(outreach).toContainText("Последний прогон готов");
  for (const name of [RUN_LEAD, ...Object.keys(VERDICTS)]) {
    await expect(page.getByRole("button").filter({ hasText: name })).toBeVisible();
  }
});

test("подтверждённый черновик уходит через подменный Node и доставляется", async ({ page, request }) => {
  const { company_id } = await lead(request, RUN_LEAD);
  await page.goto("/cold");
  await page.getByRole("button").filter({ hasText: RUN_LEAD }).click();
  await expect(page.getByRole("link", { name: `карточка ${RUN_LEAD}` })).toBeVisible();
  const queued = page.waitForResponse((r) => r.url().endsWith("/api/sender/queue") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Отправить", exact: true }).click();
  expect((await queued).status()).toBe(201);

  await page.goto("/sender");
  const row = page.getByRole("row").filter({ hasText: company_id });
  await expect(row).toContainText("оператор");
  await expect(row).toContainText("ждёт");

  await page.goto("/sandbox");
  await tickUntil(request, company_id, (thread) => thread.status === "active"); // первая доставка
  await page.reload();
  await expect(page.getByRole("listitem").filter({ hasText: "ушло лиду" })).toContainText("доставлено");
});

test("ответ лида — черновик продавца, стоп-слово закрывает тред отказом в базе прогона", async ({ page, request }) => {
  const { company_id, channel } = await lead(request, RUN_LEAD);
  await page.goto("/sandbox");
  const reply = page.getByRole("textbox", { name: /Ваш ответ в роли лида/ });
  await reply.fill("Расскажите подробнее, что вы предлагаете?");
  await page.getByRole("button", { name: "Отправить как лид" }).click();
  await expect(page.getByRole("listitem").filter({ hasText: "лид (это вы)" })).toBeVisible();
  // manual: ответ продавца остаётся черновиком оператору.
  await tickUntil(request, company_id, (thread) => Boolean(thread.draft));
  await page.reload();
  await expect(page.getByRole("listitem").filter({ hasText: "черновик — ещё не отправлено" })).toBeVisible();

  await reply.fill("Не пишите нам больше");
  await page.getByRole("button", { name: "Отправить как лид" }).click();
  await expect(page.getByText("Состояние: лид отказался")).toBeVisible();

  // Отказ — в базе прогона (её читает /api/refusals песочницы), а в боевой лид остаётся лидом.
  const refusals: { handle: string }[] = await (await request.get("/api/refusals")).json();
  expect(refusals.map((each) => each.handle)).toContain(channel!.handle);
  await page.goto("/leads");
  await expect(page.getByRole("link", { name: RUN_LEAD })).toBeVisible();
});

test("вердикты продавца: «созвонимся» и «сколько» отдают тред человеку", async ({ page, request }) => {
  const leads = await Promise.all(Object.keys(VERDICTS).map((name) => lead(request, name)));
  for (const { company_id } of leads) await sandbox.queue(request, company_id);
  for (const { company_id } of leads) {
    await tickUntil(request, company_id, (thread) => thread.status === "active");
  }
  for (const { company_id, name } of leads) {
    await sandbox.incoming(request, company_id, VERDICTS[name]);
    await tickUntil(request, company_id, (thread) => thread.status === "escalated");
  }

  await page.goto("/");
  await expect(page.getByRole("link").filter({ hasText: FIRM.interested })).toContainText("«созвонимся»");
  await expect(page.getByRole("link").filter({ hasText: FIRM.answer })).toContainText("содержит цену");
  await page.goto("/threads");
  for (const { name } of leads) {
    await expect(page.getByRole("button").filter({ hasText: name })).toContainText("ждёт человека");
  }
});
