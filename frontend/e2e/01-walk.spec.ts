/** Обход: каждая страница по нетронутому миру на десктопе и телефоне (#54, решение 6).
 *  Сторожа (консоль, 4xx/5xx, прокрутка) — в фикстуре; здесь — 200 и контент мира. */

import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { FIRM, POOL, SANDBOX_NUMBER, lead } from "./world";

const SCREENS = {
  desktop: { width: 1280, height: 800 },
  phone: { width: 390, height: 844 },
};

type Stop = { path: string; sees(page: Page): Promise<void> };

const named = (page: Page, text: string) => page.getByText(text, { exact: false }).first();

const PAGES: Stop[] = [
  {
    path: "/",
    sees: async (page) => {
      // По задаче каждого вида: эскалация, застрявшая отправка, бэкап, черновик.
      for (const [who, what] of [
        [FIRM.interested, "ждёт вас"],
        [FIRM.stuck, "проверить в телефоне"],
        ["Бэкап", "сделать бэкап"],
        [FIRM.draft, "проверить черновик"],
      ]) {
        await expect(page.getByRole("link").filter({ hasText: who }).filter({ hasText: what })).toBeVisible();
      }
    },
  },
  {
    path: "/leads",
    sees: async (page) => {
      await expect(page.getByText("лидов 15")).toBeVisible(); // world.LEADS
      await expect(page.getByRole("link", { name: FIRM.answer, exact: true })).toBeVisible();
    },
  },
  {
    path: "/leads/:id",
    sees: async (page) => {
      await expect(page.getByRole("heading", { name: FIRM.answer })).toBeVisible();
      await expect(page.getByRole("heading", { name: "Досье" })).toBeVisible();
    },
  },
  { path: "/cold", sees: (page) => expect(page.getByRole("button").filter({ hasText: FIRM.draft })).toBeVisible() },
  {
    path: "/threads",
    sees: (page) => expect(page.getByRole("button").filter({ hasText: FIRM.answer })).toBeVisible(),
  },
  {
    path: "/analytics",
    sees: async (page) => {
      await expect(page.getByRole("heading", { name: "Воронка" })).toBeVisible();
      await expect(named(page, "Бухгалтерские услуги · Алматы")).toBeVisible();
      // Воронка — внутри отступов своей карточки: на телефоне её шапка упиралась в рамку (#73).
      // Страницу это не расширяло, поэтому сторож ширины молчал.
      const card = page.locator("section").filter({ has: page.getByRole("heading", { name: "Воронка" }) });
      const [outer, inner] = [await card.boundingBox(), await card.locator("table").boundingBox()];
      const padding = await card.evaluate((element) => parseFloat(getComputedStyle(element).paddingRight));
      expect(inner!.x + inner!.width).toBeLessThanOrEqual(outer!.x + outer!.width - padding + 0.5);
    },
  },
  {
    path: "/sender",
    sees: async (page) => {
      const pool = page.locator("section").filter({ has: page.getByRole("heading", { name: "Пул номеров" }) });
      for (const number of [SANDBOX_NUMBER, ...POOL]) {
        await expect(pool.getByRole("cell", { name: number })).toBeVisible();
      }
    },
  },
  {
    path: "/activity",
    sees: (page) =>
      expect(page.locator(".daemon-name")).toContainText([
        "Монитор здоровья",
        "Тик воркера",
        "Прогрев номеров",
        "Вебхук",
      ], { useInnerText: true }),
  },
  {
    path: "/settings",
    sees: async (page) => {
      for (const section of ["Сегменты", "Исследование", "Переписка", "Отправка"]) {
        await expect(page.getByRole("heading", { name: section, level: 2 })).toBeVisible();
      }
    },
  },
  {
    path: "/sandbox",
    sees: async (page) => {
      await expect(page.getByLabel("Прошлые прогоны").locator("option:checked")).toContainText(FIRM.answer);
      await expect(named(page, "Расскажите подробнее, что вы предлагаете?")).toBeVisible();
    },
  },
];

for (const [screen, viewport] of Object.entries(SCREENS)) {
  test.describe(screen, () => {
    test.use({ viewport });
    for (const stop of PAGES) {
      test(stop.path, async ({ page, request }) => {
        const path = stop.path.replace(":id", encodeURIComponent((await lead(request, FIRM.answer)).company_id));
        const response = await page.goto(path);
        expect(response?.status()).toBe(200);
        await stop.sees(page);
        // Опросы страниц успели ответить: сторожа фикстуры видят всю загрузку.
        await page.waitForLoadState("networkidle");
      });
    }
  });
}
