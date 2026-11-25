import { expect, test } from '../fixtures/helpers';
import type { Page, Route } from '@playwright/test';

/**
 * /contracts — URL state, the `?id=` deep link, and the bulk selection.
 *
 * Every response is intercepted, so nothing here writes to the tenant and the
 * file is parallel-safe. The defects it pins:
 *
 *  - "Select all N matching" survived a filter change. The resolved id set
 *    stayed selected (and the prune effect stayed off), so a bulk Terminate
 *    after switching to "Draft" went to every contract the PREVIOUS filter
 *    matched.
 *  - The URL writer rebuilt from `page.url`, which a shallow `replaceState`
 *    never updates. After closing a deep-linked contract, the next filter
 *    change wrote the closed contract's `?id=` back, so a reload re-opened it.
 *  - Opening a contract from its row never wrote `?id=`, so a reload or a
 *    copied link lost it.
 *  - Any failed deep-link fetch toasted "Contract not found", a 500 included,
 *    with no Retry.
 */

function contract(n: number, status = 'active') {
	return {
		id: `00000000-0000-4000-b000-${String(n).padStart(12, '0')}`,
		contract_number: `E2E-CTR-${n}`,
		title: null,
		description: null,
		contract_type: 'msa',
		status,
		vendor_id: '00000000-0000-4000-c000-000000000001',
		vendor_name: 'E2E Vendor',
		currency: 'USD',
		total_value: '1000.00',
		spend_limit: null,
		not_to_exceed: false,
		start_date: null,
		end_date: null,
		signed_date: null,
		auto_renew: false,
		renewal_term_months: null,
		renewal_notice_days: 30,
		renewal_alert_sent_at: null,
		payment_terms: null,
		owner_user_id: null,
		file_url: null,
		file_key: null,
		terms: null,
		line_items: [],
		spend: null,
		created_at: '2026-01-01T00:00:00Z',
		updated_at: '2026-01-01T00:00:00Z'
	};
}

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

/** List: two rows of five on "all", one on "draft". Detail answers 200. */
async function mockList(page: Page) {
	await page.route('**/api/contracts?*', async (route) => {
		const url = new URL(route.request().url());
		if (url.pathname !== '/api/contracts') return route.continue();
		if (url.searchParams.get('status') === 'draft') {
			return json(route, { items: [contract(1, 'draft')], total: 1 });
		}
		return json(route, { items: [contract(1), contract(2)], total: 5 });
	});
}

async function mockDetail(page: Page, status: () => number = () => 200) {
	await page.route(/\/api\/contracts\/[0-9a-f-]{36}$/, async (route) => {
		const id = new URL(route.request().url()).pathname.split('/').pop()!;
		const n = Number(id.slice(-12));
		const code = status();
		if (code !== 200) return json(route, { detail: 'boom' }, code);
		return json(route, { ...contract(n), id });
	});
}

const dialog = (page: Page) => page.getByRole('dialog', { name: 'Contract detail' });

