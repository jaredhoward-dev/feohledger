import type { Page } from '@playwright/test';

import { expect, test } from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';

/**
 * /payments queue — a row whose payment schedule moved its due date
 * (decisions §565). `due_date` is the EFFECTIVE date — the one `is_overdue`
 * and the queue's server-side order read — and `own_due_date` the invoice's
 * own. The row shows the effective date with "rescheduled from <own>" beneath
 * it, and keeps the server's order: the page never re-sorts on the own date.
 *
 * Stubbed, because a rescheduled invoice in a known position is not
 * producible on demand against a seeded tenant.
 */

const DESKTOP = { width: 1280, height: 800 };
const PHONE = { width: 320, height: 800 };

function queueItem(tag: string, patch: Record<string, unknown>) {
	return {
		id: `00000000-0000-4000-8000-${tag.padStart(12, '0')}`,
		invoice_number: `INV-${tag}`,
		vendor_name: `Vendor ${tag}`,
		amount: '100.00',
		currency: 'USD',
		due_date: '2026-09-30',
		own_due_date: '2026-09-30',
		payment_terms: null,
		status: 'approved',
		is_overdue: false,
		discount_eligible: false,
		discount_date: null,
		discount_percent: null,
		discount_amount: null,
		payable_amount: '100.00',
		blocked: false,
		blocked_reason: null,
		required_method: null,
		...patch
	};
}

/** Two rows in the server's effective-date order: 9101 moved EARLIER than its
 *  own date (which is later than 9102's), so an own-date sort would swap them. */
async function stubQueue(page: Page) {
	const items = [
		queueItem('9101', { due_date: '2026-09-20', own_due_date: '2026-10-15', is_overdue: true }),
		queueItem('9102', {})
	];
	await page.route(
		(url) => url.pathname === '/api/payments/queue',
		(route) =>
			route.fulfill({
				json: {
					items,
					total: items.length,
					page: 1,
					page_size: 50,
					selectable_total: items.length,
					blocked_total: 0,
					currency: 'USD',
					by_currency: [
						{ currency: 'USD', count: 2, total_amount: '200.00', total_savings: '0.00' }
					]
				}
			})
	);
}

test.describe('/payments queue — a rescheduled due date', () => {
	test('shows both dates and keeps the effective-date order', async ({ page }) => {
		await stubQueue(page);
		await page.goto('/payments');

		const rows = page.locator('table tbody tr');
		const moved = rows.filter({ hasText: 'INV-9101' });
		const unmoved = rows.filter({ hasText: 'INV-9102' });
		await expect(moved).toBeVisible();

		await expect(moved.getByTestId('due-date')).toHaveAttribute('data-rescheduled', 'true');
		await expect(moved.getByTestId('due-rescheduled-from')).toContainText('rescheduled from');
		await expect(moved.locator('.overdue-badge')).toBeVisible();
		await expect(unmoved.getByTestId('due-date')).toBeVisible();
		await expect(unmoved.getByTestId('due-rescheduled-from')).toHaveCount(0);

		// The server's order, by effective date: 9101 (20 Sep) before 9102
		// (30 Sep), although 9101's own date (15 Oct) is the later one.
		const order = await rows.evaluateAll((trs) =>
			trs
				.map((tr) => tr.textContent ?? '')
				.map((t) => (t.includes('INV-9101') ? 9101 : t.includes('INV-9102') ? 9102 : null))
				.filter((n) => n !== null)
		);
		expect(order).toEqual([9101, 9102]);

		// Reflow (SC 1.4.10) and the rest of AA, with both dates on screen.
		await expectNoA11yViolations(page);
		await page.setViewportSize(PHONE);
		await expect(moved.getByTestId('due-rescheduled-from')).toBeVisible();
		await expectNoA11yViolations(page);
		await page.setViewportSize(DESKTOP);
	});
});
