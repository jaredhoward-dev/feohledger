import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';
import { resolveDialog } from './dialogs';
import { exceptionSummary } from './summary';

/**
 * `/exceptions` — voiding a flagged bill in the ERP (decisions §539).
 *
 * A duplicate / fraud flag on an invoice whose bill is already in the ERP
 * offers "Void the bill…" from its resolve dialog, gated on the row's
 * `void_erp_bill_refusal` verdict. The void opens its own confirm dialog
 * (a reason is required) and posts `POST /{id}/void-erp-bill`; a refusal is
 * shown in that dialog, in the reader's language. Stubbed so the verdicts and
 * the server's answers are exact.
 */

const ID = '00000000-0000-4000-9539-000000000001';

function row(verdict: unknown) {
	return {
		id: ID,
		invoice_id: '00000000-0000-4000-9539-0000000000aa',
		invoice_number: 'E2E-VOID-1',
		vendor_name: 'Void Vendor',
		amount: '100.00',
		currency: 'USD',
		exception_type: 'duplicate',
		type_label: 'Duplicate',
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
		decision_refusals: { resolve: null, dismiss: null, escalate: null },
		void_erp_bill_refusal: verdict
	};
}

async function stubQueue(page: Page, verdict: unknown) {
	await page.route('**/api/exceptions/summary*', (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify(
				exceptionSummary({ open: 1, by_type: { duplicate: 1 }, by_severity: { error: 1 } })
			)
		})
	);
	await page.route(
		(url) => url.pathname === '/api/exceptions',
		(route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [row(verdict)], total: 1, page: 1, page_size: 20 })
			})
	);
}

async function openResolve(page: Page) {
	await page.goto('/exceptions');
	await page
		.getByRole('row')
		.filter({ hasText: 'E2E-VOID-1' })
		.getByRole('button', { name: 'Resolve' })
		.click();
	return resolveDialog(page);
}

test.describe('/exceptions — void the flagged bill in the ERP', () => {
	test('offered when allowed: confirm needs a reason, then posts it', async ({ page }) => {
		await stubQueue(page, null);
		let posted: unknown = null;
		await page.route(`**/api/exceptions/${ID}/void-erp-bill`, async (route) => {
			posted = route.request().postDataJSON();
			await route.fulfill({
				status: 201,
				contentType: 'application/json',
				body: JSON.stringify({
					exception_id: ID,
					invoice_id: row(null).invoice_id,
					erp_document_id: 'BILL-9',
					invoice_status: 'approved',
					replayed: false
				})
			});
		});
		const modal = await openResolve(page);
		await modal.getByTestId('erp-void-open').click();
		const confirm = page.getByRole('dialog').filter({ has: page.getByTestId('erp-void-form') });
		await expect(confirm).toBeVisible();
		await expect(confirm.getByTestId('erp-void-confirm')).toBeDisabled();
		await confirm.getByTestId('erp-void-reason').fill('Confirmed duplicate with the vendor');
		await confirm.getByTestId('erp-void-confirm').click();
		await expect(confirm).toBeHidden();
		expect(posted).toEqual({ reason: 'Confirmed duplicate with the vendor' });
	});

	test('a refusal from the server is stated in the confirm dialog', async ({ page }) => {
		await stubQueue(page, null);
		await page.route(`**/api/exceptions/${ID}/void-erp-bill`, (route) =>
			route.fulfill({
				status: 409,
				contentType: 'application/json',
				body: JSON.stringify({
					detail: { code: 'erp_void_bill_paid', message: 'server sentence', params: {} }
				})
			})
		);
		const modal = await openResolve(page);
		await modal.getByTestId('erp-void-open').click();
		const confirm = page.getByRole('dialog').filter({ has: page.getByTestId('erp-void-form') });
		await confirm.getByTestId('erp-void-reason').fill('x');
		await confirm.getByTestId('erp-void-confirm').click();
		await expect(confirm.getByTestId('erp-void-error')).toBeVisible();
	});

	test('refused verdict: the button is disabled and says why', async ({ page }) => {
		await stubQueue(page, { code: 'erp_void_no_bill', message: 'No bill.', params: {} });
		const modal = await openResolve(page);
		await expect(modal.getByTestId('erp-void-open')).toBeDisabled();
		await expect(modal.getByTestId('erp-void-refusal')).toBeVisible();
	});

	test('not offered to a caller without the permission', async ({ page }) => {
		await stubQueue(page, { code: 'role_not_permitted', message: 'No.', params: {} });
		const modal = await openResolve(page);
		await expect(modal.getByTestId('exception-resolve-form')).toBeVisible();
		await expect(modal.getByTestId('erp-void-panel')).toHaveCount(0);
	});
});
