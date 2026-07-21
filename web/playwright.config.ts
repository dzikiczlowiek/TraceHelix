import { defineConfig, devices } from '@playwright/test';

/**
 * TraceHelix browser-acceptance configuration.
 *
 * This configuration deliberately has no implicit dev server. The real
 * production Docker Compose topology (nginx -> API -> SQLite) is brought up and
 * seeded by `scripts/verify-browser.sh`, which supplies the loopback base URL
 * through the `PLAYWRIGHT_BASE_URL` environment variable. The suite never
 * retries, locally or in CI, so flakes are surfaced instead of hidden.
 */
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? 'http://127.0.0.1:8080';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  reporter: [['list']],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL,
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
