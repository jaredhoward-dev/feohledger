import type { Page } from '@playwright/test';
import type {
	CorporateCardTransaction,
	ExpensePolicy,
	ExpensePreapproval
} from '#lib/types/expense.ts';
import { expect, test } from '../fixtures/helpers';

/**
 * /expenses: every list and count on the page describes the set the server
 * holds, and a failed read is never shown as an empty one.
 *
 * - The Policies and Pre-approvals tabs read only the server's first page
 *   (20 rows) with no footer, so row 21 onward could not be reached.
 * - The Cards tab's Unmatched / Matched cards counted the LOADED page while
 *   "Transactions" beside them was the server's total.
 * - A failed match-suggestions read toasted, then told the user there were no
 *   candidates and to create an expense instead.
 * - A `?tab=` with no panel rendered a tablist with nothing selected.
 *
 * Stubbed: each test hands the page the server's answer and asserts the
 * page's reading of it. Stubs match the exact API pathname
 * (`tests-e2e/README.md` § Stubbing).
 */

const STAMP = '2026-06-01T00:00:00Z';

function policy(i: number): ExpensePolicy {
	return {
		id: `00000000-0000-0000-0000-00000000a${String(i).padStart(3, '0')}`,
		name: `Stub policy ${i}`,
		active: true,
		category: null,
		threshold_currency: 'USD',
		per_diem_amount: null,
		per_diem_currency: null,
		mileage_rate: null,
		category_limit: null,
		requires_preapproval_above: null,
		requires_receipt_above: null,
		rules: null,
		created_at: STAMP,
		updated_at: STAMP
	} as ExpensePolicy;
}

function preapproval(i: number): ExpensePreapproval {
	return {
		id: `00000000-0000-0000-0000-000000000${String(i).padStart(3, '0')}`,
		requester_user_id: '00000000-0000-0000-0000-0000000000ff',
		title: `Stub request ${i}`,
		estimated_amount: '10.00',
		currency: 'USD',
		category: null,
		justification: null,
		status: 'approved',
		decided_by: null,
		decided_at: null,
		expense_report_id: null,
		created_at: STAMP,
		updated_at: STAMP
	} as ExpensePreapproval;
}

function cardTxn(i: number, status: string): CorporateCardTransaction {
	return {
		id: `00000000-0000-0000-0000-000000000${String(i).padStart(3, '0')}`,
		card_ref: null,
		card_last_four: '4242',
		virtual_card_id: null,
		txn_date: '2026-06-01',
		posted_date: null,
		merchant: `Stub merchant ${i}`,
		amount: '12.00',
		currency: 'USD',
		external_txn_id: null,
		matched_expense_id: null,
		reconciliation_status: status,
		import_batch: null,
		created_at: STAMP,
		updated_at: STAMP,
		actions: {
			match: null,
			create_expense: null,
			unmatch: { code: 'x', message: 'x', params: {} },
			ignore: null
		}
	} as CorporateCardTransaction;
}

/** Serve `rows` the way the backend pages them: `page` / `page_size` from the query. */
async function stubPaged<T>(page: Page, pathname: string, rows: T[]): Promise<string[]> {
	const pagesAsked: string[] = [];
	await page.route(
		(url) => url.pathname === pathname,
		(route) => {
			const url = new URL(route.request().url());
			const p = Number(url.searchParams.get('page') ?? '1');
			const size = Number(url.searchParams.get('page_size') ?? '20');
			pagesAsked.push(String(p));
			const items = rows.slice((p - 1) * size, p * size);
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items, total: rows.length, page: p, page_size: size })
			});
		}
	);
	return pagesAsked;
}