test.describe('/contracts — URL state and selection', () => {
	test('a filter change drops "all matching" back to the rows on screen', async ({ page }) => {
		await mockList(page);
		await page.route('**/api/contracts/ids*', (route) =>
			json(route, {
				ids: [1, 2, 3, 4, 5].map((n) => contract(n).id),
				total: 5,
				truncated: false
			})
		);
		await page.goto('/contracts');
		await expect(page.getByRole('button', { name: 'Open contract E2E-CTR-2' })).toBeVisible();

		await page.getByRole('checkbox', { name: 'Select all contracts on this page' }).check();
		await page.getByRole('button', { name: 'Select all 5 matching' }).click();
		await expect(page.getByText('All matching selected')).toBeVisible();
		await expect(page.locator('.bulk-count')).toContainText('5 selected');

		await page.getByRole('button', { name: 'Draft', exact: true }).click();
		await expect(page.getByRole('button', { name: 'Open contract E2E-CTR-2' })).toBeHidden();

		// Only the still-visible, still-selected row remains selected.
		await expect(page.locator('.bulk-count')).toContainText('1 selected');
		await expect(page.getByText('All matching selected')).toBeHidden();
		await expect(
			page.getByRole('checkbox', { name: 'Select contract E2E-CTR-1' })
		).toBeChecked();
	});

	test('opening a row writes ?id= and a reload re-opens it', async ({ page }) => {
		await mockList(page);
		await mockDetail(page);
		await page.goto('/contracts');
		await page.getByRole('button', { name: 'Open contract E2E-CTR-2' }).click();
		await expect(dialog(page)).toBeVisible();
		await expect(page).toHaveURL(new RegExp(`[?&]id=${contract(2).id}`));

		await page.reload();
		await expect(dialog(page)).toBeVisible();
		await expect(dialog(page)).toContainText('E2E-CTR-2');
	});

	test('closing a deep-linked contract keeps later filters and never re-writes its ?id=', async ({
		page
	}) => {
		await mockList(page);
		await mockDetail(page);
		await page.goto(`/contracts?id=${contract(1).id}`);
		await expect(dialog(page)).toBeVisible();

		await page.keyboard.press('Escape');
		await expect(dialog(page)).toBeHidden();
		await expect(page).not.toHaveURL(/[?&]id=/);

		await page.getByRole('button', { name: 'Draft', exact: true }).click();
		await expect(page).toHaveURL(/[?&]status=draft/);
		await expect(page).not.toHaveURL(/[?&]id=/);

		await page.reload();
		await expect(page.getByRole('button', { name: 'Draft', exact: true })).toHaveAttribute(
			'aria-pressed',
			'true'
		);
		await expect(page.getByRole('button', { name: 'Open contract E2E-CTR-1' })).toBeVisible();
		await expect(dialog(page)).toBeHidden();
	});

	test('a deep link that fails to load says so with a Retry, not "not found"', async ({
		page
	}) => {
		await mockList(page);
		let fail = true;
		await mockDetail(page, () => (fail ? 500 : 200));
		await page.goto(`/contracts?id=${contract(3).id}`);

		const error = page.getByTestId('contracts-deeplink-error');
		await expect(error).toContainText('Couldn’t load the linked contract');
		await expect(page.getByText('Contract not found')).toHaveCount(0);
		await expect(page).toHaveURL(new RegExp(`[?&]id=${contract(3).id}`));

		fail = false;
		await error.getByRole('button', { name: 'Retry' }).click();
		await expect(dialog(page)).toBeVisible();
		await expect(error).toBeHidden();
	});

	test('a deep link to a missing contract says "not found" and leaves the URL', async ({
		page
	}) => {
		await mockList(page);
		await mockDetail(page, () => 404);
		await page.goto(`/contracts?id=${contract(4).id}`);
		await expect(page.getByText('Contract not found')).toBeVisible();
		await expect(page).not.toHaveURL(/[?&]id=/);
		await expect(page.getByTestId('contracts-deeplink-error')).toHaveCount(0);
	});

	// The backend 422s a sort key outside its allowlist; read raw, a stale link
	// would fail every load, Retry included.
	test('an unknown sort in the URL falls back to the default order', async ({ page }) => {
		await mockList(page);
		const listRequest = page.waitForRequest((r) => {
			const url = new URL(r.url());
			return url.pathname === '/api/contracts';
		});
		await page.goto('/contracts?sort=not_a_column&order=desc');
		expect(new URL((await listRequest).url()).searchParams.has('sort')).toBe(false);

		await expect(page.getByRole('button', { name: 'Open contract E2E-CTR-2' })).toBeVisible();
		await expect(page.getByTestId('contracts-error')).toHaveCount(0);
	});

	// A client-side navigation to the same route (the sidebar row's shape, via
	// an in-page link so the nav group's open state does not matter) and Back
	// must re-read the URL: the filters were seeded at mount only.
	test('a same-route navigation and Back re-apply the URL', async ({ page }) => {
		await mockList(page);
		const draftChip = page.getByRole('button', { name: 'Draft', exact: true });
		await page.goto('/contracts?status=draft');
		await expect(draftChip).toHaveAttribute('aria-pressed', 'true');
		await expect(page.getByRole('button', { name: 'Open contract E2E-CTR-2' })).toBeHidden();

		await page.evaluate(() => {
			const a = document.createElement('a');
			a.href = '/contracts';
			a.id = 'e2e-same-route';
			a.textContent = 'same route';
			document.body.appendChild(a);
		});
		await page.locator('#e2e-same-route').click();
		await expect(page).toHaveURL(/\/contracts$/);
		await expect(draftChip).toHaveAttribute('aria-pressed', 'false');
		await expect(page.getByRole('button', { name: 'Open contract E2E-CTR-2' })).toBeVisible();

		await page.goBack();
		await expect(page).toHaveURL(/[?&]status=draft/);
		await expect(draftChip).toHaveAttribute('aria-pressed', 'true');
		await expect(page.getByRole('button', { name: 'Open contract E2E-CTR-2' })).toBeHidden();
	});
});
