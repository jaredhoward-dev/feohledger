import type { Page } from '@playwright/test';
import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	selectVendorInPicker,
	signInAndWait,
	tenantBase,
	test,
	vendorPicker
} from '../fixtures/helpers';

/**
 * /organization → PO match rules (decisions §512).
 *
 * The panel reads and writes `settings.matching` through its own audited
 * router (`backend/app/api/organization_match_rules.py`); validation, the
 * audit rows and the generic PATCH's refusal are proven in
 * `backend/tests/test_organization_match_rules.py`. What this spec holds is
 * the page's side: a vendor rule can be added, shows in the list, edited and
 * removed (the removal takes a second click), a refused value is stated, and
 * a non-admin gets the notice and no request at all.
 *
 * Every test starts from, and the suite leaves, no vendor rules for the
 * vendor it uses — the e2e tenant is shared by every spec on a worker.
 */

const RULES = '/api/organization/match-rules';

interface VendorRow {
	id: string;
	name: string;
}

async function firstVendor(page: Page): Promise<VendorRow> {
	const headers = await authedTenantHeaders(page);
	const res = await page.request.get(`${API_BASE}/api/vendors?page_size=1`, {
		headers
	});
	expect(res.ok()).toBe(true);
	const body = await res.json();
	const rows: VendorRow[] = Array.isArray(body) ? body : body.items;
	expect(rows.length).toBeGreaterThan(0);
	return rows[0];
}

async function clearVendorRule(page: Page, vendorId: string): Promise<void> {
	const headers = await authedTenantHeaders(page);
	const res = await page.request.delete(`${API_BASE}${RULES}/vendors/${vendorId}`, { headers });
	expect(res.ok()).toBe(true);
}

function savedResponse(page: Page, method: 'PUT' | 'DELETE') {
	return page.waitForResponse(
		(r) =>
			new URL(r.url()).pathname.startsWith(`${RULES}/vendors/`) && r.request().method() === method
	);
}

test.describe('/organization — PO match rules', () => {
	let vendor: VendorRow;

	test.beforeEach(async ({ page }) => {
		vendor = await firstVendor(page);
		await clearVendorRule(page, vendor.id);
	});

	test.afterAll(async ({ browser }) => {
		const ctx = await browser.newContext({
			baseURL: tenantBase(currentTenantSlug())
		});
		const page = await ctx.newPage();
		try {
			await signInAndWait(page);
			if (vendor) await clearVendorRule(page, vendor.id);
		} finally {
			await ctx.close();
		}
	});

	test('a vendor rule can be added, edited and removed', async ({ page }) => {
		await page.goto('/organization?section=match-rules');
		const panel = page.getByTestId('match-rules-panel');
		await expect(panel.getByTestId('match-rules-organization')).toBeVisible();

		// Add.
		await panel.getByTestId('match-rule-add').click();
		const form = panel.getByTestId('match-rule-form');
		await selectVendorInPicker(vendorPicker(form), vendor.name);
		await form.getByTestId('match-rule-tolerance').fill('1.5');
		const created = savedResponse(page, 'PUT');
		await form.getByTestId('match-rule-save').click();
		expect((await created).status()).toBe(200);

		const row = panel.getByTestId('match-rule-row').filter({ hasText: vendor.name });
		await expect(row).toHaveCount(1);
		await expect(row).toContainText('1.5%');

		// Edit — the reload proves the list is the server's.
		await page.reload();
		const reloaded = page
			.getByTestId('match-rules-panel')
			.getByTestId('match-rule-row')
			.filter({ hasText: vendor.name });
		await reloaded.getByRole('button', { name: `Edit the rule for ${vendor.name}` }).click();
		const editForm = page.getByTestId('match-rule-form');
		await editForm.getByTestId('match-rule-tolerance').fill('0.5');
		await editForm.getByTestId('match-rule-inspection').selectOption('required');
		const edited = savedResponse(page, 'PUT');
		await editForm.getByTestId('match-rule-save').click();
		expect((await edited).status()).toBe(200);
		await expect(reloaded).toContainText('0.5%');
		await expect(reloaded).toContainText('Required');

		// Remove — the first click arms, the second removes.
		await reloaded.getByRole('button', { name: `Remove the rule for ${vendor.name}` }).click();
		const removed = savedResponse(page, 'DELETE');
		await reloaded
			.getByRole('button', {
				name: `Confirm removing the rule for ${vendor.name}`
			})
			.click();
		expect((await removed).status()).toBe(200);
		await expect(reloaded).toHaveCount(0);
	});

	test('a tolerance the server refuses is stated and nothing is saved', async ({ page }) => {
		await page.goto('/organization?section=match-rules');
		const panel = page.getByTestId('match-rules-panel');
		await panel.getByTestId('match-rule-add').click();
		const form = panel.getByTestId('match-rule-form');
		await selectVendorInPicker(vendorPicker(form), vendor.name);
		await form.getByTestId('match-rule-tolerance').fill('150');
		const refused = savedResponse(page, 'PUT');
		await form.getByTestId('match-rule-save').click();
		expect((await refused).status()).toBe(422);
		await expect(page.getByText('The tolerance must be between 0% and 100%.')).toBeVisible();
		await expect(panel.getByTestId('match-rule-row').filter({ hasText: vendor.name })).toHaveCount(
			0
		);
	});

	test('a non-admin sees the notice and the page asks the server nothing', async ({
		page,
		tenantClerk
	}) => {
		let asked = 0;
		page.on('request', (r) => {
			if (new URL(r.url()).pathname.startsWith(RULES)) asked += 1;
		});
		await signInAndWait(page, tenantClerk);
		await page.goto('/organization?section=match-rules');

		const panel = page.getByTestId('match-rules-panel');
		await expect(panel.getByTestId('match-rules-admin-only')).toBeVisible();
		await expect(panel.getByTestId('match-rule-add')).toHaveCount(0);
		expect(asked).toBe(0);
	});
});