test.describe('/expenses lists reach every row', () => {
	test('the Policies tab pages past the first page', async ({ page }) => {
		const rows = Array.from({ length: 60 }, (_, i) => policy(i + 1));
		await stubPaged(page, '/api/expense-policies', rows);
		await page.goto('/expenses?tab=policies');

		const tbody = page.locator('[role="tabpanel"] table tbody');
		await expect(tbody.locator('tr')).toHaveCount(50);
		const more = page.getByTestId('expense-policies-load-more');
		await expect(more).toHaveText('Load more (50 of 60)');
		await more.click();
		await expect(tbody.locator('tr')).toHaveCount(60);
		await expect(tbody).toContainText('Stub policy 60');
		await expect(page.locator('.load-more-end')).toHaveText('Showing all 60 policies');
	});

	test('the Pre-approvals tab pages past the first page', async ({ page }) => {
		const rows = Array.from({ length: 55 }, (_, i) => preapproval(i + 1));
		await stubPaged(page, '/api/expense-preapprovals', rows);
		await page.goto('/expenses?tab=preapprovals');

		const tbody = page.locator('[role="tabpanel"] table tbody');
		await expect(tbody.locator('tr')).toHaveCount(50);
		const more = page.getByTestId('expense-preapprovals-load-more');
		await expect(more).toHaveText('Load more (50 of 55)');
		await more.click();
		await expect(tbody.locator('tr')).toHaveCount(55);
		await expect(tbody).toContainText('Stub request 55');
		await expect(page.locator('.load-more-end')).toHaveText(
			'Showing all 55 pre-approval requests'
		);
	});
});

test.describe('/expenses Cards tab', () => {
	test('the KPI cards count the whole feed, whatever is loaded or filtered', async ({ page }) => {
		// 70 unmatched + 5 matched: the first page (50 rows, newest first) holds
		// only unmatched rows, so counting the loaded page read "Matched 0" and
		// "Unmatched 50".
		const rows = [
			...Array.from({ length: 70 }, (_, i) => cardTxn(i + 1, 'unmatched')),
			...Array.from({ length: 5 }, (_, i) => cardTxn(i + 71, 'matched'))
		];
		await page.route(
			(url) => url.pathname === '/api/corporate-card-transactions',
			(route) => {
				const url = new URL(route.request().url());
				const status = url.searchParams.get('reconciliation_status');
				const p = Number(url.searchParams.get('page') ?? '1');
				const size = Number(url.searchParams.get('page_size') ?? '20');
				const set = status ? rows.filter((r) => r.reconciliation_status === status) : rows;
				return route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({
						items: set.slice((p - 1) * size, p * size),
						total: set.length,
						page: p,
						page_size: size
					})
				});
			}
		);
		await page.goto('/expenses?tab=cards');

		const kpi = (label: string) =>
			page
				.locator('.kpi')
				.filter({ has: page.locator('.kpi-label', { hasText: new RegExp(`^${label}$`) }) })
				.locator('.kpi-value');
		await expect(kpi('Unmatched')).toHaveText('70');
		await expect(kpi('Matched')).toHaveText('5');
		await expect(kpi('Transactions')).toHaveText('75');

		// Filtered to Matched, the cards still describe the whole feed.
		await page.getByRole('button', { name: 'Matched', exact: true }).click();
		await expect(page.locator('[role="tabpanel"] table tbody tr')).toHaveCount(5);
		await expect(kpi('Unmatched')).toHaveText('70');
		await expect(kpi('Transactions')).toHaveText('75');
	});

	test('a failed suggestions read is an error with Retry, not "no candidates"', async ({
		page
	}) => {
		const row = cardTxn(1, 'unmatched');
		await page.route(
			(url) => url.pathname === '/api/corporate-card-transactions',
			(route) =>
				route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({ items: [row], total: 1, page: 1, page_size: 50 })
				})
		);
		let calls = 0;
		await page.route(
			(url) => url.pathname === `/api/corporate-card-transactions/${row.id}/match-suggestions`,
			(route) => {
				calls += 1;
				return calls === 1
					? route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' })
					: route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
			}
		);
		await page.goto('/expenses?tab=cards');
		await page.getByRole('button', { name: 'Match', exact: true }).click();

		const dialog = page.getByRole('dialog');
		await expect(dialog.getByTestId('expense-match-error')).toBeVisible();
		await expect(dialog).not.toContainText('No candidate expenses');

		await dialog.getByRole('button', { name: 'Retry' }).click();
		await expect(dialog.getByTestId('expense-match-error')).toHaveCount(0);
		await expect(dialog).toContainText('No candidate expenses');
	});
});

test('an unknown ?tab= opens the Expenses tab', async ({ page }) => {
	await page.goto('/expenses?tab=nonsense');
	await expect(page.getByRole('tab', { name: 'Expenses', exact: true })).toHaveAttribute(
		'aria-selected',
		'true'
	);
	await expect(page.locator('#expenses-panel-expenses')).toBeVisible();
});
