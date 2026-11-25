import type { Page } from '@playwright/test';

import { acceptConsent, expect, test } from '../fixtures/helpers';

/**
 * Supplier-portal help (`/portal/help`, decisions §508).
 *
 * A supplier cannot open the employee help centre at `/help`, so the portal
 * carries its own: supplier guides in the help centre's `Guide` shape,
 * rendered by the same section renderer, with every link kept inside the
 * portal. What this pins that `lib/help/content.test.ts` can't:
 *  - the portal nav reaches it, and a guide renders inside the portal shell;
 *  - a `{ui:key}` reference renders the live catalogue label;
 *  - a glossary link lands on the portal's own glossary, not on /help;
 *  - the guide's "Open …" button goes to the portal page the work is on.
 *
 * Signs in as the seeded portal user, like the other portal specs.
 */

const PORTAL_EMAIL = 'supplier@portal.test';
const PORTAL_PASSWORD = 'demo';

test.use({ storageState: { cookies: [], origins: [] } });

async function portalSignIn(page: Page) {
	await acceptConsent(page);
	await page.goto('/portal/login');
	await page.locator('input[type="email"]').fill(PORTAL_EMAIL);
	await page.locator('input[type="password"]').fill(PORTAL_PASSWORD);
	await page.locator('button[type="submit"]').click();
	await expect(page).toHaveURL(/\/portal\/?$/, { timeout: 15_000 });
}

test.describe('supplier portal help', () => {
	test('a supplier opens help from the portal nav and reads a guide', async ({ page }) => {
		await portalSignIn(page);

		const nav = page.getByRole('navigation', { name: 'Supplier portal' });
		await nav.getByRole('link', { name: 'Help' }).click();
		await expect(page).toHaveURL(/\/portal\/help$/);
		await expect(nav.getByRole('link', { name: 'Help' })).toHaveAttribute('aria-current', 'page');
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

		await page.getByRole('link', { name: 'Change your bank details' }).click();
		await expect(page).toHaveURL(/\/portal\/help\/portal-bank-details$/);
		const guide = page.getByTestId('portal-help-guide');
		await expect(guide.getByRole('heading', { level: 1, name: 'Change your bank details' })).toBeVisible();
		// `{ui:portal.company.bank.request}` renders the button's own label.
		await expect(guide.locator('strong.ui-label', { hasText: 'Request bank-detail change' })).toBeVisible();

		// A glossary link stays in the portal: a supplier cannot open /help.
		const term = guide.locator('.body a[href="/portal/help#bank-change-request"]').first();
		await expect(term).toBeVisible();
		await term.click();
		await expect(page).toHaveURL(/\/portal\/help#bank-change-request$/);
		await expect(page.locator('#bank-change-request')).toBeVisible();

		await page.goBack();
		await page.getByTestId('portal-help-open-page').click();
		await expect(page).toHaveURL(/\/portal\/company$/);
	});

	test('an unknown guide id says so, with a way back', async ({ page }) => {
		await portalSignIn(page);
		await page.goto('/portal/help/no-such-guide');
		await expect(page.getByRole('heading', { level: 1, name: 'Guide not found' })).toBeVisible();
		await page.getByRole('link', { name: 'Back to the help center' }).click();
		await expect(page).toHaveURL(/\/portal\/help$/);
	});
});
