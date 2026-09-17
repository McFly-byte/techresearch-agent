import { test, expect } from '@playwright/test';

test('fake end-to-end: create task -> progress -> report', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /new research task/i })).toBeVisible();

  // Fill the query and submit.
  await page.getByPlaceholder(/what do you want/i).fill('compare langgraph and llamaindex');
  await page.getByRole('button', { name: /run/i }).click();

  // Wait for progress page.
  await expect(page).toHaveURL(/\/progress\//);
  await expect(page.locator('[data-testid="progress-bar"]')).toBeVisible();

  // Eventually we auto-navigate to the report.
  await expect(page).toHaveURL(/\/report\//, { timeout: 30000 });

  // Report must contain content and a clickable citation anchor.
  await expect(page.locator('.report-body')).toBeVisible({ timeout: 5000 });
  const anchor = page.locator('a[href^="#c"]').first();
  await expect(anchor).toBeAttached();

  await page.screenshot({ path: 'tests/e2e/screenshots/report.png', fullPage: true });
});
