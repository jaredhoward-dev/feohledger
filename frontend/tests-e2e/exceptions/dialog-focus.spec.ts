import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';
import { bulkResolveDialog, resolveDialog } from './dialogs';
import { exceptionSummary } from './summary';

/**
 * `/exceptions` — where focus lands when a resolve dialog opens.
 *
 * Both dialogs asked for their note field with the native `autofocus`
 * attribute, which never took: Svelte only honours it while focus sits on
 * `<body>` (it is on the row's Resolve button), and the shared focus trap then
 * puts focus on the dialog's FIRST control — the ⓘ help button beside the
 * single-row title. The keyboard operator opened "Resolve" and had to Tab
 * before typing the note every decision except a dismissal needs. The note is
 * now marked `data-autofocus`, the focus trap's own opt-in (`focusTrap.ts`).
 *
 * Stubbed so the row and its verdicts are exact.
 */

function row(n: number) {
	return {
		id: `00000000-0000-4000-9500-${String(n).padStart(12, '0')}`,
		invoice_id: null,
		invoice_number: `E2E-FOCUS-${n}`,
		vendor_name: 'Focus Vendor',
		amount: '100.00',
		currency: 'USD',
		exception_type: 'fraud_flag',
		type_label: 'Fraud Flag',
		severity: 'error',
		description: 'stubbed',
		status: 'open',
		resolution: null,
		resolved_by: null,
		resolved_at: null,
		assigned_to: null,
		assigned_to_user_id: null,
		due_at: null,
		is_overdue: false,
		time_to_resolution_hours: null,
		created_at: '2026-01-01T00:00:00Z',
		decision_refusals: { resolve: null, dismiss: null, escalate: null }
	};
}

async function stubQueue(page: Page) {
	await page.route('**/api/exceptions/summary*', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(
				exceptionSummary({ open: 2, by_type: { fraud_flag: 2 }, by_severity: { error: 2 } })
			)
		})
	);
	await page.route(
		(url) => url.pathname === '/api/exceptions',
		(route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [row(1), row(2)], total: 2, page: 1, page_size: 20 })
			})
	);
}

test.describe('/exceptions — resolve dialogs open on the note field', () => {
	test.beforeEach(async ({ page }) => {
		await stubQueue(page);
		await page.goto('/exceptions');
		await expect(page.getByText('E2E-FOCUS-1')).toBeVisible();
	});

	test('single-row Resolve', async ({ page }) => {
		const trigger = page
			.getByRole('row')
			.filter({ hasText: 'E2E-FOCUS-1' })
			.getByRole('button', { name: 'Resolve' });
		await trigger.click();
		const modal = resolveDialog(page);
		await expect(modal).toBeVisible();
		await expect(modal.locator('input[type="text"]')).toBeFocused();

		// And back to the trigger on Cancel (the row is still there).
		await modal.getByRole('button', { name: 'Cancel' }).click();
		await expect(trigger).toBeFocused();
	});

	test('bulk Resolve', async ({ page }) => {
		await page.getByLabel('Select all selectable exceptions').check();
		await page.getByRole('button', { name: 'Resolve 2' }).click();
		const modal = bulkResolveDialog(page);
		await expect(modal).toBeVisible();
		await expect(modal.locator('input[type="text"]')).toBeFocused();
	});
});

/**
 * When the "record the ERP's payment" panel shows, it sits ABOVE the note, so
 * the dialog opens on its first control (the focus trap's default) rather than
 * skipping a keyboard reader past the panel to the note.
 */
test('single-row Resolve with the ERP payment panel opens on the first control', async ({
	page
}) => {
	const erpRow = {
		...row(3),
		invoice_id: '00000000-0000-4000-9500-0000000000aa',
		exception_type: 'payment_reconciliation',
		type_label: 'Payment Reconciliation',
		erp_report: {
			kind: 'paid_not_recorded',
			erp_type: 'quickbooks_online',
			erp_document_id: 'BILL-FOCUS',
			event_id: null,
			method: 'ach',
			reference: 'BILL-FOCUS',
			paid_on: '2026-10-01',
			paid_on_reported: true,
			amount: '100.00',
			amount_currency: 'USD',
			payment_parts: null,
			refusal_code: 'external_payment_in_run'
		},
		erp_name: 'QuickBooks Online',
		record_erp_payment_refusal: null
	};
	await page.route('**/api/exceptions/summary*', (route) =>
		route.fulfill({
			json: exceptionSummary({
				open: 1,
				by_type: { payment_reconciliation: 1 },
				by_severity: { error: 1 }
			})
		})
	);
	await page.route(
		(url) => url.pathname === '/api/exceptions',
		(route) => route.fulfill({ json: { items: [erpRow], total: 1, page: 1, page_size: 20 } })
	);
	await page.goto('/exceptions');
	await page
		.getByRole('row')
		.filter({ hasText: 'E2E-FOCUS-3' })
		.getByRole('button', { name: 'Resolve' })
		.click();

	const modal = resolveDialog(page);
	await expect(modal.getByTestId('erp-payment-panel')).toBeVisible();
	// The focus trap's first visible control, in DOM order — not the note.
	await expect
		.poll(() =>
			modal.evaluate((dialog) => {
				const first = [
					...dialog.querySelectorAll<HTMLElement>(
						'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
					)
				].find((el) => el.offsetWidth > 0 || el.offsetHeight > 0);
				return !!first && first === document.activeElement;
			})
		)
		.toBe(true);
	await expect(
		modal.getByTestId('exception-resolve-form').locator('input[type="text"]')
	).not.toBeFocused();
});

