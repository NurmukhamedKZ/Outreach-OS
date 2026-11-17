import { defineConfig, devices } from "@playwright/test";

// Набор идёт по своему стенду (e2e/stand.ts) одним воркером и по порядку файлов:
// пульт песочницы (часы, активный прогон) — состояние процесса бэкенда, общее на все
// тесты, а сценарий, сменивший прогон, — последний файл (#54, решения 4 и 7).
export default defineConfig({
  testDir: "e2e",
  globalSetup: "./e2e/stand.ts",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  forbidOnly: true,
  reporter: "list",
  // Первый заход на страницу next dev компилирует её — это секунды, а не миллисекунды.
  timeout: 60_000,
  expect: { timeout: 15_000 },
  outputDir: "e2e/results",
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1280, height: 800 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
