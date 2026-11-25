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
 *  - the guide's "Open …" button goes to the portal page the work is on;
 *  - the search field narrows the page, announces its count, keeps `?q=`
 *    across a reload and shows an empty state (decisions §517).
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

	test('the purchase-order guide names the row button and opens the orders page', async ({ page }) => {
		await portalSignIn(page);
		await page.goto('/portal/help');

		await page.getByRole('link', { name: 'Create an invoice from a purchase order' }).click();
		await expect(page).toHaveURL(/\/portal\/help\/portal-po-invoice$/);
		const guide = page.getByTestId('portal-help-guide');
		await expect(
			guide.getByRole('heading', { level: 1, name: 'Create an invoice from a purchase order' })
		).toBeVisible();
		// `{ui:portal.po.createInvoice}` renders the row button's own label.
		await expect(guide.locator('strong.ui-label', { hasText: 'Create invoice' }).first()).toBeVisible();

		await page.getByTestId('portal-help-open-page').click();
		await expect(page).toHaveURL(/\/portal\/purchase-orders$/);
	});

	test('an unknown guide id says so, with a way back', async ({ page }) => {
		await portalSignIn(page);
		await page.goto('/portal/help/no-such-guide');
		await expect(page.getByRole('heading', { level: 1, name: 'Guide not found' })).toBeVisible();
		await page.getByRole('link', { name: 'Back to the help center' }).click();
		await expect(page).toHaveURL(/\/portal\/help$/);
	});

	test('the search field narrows the guides and terms, announces the count and keeps ?q=', async ({ page }) => {
		await portalSignIn(page);
		await page.goto('/portal/help');
		const help = page.getByTestId('portal-help');
		const status = page.getByTestId('portal-help-search-status');
		await expect(status).toHaveAttribute('role', 'status');
		await expect(status).toHaveText('');

		await page.getByRole('textbox', { name: 'Search the supplier help' }).fill('bank details');
		await expect(status).toContainText(/\d+ results? for “bank details”/);
		// Best match first, and a guide that doesn't match is gone.
		await expect(help.locator('.guides a').first()).toHaveText('Change your bank details');
		await expect(help.getByRole('link', { name: 'Create an invoice from a purchase order' })).toHaveCount(0);
		await expect(page).toHaveURL(/\/portal\/help\?q=bank\+details$/);

		// A reload keeps the search.
		await page.reload();
		await expect(page.getByTestId('portal-help-search')).toHaveValue('bank details');
		await expect(status).toContainText('for “bank details”');

		// No match: the empty state says so, and Clear brings everything back.
		await page.getByTestId('portal-help-search').fill('zzqqxx');
		await expect(status).toHaveText('No guides or terms match “zzqqxx”. Try a shorter word, or clear the search to see them all.');
		await expect(help.locator('.guides')).toHaveCount(0);
		await page.getByTestId('portal-help-search-clear').click();
		await expect(status).toHaveText('');
		await expect(help.getByRole('link', { name: 'Create an invoice from a purchase order' })).toBeVisible();
		await expect(page).toHaveURL(/\/portal\/help$/);
	});
});
