import type { Page } from '@playwright/test';
import { en } from '#lib/i18n/locales/en.ts';
import { enRefusal } from '#lib/i18n/locales/refusal/en.ts';
import { expectNoA11yViolations } from '../a11y/axe-helper';
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
 * shown in that dialog, in the reader's language. The confirm STACKS on the
 * flag's dialog: closing it returns to the flag, and a void leaves the flag
 * open (re-read) for a person to decide. Stubbed so the verdicts and the
 * server's answers are exact.
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

/** `GET /api/exceptions/{ID}` — the detail read behind `?id=` and the
 *  re-read after a void. Returns the verdict `verdict()` names at call time. */
async function stubDetail(page: Page, verdict: () => unknown) {
	let reads = 0;
	await page.route(
		(url) => url.pathname === `/api/exceptions/${ID}`,
		(route) => {
			reads += 1;
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(row(verdict()))
			});
		}
	);
	return () => reads;
}

function confirmDialog(page: Page) {
	return page.getByRole('dialog').filter({ has: page.getByTestId('erp-void-form') });
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
		// After the void the bill is gone: the re-read refuses a second one.
		let voided = false;
		const reads = await stubDetail(page, () =>
			voided ? { code: 'erp_void_no_bill', message: 'No bill.', params: {} } : null
		);
		await page.route(`**/api/exceptions/${ID}/void-erp-bill`, async (route) => {
			voided = true;
			await route.fallback();
		});
		const modal = await openResolve(page);
		await modal.getByTestId('erp-void-open').click();
		const confirm = confirmDialog(page);
		await expect(confirm).toBeVisible();
		await expect(confirm.getByTestId('erp-void-confirm')).toBeDisabled();
		await confirm.getByTestId('erp-void-reason').fill('Confirmed duplicate with the vendor');
		await confirm.getByTestId('erp-void-confirm').click();
		await expect(confirm).toBeHidden();
		expect(posted).toEqual({ reason: 'Confirmed duplicate with the vendor' });
		await expect(
			page
				.getByTestId('toast')
				.filter({ hasText: en['exceptions.erpVoid.done'].replace('{invoice}', 'E2E-VOID-1') })
		).toBeVisible();
		// The flag stays open, re-read: the void is no longer on offer.
		await expect(modal).toBeVisible();
		await expect(modal.getByTestId('erp-void-open')).toBeDisabled();
		await expect(modal.getByTestId('erp-void-refusal')).toBeVisible();
		expect(reads()).toBeGreaterThan(0);
		await expect(page).toHaveURL(new RegExp(`[?&]id=${ID}`));
	});

	test('a deep link to the flag opens its dialog with the void panel first', async ({ page }) => {
		await stubQueue(page, null);
		await stubDetail(page, () => null);
		await page.goto(`/exceptions?id=${ID}`);
		const modal = resolveDialog(page);
		await expect(modal.getByTestId('erp-void-panel')).toBeVisible();
		// The note sits below the panel; first focus must not skip past it.
		await expect
			.poll(() =>
				page.evaluate(() => {
					const active = document.activeElement;
					const panel = document.querySelector('[data-testid="erp-void-panel"]');
					if (!active || !panel || active === document.body) return false;
					return (
						panel.contains(active) ||
						(active.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0
					);
				})
			)
			.toBe(true);
	});

	test('Cancel and Escape close only the confirm and return focus to the trigger', async ({
		page
	}) => {
		await stubQueue(page, null);
		const modal = await openResolve(page);
		const trigger = modal.getByTestId('erp-void-open');
		const confirm = confirmDialog(page);

		await trigger.click();
		await expect(confirm).toBeVisible();
		await confirm.getByRole('button', { name: en['exceptions.erpVoid.cancel'] }).click();
		await expect(confirm).toBeHidden();
		await expect(modal).toBeVisible();
		await expect(trigger).toBeFocused();

		await trigger.click();
		await expect(confirm.getByTestId('erp-void-reason')).toBeFocused();
		await page.keyboard.press('Escape');
		await expect(confirm).toBeHidden();
		await expect(modal).toBeVisible();
		await expect(trigger).toBeFocused();
	});

	test('the panel and the stacked confirm pass axe', async ({ page }) => {
		await stubQueue(page, null);
		const modal = await openResolve(page);
		await expect(modal.getByTestId('erp-void-panel')).toBeVisible();
		await expectNoA11yViolations(page);
		await modal.getByTestId('erp-void-open').click();
		await expect(confirmDialog(page).getByTestId('erp-void-reason')).toBeFocused();
		await expectNoA11yViolations(page);
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
		const confirm = confirmDialog(page);
		await confirm.getByTestId('erp-void-reason').fill('x');
		await confirm.getByTestId('erp-void-confirm').click();
		// Stated from the code in the reader's language, never the raw body.
		await expect(confirm.getByTestId('erp-void-error')).toHaveText(
			enRefusal['refusal.erpVoidBillPaid']
		);
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
