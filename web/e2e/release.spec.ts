import { test, expect } from '@playwright/test';

/**
 * Release browser acceptance for the v0.1.0 local trusted single-user source
 * release. The suite exercises the real production Docker Compose topology
 * (nginx -> API -> SQLite) with two committed synthetic JSONL traces seeded
 * through the real containerized CLI by `scripts/verify-browser.sh`.
 *
 * Every assertion is an accessible role, label, or user-visible text node.
 * There are no CSS implementation selectors (no `main > ul > li`, no
 * `[aria-labelledby=...] code`), no arbitrary sleeps, and no test-only API
 * mocks. The deterministic rules classifier is triggered through the UI, not a
 * back door.
 */

const FIRST_RUN = 'minimal.jsonl';
const SECOND_RUN = 'minimal-variant.jsonl';

const ALERT_CODES = [
  'THX001_NO_PROGRESS_LOOP',
  'THX002_PLAN_LOOP',
  'THX003_VERIFICATION_GAP',
  'THX004_PREMATURE_SUCCESS',
  'THX005_RECOVERY_STORM',
  'THX006_TOOL_ERROR_CASCADE',
] as const;

test.describe('Release browser acceptance through nginx', () => {
  test.describe.configure({ mode: 'serial' });

  test('loads through nginx and shows the seeded run list', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'TraceHelix runs' })).toBeVisible();
    await expect(page.getByRole('link', { name: FIRST_RUN })).toBeVisible();
    await expect(page.getByRole('link', { name: SECOND_RUN })).toBeVisible();
    // Exactly the two seeded runs are listed (no duplicates, no extras): the
    // runs list is the only list on this route, so its list items are the
    // user-perceivable run entries.
    await expect(page.getByRole('listitem')).toHaveCount(2);
  });

  test('navigates to a seeded run and shows event detail and content hashes', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('link', { name: FIRST_RUN }).click();
    await expect(page.getByRole('heading', { name: FIRST_RUN })).toBeVisible();
    await expect(page.getByText('Showing 12 of 12 events')).toBeVisible();
    await expect(page.getByText('Input hash:')).toBeVisible();

    const eventButton = page.getByRole('button', { name: /^Sequence 0:/ }).first();
    await expect(eventButton).toBeVisible();
    await eventButton.click();
    await expect(page.getByText('Content hash:')).toBeVisible();
    await expect(page.getByText('search repository').first()).toBeVisible();
  });

  test('triggers deterministic rules analysis and shows all six alert codes with evidence', async ({ page }) => {
    await page.goto('/');
    await page.getByRole('link', { name: FIRST_RUN }).click();
    await expect(page.getByRole('heading', { name: 'Analysis' })).toBeVisible();

    const trigger = page.getByRole('button', { name: /^Run rules analysis$|^Run analysis again$/ });
    await expect(trigger).toBeVisible();
    await trigger.click();

    await expect(page.getByRole('heading', { name: 'Alerts' })).toBeVisible();
    // The alerts section is a named landmark (section[aria-labelledby]); scope
    // visible-evidence assertions to it without reaching for CSS selectors.
    const alertsRegion = page.getByRole('region', { name: 'Alerts' });
    await expect(alertsRegion).toBeVisible();
    for (const code of ALERT_CODES) {
      await expect(page.getByRole('heading', { name: code, level: 4 })).toBeVisible();
    }
    // Visible evidence rendered to users: severity label, sequence range, and
    // the evidence-event label. THX006_TOOL_ERROR_CASCADE is Critical severity.
    await expect(alertsRegion.getByText('Critical severity').first()).toBeVisible();
    await expect(alertsRegion.getByText(/^Sequences \d+/).first()).toBeVisible();
    await expect(alertsRegion.getByText('Evidence:').first()).toBeVisible();
  });

  test('selects a second run and compares with accessible output', async ({ page }) => {
    await page.goto('/');
    await page.getByLabel('Left run').selectOption({ label: FIRST_RUN });
    await page.getByLabel('Right run').selectOption({ label: SECOND_RUN });
    await page.getByRole('button', { name: 'Compare selected runs' }).click();

    await expect(page.getByRole('heading', { name: 'Compare runs' })).toBeVisible();
    await expect(
      page.getByText('Independent summaries only; observed differences are not causal proof.'),
    ).toBeVisible();
    await expect(page.getByText('12 events (denominator)')).toHaveCount(2);
  });
});
