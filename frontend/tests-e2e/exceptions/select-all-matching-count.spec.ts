import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';
import { exceptionSummary } from './summary';

/**
 * `/exceptions` under the All status chip — "Select all N matching".
 *
 * The button's N was the list's `total`, which under All counts resolved and
 * dismissed rows too, while the selection it makes (`GET /api/exceptions/ids`
 * with `status=open,escalated`) can only ever hold open + escalated rows. So it
 * promised more than it selected, and it offered itself when every selectable
 * row was already selected (only closed rows lay beyond the page). N is now
 * the summary's open + escalated within the same filters.
 *
 * Stubbed so the counts are exact.
 */

const CLOSED = { code: 'already_closed', message: 'This exception is already closed.' };

function row(n: number, status: 'open' | 'resolved') {
	return {
		id: `00000000-0000-4000-9600-${String(n).padStart(12, '0')}`,
		invoice_id: null,
		invoice_number: `E2E-SAM-${n}`,
		vendor_name: 'Select Vendor',
		amount: '100.00',
		currency: 'USD',
		exception_type: 'duplicate',
		type_label: 'Duplicate',
		severity: 'warning',
		description: 'stubbed',
		status,
		resolution: null,
		resolved_by: null,
		resolved_at: null,
		assigned_to: null,
		assigned_to_user_id: null,
		due_at: null,
		is_overdue: false,
		time_to_resolution_hours: null,
		created_at: '2026-01-01T00:00:00Z',
		decision_refusals:
			status === 'open'
				? { resolve: null, dismiss: null, escalate: null }
				: { resolve: CLOSED, dismiss: CLOSED, escalate: CLOSED }
	};
}

async function stub(
	page: Page,
	opts: { items: ReturnType<typeof row>[]; total: number; open: number; resolved: number }
) {
	await page.route('**/api/exceptions/summary*', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(
				exceptionSummary({
					open: opts.open,
					resolved: opts.resolved,
					by_type: { duplicate: opts.open + opts.resolved },
					by_severity: { warning: opts.open + opts.resolved }
				})
			)
		})
	);
	await page.route(
		(url) => url.pathname === '/api/exceptions',
		(route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: opts.items, total: opts.total, page: 1, page_size: 20 })
			})
	);
}

test.describe('/exceptions — Select all N matching under the All chip', () => {
	test('N counts the open + escalated rows, not the closed ones', async ({ page }) => {
		// 20 open rows on page 1; 22 open + 3 resolved in the whole set.
		const items = Array.from({ length: 20 }, (_, i) => row(i + 1, 'open'));
		await stub(page, { items, total: 25, open: 22, resolved: 3 });

		await page.goto('/exceptions?status=all');
		await expect(page.getByText('E2E-SAM-1', { exact: true })).toBeVisible();
		await expect(page.getByRole('button', { name: /^Open/ }).locator('.count')).toHaveText('22');

		await page.getByLabel('Select all selectable exceptions').check();
		await expect(page.getByRole('button', { name: 'Select all 22 matching' })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Select all 25 matching' })).toHaveCount(0);
	});

	test('it is not offered when only closed rows lie beyond the selection', async ({ page }) => {
		await stub(page, {
			items: [row(1, 'open'), row(2, 'open'), row(3, 'resolved')],
			total: 3,
			open: 2,
			resolved: 1
		});

		await page.goto('/exceptions?status=all');
		await expect(page.getByRole('button', { name: /^Open/ }).locator('.count')).toHaveText('2');

		await page.getByLabel('Select all selectable exceptions').check();
		// The bulk bar is up (two rows selected) …
		await expect(page.getByRole('button', { name: 'Resolve 2' })).toBeVisible();
		// … with no "select all" that could only re-select the same two.
		await expect(page.getByRole('button', { name: /^Select all \d+ matching$/ })).toHaveCount(0);
	});
});
