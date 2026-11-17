/** Сценарии по прогону мира: очередь и режим на /sender, «Настройки», исследование
 *  (#54, решение 7). Прогон не меняют — переписка в новом прогоне идёт следующим файлом. */

import { expect, test } from "./fixtures";
import { FIRM, lead, sandbox } from "./world";

test("/sender: «Отправить» у строки автомата под manual делает её строкой оператора", async ({ page, request }) => {
  const held = await lead(request, FIRM.held);
  await page.goto("/sender");
  const rows = page.getByRole("row").filter({ hasText: held.company_id });
  const author = (name: string) => rows.filter({ has: page.getByRole("cell", { name, exact: true }) });

  // Кнопки есть только у ждущих строк: «Ждут отправки».
  await author("автомат").getByRole("button", { name: "Отправить" }).click();
  // Строку оператора manual отпускает: её мог уже взять фоновый тик — тогда она в «Последних».
  await expect(author("оператор")).toHaveCount(1);
  await expect(author("автомат")).toHaveCount(0);
});

test("/sender: «Стоп» держит очередь и переживает перезагрузку, «Отменить» у ждущей", async ({ page, request }) => {
  const draft = await lead(request, FIRM.draft);
  await page.goto("/sender");
  const mode = (name: string) => page.getByRole("button", { name, exact: true });
  await expect(mode("Ручной")).toHaveAttribute("aria-pressed", "true");
  // Под «Ручным» строку оператора фоновый тик отправил бы за 20 секунд, если окно открыто.
  await mode("Стоп").click();
  await expect(mode("Стоп")).toHaveAttribute("aria-pressed", "true");

  await sandbox.queue(request, draft.company_id); // черновик «Черновиков топ-N»
  await page.reload();
  await expect(mode("Стоп")).toHaveAttribute("aria-pressed", "true");
  const row = page.getByRole("row").filter({ hasText: draft.company_id });
  await row.filter({ hasText: "ждёт" }).getByRole("button", { name: "Отменить" }).click();
  await expect(row.filter({ hasText: "отменено" })).toHaveCount(1);
  await expect(row.filter({ hasText: "ждёт" })).toHaveCount(0);

  // Мир остаётся в manual, как его собрал демо-сценарий.
  await mode("Ручной").click();
  await expect(mode("Ручной")).toHaveAttribute("aria-pressed", "true"); // сохранено: кнопка — ответ API
  await page.reload();
  await expect(mode("Ручной")).toHaveAttribute("aria-pressed", "true");
});

test("«Настройки»: размер партии сохраняется и переживает перезагрузку", async ({ page }) => {
  await page.goto("/settings");
  const research = page.locator("section").filter({ has: page.getByRole("heading", { name: "Исследование", level: 2 }) });
  const size = research.getByRole("spinbutton", { name: /Размер партии/ });
  await expect(size).toHaveValue("50");

  await size.fill("37");
  const saved = page.waitForResponse((r) => r.url().endsWith("/api/research/settings") && r.request().method() === "PUT");
  await research.getByRole("button", { name: "Сохранить" }).click();
  expect((await saved).ok()).toBe(true);

  await page.reload();
  await expect(size).toHaveValue("37");
});

test("исследование: «Исследовать заново» на карточке — итог партии на /leads", async ({ page }) => {
  await page.goto("/leads");
  await page.getByRole("link", { name: FIRM.answer }).click();
  const again = page.getByRole("button", { name: "Исследовать заново" });
  const started = page.waitForResponse((r) => r.url().includes("/api/research/companies/") && r.request().method() === "POST");
  await again.click();
  expect((await started).status()).toBe(202);
  // Офлайн партия из одной компании кончается за секунды: кнопка возвращается.
  await expect(again).toBeEnabled();

  await page.getByRole("link", { name: "← к лидам" }).click();
  const batch = page.getByRole("region", { name: "Партия исследования" });
  await expect(batch).toContainText("Последняя партия готова");
  await expect(batch).toContainText(/компаний\s*1\b/);
  await expect(batch).toContainText(/готово\s*1\b/);
  await expect(page.getByRole("link", { name: FIRM.answer })).toBeVisible();
});
