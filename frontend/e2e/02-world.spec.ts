/** Сценарии по прогону мира: очередь и режим на /sender, «Настройки», исследование
 *  (#54, решение 7). Прогон не меняют — переписка в новом прогоне идёт следующим файлом. */

import { expect, test } from "./fixtures";
import { FIRM, lead, sandbox } from "./world";

test("/sender: «Отправить» у строки автомата под manual, «Отменить» у ждущей", async ({ page, request }) => {
  const held = await lead(request, FIRM.held);
  const draft = await lead(request, FIRM.draft);
  // Вторая ждущая строка — черновик «Черновиков топ-N»; джиттер держит её от фонового тика.
  await sandbox.queue(request, draft.company_id);
  await page.goto("/sender");
  const rows = page.getByRole("row");

  // Кнопки есть только у ждущих строк: «Ждут отправки».
  const automat = rows.filter({ hasText: held.company_id }).filter({ hasText: "автомат" });
  await automat.getByRole("button", { name: "Отправить" }).click();
  // Автор стал оператором; строку мог уже взять фоновый тик — тогда она в «Последних».
  await expect(rows.filter({ hasText: held.company_id }).filter({ hasText: "оператор" }).first()).toBeVisible();

  await rows.filter({ hasText: draft.company_id }).getByRole("button", { name: "Отменить" }).click();
  await expect(rows.filter({ hasText: draft.company_id }).filter({ hasText: "отменено" }).first()).toBeVisible();
});

test("/sender: режим автопилота переживает перезагрузку", async ({ page }) => {
  await page.goto("/sender");
  const mode = (name: string) => page.getByRole("button", { name, exact: true });
  await expect(mode("Ручной")).toHaveAttribute("aria-pressed", "true");

  await mode("Только ответы").click();
  await expect(mode("Только ответы")).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  await expect(mode("Только ответы")).toHaveAttribute("aria-pressed", "true");

  // Мир остаётся в manual: автомат не должен отпускать строки следующим сценариям.
  await mode("Ручной").click();
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
