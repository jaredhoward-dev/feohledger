import type { Page } from '@playwright/test';
import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	signInAndWait,
	tenantBase,
	test
} from '../fixtures/helpers';

/**
 * /organization → Line-total check and Exception agents (decisions §502).
 *
 * Each panel reads and writes one key through its own audited endpoint
 * (`backend/app/api/organization_controls.py`); the server-side audit row,
 * validation and the generic PATCH's carry/refuse are proven in
 * `backend/tests/test_organization_controls.py`. What this spec holds is the
 * page's side: the panel loads the stored value, a change saves, a reload
 * shows it, and a non-admin gets the notice and no request at all.
 *
 * Every test starts from, and the suite leaves, the defaults (check on,
 * `conservative`), since the e2e tenant is shared by every spec on a worker.
 */

const LINE_TOTAL = '/api/organization/line-total-check';
const AUTONOMY = '/api/organization/exception-agents/autonomy';

async function resetControls(page: Page): Promise<void> {
	const headers = await authedTenantHeaders(page);
	const a = await page.request.put(`${API_BASE}${LINE_TOTAL}`, {
		headers,
		data: { enabled: true }
	});
	expect(a.ok()).toBe(true);
	const b = await page.request.put(`${API_BASE}${AUTONOMY}`, {
		headers,
		data: { autonomy_level: 'conservative' }
	});
	expect(b.ok()).toBe(true);
}

function savedResponse(page: Page, path: string) {
	return page.waitForResponse(
		(r) => new URL(r.url()).pathname === path && r.request().method() === 'PUT'
	);
}

test.describe('/organization — line-total check and exception-agent autonomy', () => {
	test.beforeEach(async ({ page }) => {
		await resetControls(page);
	});

	test.afterAll(async ({ browser }) => {
		const ctx = await browser.newContext({ baseURL: tenantBase(currentTenantSlug()) });
		const page = await ctx.newPage();
		try {
			await signInAndWait(page);
			await resetControls(page);
		} finally {
			await ctx.close();
		}
	});

	test('line-total check: turning it off saves, and a reload shows it off', async ({ page }) => {
		await page.goto('/organization?section=line-total-check');
		const panel = page.getByTestId('line-total-panel');
		const toggle = panel.getByTestId('line-total-toggle');
		const save = panel.getByTestId('line-total-save');

		await expect(toggle).toBeChecked();
		// Nothing to save until something changes.
		await expect(save).toBeDisabled();

		await toggle.uncheck();
		await expect(panel.getByTestId('line-total-off-warning')).toBeVisible();
		const saved = savedResponse(page, LINE_TOTAL);
		await save.click();
		expect((await saved).status()).toBe(200);
		await expect(save).toBeDisabled();

		await page.reload();
		await expect(page.getByTestId('line-total-toggle')).not.toBeChecked();
	});

	test('autonomy: choosing Balanced saves, and a reload shows it chosen', async ({ page }) => {
		await page.goto('/organization?section=exception-agents');
		const panel = page.getByTestId('agent-autonomy-panel');
		const save = panel.getByTestId('agent-autonomy-save');

		await expect(panel.getByTestId('agent-autonomy-conservative')).toBeChecked();
		await expect(save).toBeDisabled();

		await panel.getByRole('radio', { name: /^Balanced/ }).check();
		const saved = savedResponse(page, AUTONOMY);
		await save.click();
		expect((await saved).status()).toBe(200);
		await expect(save).toBeDisabled();

		await page.reload();
		await expect(page.getByTestId('agent-autonomy-balanced')).toBeChecked();
	});

	test('saving Fraud Detection leaves the line-total check as the panel set it', async ({
		page
	}) => {
		const headers = await authedTenantHeaders(page);
		await page.request.put(`${API_BASE}${LINE_TOTAL}`, { headers, data: { enabled: false } });

		await page.goto('/organization?section=fraud');
		const card = page.locator('section.card', { hasText: 'Fraud Detection' });
		await expect(card).toBeVisible();
		const patched = page.waitForResponse(
			(r) => r.url().endsWith('/api/organization') && r.request().method() === 'PATCH'
		);
		await card.getByRole('button', { name: 'Save' }).click();
		expect((await patched).status()).toBe(200);

		const after = await page.request.get(`${API_BASE}${LINE_TOTAL}`, { headers });
		expect(await after.json()).toEqual({ enabled: false });
	});

	for (const [section, testId, path] of [
		['line-total-check', 'line-total', LINE_TOTAL],
		['exception-agents', 'agent-autonomy', AUTONOMY]
	] as const) {
		test(`a non-admin sees the ${section} notice and the page asks the server nothing`, async ({
			page,
			tenantClerk
		}) => {
			let asked = 0;
			page.on('request', (r) => {
				if (new URL(r.url()).pathname === path) asked += 1;
			});
			await signInAndWait(page, tenantClerk);
			await page.goto(`/organization?section=${section}`);

			const panel = page.getByTestId(`${testId}-panel`);
			await expect(panel.getByTestId(`${testId}-admin-only`)).toBeVisible();
			await expect(panel.getByTestId(`${testId}-save`)).toHaveCount(0);
			expect(asked).toBe(0);
		});
	}
});
