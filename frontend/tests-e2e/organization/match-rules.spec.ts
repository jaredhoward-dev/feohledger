import type { Page, Route } from '@playwright/test';
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
 * a non-admin gets the notice and no request at all. The "stubbed" describe
 * holds the panel's own conventions against a fixed rule set: out-of-range and
 * looser-inspection flags, the picker field error, the GL-list Retry, the
 * armed Remove's outside-click disarm and focus on open / close.
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

	test('focus moves into the form on Add and back to Add on Cancel and Save', async ({ page }) => {
		await page.goto('/organization?section=match-rules');
		const panel = page.getByTestId('match-rules-panel');
		await panel.getByTestId('match-rule-add').click();
		const form = panel.getByTestId('match-rule-form');
		await expect(form.getByRole('radio', { name: 'Vendor' })).toBeFocused();
		await form.getByTestId('match-rule-cancel').click();
		await expect(form).toHaveCount(0);
		await expect(panel.getByTestId('match-rule-add')).toBeFocused();

		await panel.getByTestId('match-rule-add').click();
		await selectVendorInPicker(vendorPicker(form), vendor.name);
		await form.getByTestId('match-rule-tolerance').fill('1');
		const created = savedResponse(page, 'PUT');
		await form.getByTestId('match-rule-save').click();
		expect((await created).status()).toBe(200);
		await expect(panel.getByTestId('match-rule-add')).toBeFocused();
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

const VENDOR_ID = '00000000-0000-4000-8000-000000000512';

function stubbedRules(overrides: Record<string, unknown> = {}) {
	return {
		organization: {
			tolerance_pct: '500',
			require_inspection: true,
			tolerance_out_of_range: true
		},
		built_in: { tolerance_pct: '5', require_inspection: false },
		vendor_rules: [
			{
				vendor_id: VENDOR_ID,
				vendor_name: 'Stubbed Freight Co',
				tolerance_pct: null,
				require_inspection: false,
				tolerance_out_of_range: false
			}
		],
		commodity_rules: [],
		...overrides
	};
}

async function stubRules(page: Page, body = stubbedRules()): Promise<void> {
	await page.route(
		(url) => url.pathname === RULES,
		(route: Route) =>
			route.request().method() === 'GET' ? route.fulfill({ json: body }) : route.fallback()
	);
}

test.describe('/organization — PO match rules (stubbed rule set)', () => {
	test('an out-of-range stored tolerance is flagged, not rewritten; dropping inspection is looser', async ({
		page
	}) => {
		const writes: string[] = [];
		page.on('request', (r) => {
			if (new URL(r.url()).pathname.startsWith(RULES) && r.method() !== 'GET') writes.push(r.url());
		});
		await stubRules(page);
		await page.goto('/organization?section=match-rules');
		const panel = page.getByTestId('match-rules-panel');

		await expect(panel.getByTestId('match-rules-out-of-range')).toContainText('outside 0–100%');
		const orgTol = panel.getByTestId('match-rules-org-tolerance');
		await expect(orgTol).toHaveValue('500');
		await expect(orgTol).toHaveAttribute('aria-invalid', 'true');
		await expect(panel.getByTestId('match-rules-org-out-of-range')).toBeVisible();

		const row = panel.getByTestId('match-rule-row').filter({ hasText: 'Stubbed Freight Co' });
		// No own tolerance, but "Not required" over an organization that
		// requires inspection skips the quality hold: looser.
		await expect(row).toContainText('Looser than the organization');
		expect(writes).toEqual([]);
	});

	test('saving with no vendor chosen marks the picker and moves focus to it', async ({ page }) => {
		await stubRules(page);
		await page.goto('/organization?section=match-rules');
		const panel = page.getByTestId('match-rules-panel');
		await panel.getByTestId('match-rule-add').click();
		const form = panel.getByTestId('match-rule-form');
		await form.getByTestId('match-rule-save').click();

		const picker = form.getByTestId('match-rule-vendor');
		await expect(picker).toBeFocused();
		await expect(picker).toHaveAttribute('aria-invalid', 'true');
		const error = form.getByTestId('match-rule-target-error');
		await expect(error).toHaveText('Choose the vendor the rule applies to.');
		const describedBy = (await picker.getAttribute('aria-describedby')) ?? '';
		expect(describedBy.split(' ')[0]).toBe(await error.getAttribute('id'));
	});

	test('a failed GL-account list is an error with Retry, not an empty select', async ({ page }) => {
		await stubRules(page);
		let fail = true;
		await page.route(
			(url) => url.pathname === '/api/gl-accounts',
			(route: Route) =>
				fail
					? route.fulfill({ status: 500, json: { detail: 'boom' } })
					: route.fulfill({
							json: [{ id: 'gl-1', code: '6100', name: 'Freight', account_type: 'expense' }]
						})
		);
		await page.goto('/organization?section=match-rules');
		const panel = page.getByTestId('match-rules-panel');
		await panel.getByTestId('match-rule-add').click();
		const form = panel.getByTestId('match-rule-form');
		await form.getByRole('radio', { name: 'GL account' }).check();

		const failed = form.getByTestId('match-rule-gl-load-error');
		await expect(failed).toContainText('Could not load the GL accounts.');
		await expect(form.getByTestId('match-rule-gl')).toHaveCount(0);
		fail = false;
		await failed.getByRole('button', { name: 'Retry' }).click();
		await expect(form.getByTestId('match-rule-gl')).toBeVisible();
		await expect(form.getByTestId('match-rule-gl').locator('option[value="6100"]')).toHaveCount(1);
	});

	test('an armed Remove disarms on a click outside it', async ({ page }) => {
		await stubRules(page);
		await page.goto('/organization?section=match-rules');
		const panel = page.getByTestId('match-rules-panel');
		const row = panel.getByTestId('match-rule-row').filter({ hasText: 'Stubbed Freight Co' });
		await row.getByRole('button', { name: 'Remove the rule for Stubbed Freight Co' }).click();
		await expect(
			row.getByRole('button', { name: 'Confirm removing the rule for Stubbed Freight Co' })
		).toBeVisible();
		await panel.getByRole('heading', { name: 'Vendor and GL account rules' }).click();
		await expect(
			row.getByRole('button', { name: 'Remove the rule for Stubbed Freight Co' })
		).toBeVisible();
	});

	test('Edit focuses the tolerance field and Cancel returns focus to that Edit', async ({
		page
	}) => {
		await stubRules(
			page,
			stubbedRules({
				vendor_rules: [
					{
						vendor_id: VENDOR_ID,
						vendor_name: 'Stubbed Freight Co',
						tolerance_pct: '2.5',
						require_inspection: null,
						tolerance_out_of_range: false
					}
				]
			})
		);
		await page.goto('/organization?section=match-rules');
		const panel = page.getByTestId('match-rules-panel');
		const edit = panel.getByRole('button', { name: 'Edit the rule for Stubbed Freight Co' });
		await edit.click();
		const form = panel.getByTestId('match-rule-form');
		const tolerance = form.getByTestId('match-rule-tolerance');
		await expect(tolerance).toBeFocused();
		// English reader: the stored "2.5" pre-fills as written.
		await expect(tolerance).toHaveValue('2.5');
		await form.getByTestId('match-rule-cancel').click();
		await expect(edit).toBeFocused();
	});
});
