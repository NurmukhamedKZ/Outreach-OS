/** Общая фикстура e2e: адрес стенда и сторожа на каждом тесте (#54, решение 5).
 *
 *  Тест падает, если за время теста страница написала ошибку в консоль, бросила
 *  исключение, получила ответ 4xx/5xx или в конце шире экрана. Ожидаемый отказ
 *  тест называет сам: `guard.allow("/api/sandbox/chat", 409)` — молчаливого
 *  списка исключений нет. */

import { test as base, expect } from "@playwright/test";
import { dashboard } from "./stand";

export type Guard = { allow(path: string, status: number): void };

export const test = base.extend<{ guard: Guard }>({
  baseURL: async ({}, use) => use(dashboard()),
  guard: [
    async ({ page }, use) => {
      const problems: string[] = [];
      const allowed: { path: string; status: number }[] = [];
      page.on("console", (message) => {
        // Неудачный запрос Chrome дублирует в консоль; его судит сторож ответов ниже.
        if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) {
          problems.push(`консоль: ${message.text()}`);
        }
      });
      page.on("pageerror", (error) => problems.push(`исключение: ${error.message}`));
      page.on("response", (response) => {
        const status = response.status();
        const path = new URL(response.url()).pathname;
        if (status >= 400 && !allowed.some((each) => each.status === status && path === each.path)) {
          problems.push(`${status} ${response.request().method()} ${path}`);
        }
      });
      await use({ allow: (path, status) => allowed.push({ path, status }) });
      if (!page.isClosed() && page.url().startsWith("http")) {
        const [scroll, width] = await page.evaluate(() => [
          document.documentElement.scrollWidth,
          window.innerWidth,
        ]);
        if (scroll > width) problems.push(`страница шире экрана: ${scroll} > ${width}`);
      }
      expect(problems, "сторожа страницы").toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };
