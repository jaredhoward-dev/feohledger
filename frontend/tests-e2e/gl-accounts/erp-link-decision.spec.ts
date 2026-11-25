import type { Page } from '@playwright/test';

import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	signInAndWait,
	tenantBase,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * `/gl-accounts` — "Needs a decision" (docs/decisions.md §284).
 *
 * Found live against the QuickBooks sandbox: an old row "Depreciation" (keyed
 * by the bare name before §284) could mean the top-level account (40) or
 * "Truck:Depreciation" (39). The sync rightly refuses to guess, but it used to
 * refuse on every run with no way out in the product — the conflict only ever
 * reached a toast. The row now carries its candidates
 * (`gl_accounts.erp_link_candidates`), the page shows it as needing a
 * decision, and an admin picks one (`POST /api/gl-accounts/{id}/erp-link`).
 *
 * The candidates are written with SQL, exactly as the sync leaves them, so the
 * spec needs no live ERP; the sync rules that produce them are covered by
 * `backend/tests/test_erp_link_realm.py`. The `mock` ERP has no company to
 * name, so the candidates' realm is null, which is what the endpoint checks
 * them against.
 */

const PREFIX = 'GLDEC-';

async function headers(page: Page) {
	return { ...(await authedTenantHeaders(page)), 'Content-Type': 'application/json' };
}

async function setErp(page: Page, erp: Record<string, unknown> | null) {
	const r = await page.request.patch(`${API_BASE}/api/organization`, {
		headers: await headers(page),
		data: { settings: { erp } }
	});
	expect(r.ok(), await r.text()).toBe(true);
}

test.describe('/gl-accounts — a row waiting for a decision', () => {
	test.beforeEach(async ({ page }) => {
		await setErp(page, { type: 'mock', integration_method: 'direct' });
	});

	test.afterEach(() => {
		tenantPsql(`DELETE FROM gl_accounts WHERE code LIKE '${PREFIX}%'`);
	});

	// Leaving an `erp` config behind changes how downstream specs push
	// (see organization/gl-accounts-sync.spec.ts).
	test.afterAll(async ({ browser }) => {
		const context = await browser.newContext({ baseURL: tenantBase(currentTenantSlug()) });
		const page = await context.newPage();
		await signInAndWait(page);
		await setErp(page, null);
		await context.close();
	});

	test('shows the candidates and links the one the admin picks', async ({ page }) => {
		const code = `${PREFIX}${Date.now().toString(36)}`;
		const created = await page.request.post(`${API_BASE}/api/gl-accounts`, {
			headers: await headers(page),
			data: { code, name: 'Depreciation', account_type: 'expense' }
		});
		expect(created.ok(), await created.text()).toBe(true);
		const candidates = {
			realm: null,
			reason: 'ambiguous_legacy',
			accounts: [
				{ erp_account_id: '39', code: 'Truck:Depreciation', name: 'Truck:Depreciation' },
				{ erp_account_id: '40', code: 'Depreciation', name: 'Depreciation' }
			]
		};
		tenantPsql(
			`UPDATE gl_accounts SET erp_link_candidates = '${JSON.stringify(candidates)}'::jsonb ` +
				`WHERE code = '${code}'`
		);

		await page.goto(`/gl-accounts?search=${encodeURIComponent(code)}`);
		const row = page.locator('table tbody tr', {
			has: page.locator('td.mono', { hasText: new RegExp(`^${code}$`) })
		});
		await expect(row.getByText('Needs a decision')).toBeVisible();
		await expect(page.getByTestId('gl-erp-decisions')).toContainText(
			'1 account needs a decision'
		);

		await row.getByRole('button', { name: `Choose ERP account for ${code}` }).click();
		const modal = page.locator('div.modal[role="dialog"]', {
			hasText: `Which ERP account is ${code}?`
		});
		await expect(modal).toBeVisible();
		const options = modal.getByTestId('gl-erp-candidate');
		await expect(options).toHaveCount(2);
		await expect(options.nth(0)).toContainText('Truck:Depreciation');
		await expect(options.nth(0)).toContainText('ERP id 39');
		// Nothing is preselected: confirming is always a choice.
		const confirm = modal.getByRole('button', { name: 'Link to this account' });
		await expect(confirm).toBeDisabled();

		await options.nth(1).getByRole('radio').check();
		const linked = page.waitForResponse(
			(r) => r.request().method() === 'POST' && r.url().includes('/erp-link')
		);
		await confirm.click();
		const resp = await linked;
		expect(resp.status()).toBe(200);
		expect(resp.request().postDataJSON()).toEqual({ erp_account_id: '40' });

		await expect(modal).toBeHidden();
		await expect(row.getByText('Needs a decision')).toHaveCount(0);
		await expect(row.locator('td', { hasText: /^40$/ })).toBeVisible();
		expect(
			tenantPsql(
				`SELECT erp_account_id, erp_link_confirmed_at IS NOT NULL, erp_link_candidates IS NULL ` +
					`FROM gl_accounts WHERE code = '${code}'`
			).trim()
		).toBe('40|t|t');
	});

	// §524: a QuickBooks row the sync kept LINKED but questions — its code is
	// its account's current code and also a legacy key another account shared.
	// Bills still post to the linked id, so the table shows that id beside the
	// badge, and confirming the same account keeps the link and clears it.
	test('a linked row the sync questions keeps its id and confirms in place', async ({
		page
	}) => {
		const code = `${PREFIX}L${Date.now().toString(36)}`;
		const created = await page.request.post(`${API_BASE}/api/gl-accounts`, {
			headers: await headers(page),
			data: { code, name: 'Depreciation', account_type: 'expense' }
		});
		expect(created.ok(), await created.text()).toBe(true);
		const candidates = {
			realm: null,
			reason: 'ambiguous_legacy',
			accounts: [
				{ erp_account_id: '9139', code: 'Truck:Depreciation', name: 'Truck:Depreciation' },
				{ erp_account_id: '9140', code: 'Depreciation', name: 'Depreciation' }
			]
		};
		tenantPsql(
			`UPDATE gl_accounts SET erp_account_id = '9140', ` +
				`erp_link_candidates = '${JSON.stringify(candidates)}'::jsonb ` +
				`WHERE code = '${code}'`
		);

		await page.goto(`/gl-accounts?search=${encodeURIComponent(code)}`);
		const row = page.locator('table tbody tr', {
			has: page.locator('td.mono', { hasText: new RegExp(`^${code}$`) })
		});
		await expect(row.getByText('Needs a decision')).toBeVisible();
		await expect(row.locator('.erp-id')).toHaveText('9140');
		await expect(page.getByTestId('gl-erp-decisions')).toContainText(
			'1 account needs a decision'
		);

		await row.getByRole('button', { name: `Choose ERP account for ${code}` }).click();
		const modal = page.locator('div.modal[role="dialog"]', {
			hasText: `Which ERP account is ${code}?`
		});
		await expect(modal).toBeVisible();
		await expect(modal.locator('p.intro')).toContainText(
			'This row is linked to ERP account Depreciation'
		);
		const options = modal.getByTestId('gl-erp-candidate');
		await expect(options).toHaveCount(2);
		// The current link is tagged, on its own card only, and still not
		// preselected: confirming it is a choice too (§284).
		await expect(modal.getByTestId('gl-erp-current-link')).toHaveCount(1);
		await expect(options.nth(1).getByTestId('gl-erp-current-link')).toHaveText('Current link');
		await expect(options.nth(1).getByRole('radio')).toHaveAccessibleDescription('Current link');
		await expect(options.nth(1).getByRole('radio')).not.toBeChecked();
		const confirm = modal.getByRole('button', { name: 'Link to this account' });
		await expect(confirm).toBeDisabled();

		await options.nth(1).getByRole('radio').check();
		const linked = page.waitForResponse(
			(r) => r.request().method() === 'POST' && r.url().includes('/erp-link')
		);
		await confirm.click();
		const resp = await linked;
		expect(resp.status()).toBe(200);
		expect(resp.request().postDataJSON()).toEqual({ erp_account_id: '9140' });

		await expect(modal).toBeHidden();
		await expect(row.getByText('Needs a decision')).toHaveCount(0);
		await expect(row.locator('.erp-id')).toHaveText('9140');
		expect(
			tenantPsql(
				`SELECT erp_account_id, erp_link_confirmed_at IS NOT NULL, erp_link_candidates IS NULL ` +
					`FROM gl_accounts WHERE code = '${code}'`
			).trim()
		).toBe('9140|t|t');
	});
});
