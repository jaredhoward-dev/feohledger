import type { Page } from '@playwright/test';

import { en } from '#lib/i18n/locales/en.ts';
import {
	API_BASE,
	authedTenantHeaders,
	deleteInvoicesWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';
import { expectNoA11yViolations } from './axe-helper';

/**
 * axe at WCAG 2.2 AA, at a desktop width and at the 320px phone width WCAG
 * 1.4.10 names, over the invoice modal's shared-key bill confirmation
 * (`erp/ErpBillConfirm.svelte` over `erp/ErpBillLookup.svelte`, decisions
 * §587): the panel with a found bill in its live region, then the stacked
 * confirm dialog with its required reason. The verdict and lookup are routed
 * as `invoices/erp-bill-confirm.spec.ts` routes them: a Xero connection is not
 * part of the e2e tenant.
 */

const WIDTHS = [
	{ name: 'desktop', width: 1280, height: 800 },
	{ name: 'phone (320px)', width: 320, height: 720 }
] as const;

const PREFIX = 'E2E-A11Y-BILL-CONFIRM';
const BILL = {
	erp_document_id: 'xero-bill-77',
	erp_document_number: 'SUP-77',
	amount: '100.00',
	currency: 'USD',
	erp_status: 'open'
};

async function sharedKeyInvoice(page: Page, number: string): Promise<string> {
	const res = await page.request.post(`${API_BASE}/api/invoices`, {
		headers: await authedTenantHeaders(page),
		data: { vendor: 'E2E A11y Confirm Vendor', invoice_number: number, amount: '100.00', currency: 'USD' }
	});
	expect(res.status()).toBe(201);
	const id = ((await res.json()) as { id: string }).id;
	tenantPsql(
		`UPDATE invoices SET status='failed', approved_by='Approver', approval_date=CURRENT_DATE WHERE id='${id}';` +
			` UPDATE workflow_instances SET state_data = COALESCE(state_data, '{}'::jsonb) || '{"erp_last_refusal_code": "erp_bill_key_shared"}'::jsonb WHERE invoice_id='${id}';` +
			// The row a refused push writes: the invoice's ERP section shows a
			// failed push from it, so the panel inside it renders.
			` INSERT INTO audit_log (id, correlation_id, organization_id, action, entity_type, entity_id, details, created_at) SELECT gen_random_uuid(), correlation_id, organization_id, 'invoice.erp_failed', 'invoice', id, '{"error": "xero: another invoice shares this supplier and number", "refusal": {"code": "erp_bill_key_shared", "params": {"provider": "xero"}}}'::jsonb, now() FROM invoices WHERE id='${id}'`
	);
	await page.route(`**/api/invoices/${id}/review-actions`, async (route) => {
		const real = await route.fetch();
		const body = (await real.json()) as Record<string, unknown>;
		await route.fulfill({ response: real, json: { ...body, erp_bill_confirm_refusal: null } });
	});
	await page.route(`**/api/invoices/${id}/erp-bill-confirmation`, (route) =>
		route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ invoice_id: id, found: true, bill: BILL })
		})
	);
	return id;
}

test.describe('accessibility — ERP bill confirmation (WCAG 2.2 AA)', () => {
	test.afterEach(() => deleteInvoicesWhere(`invoice_number LIKE '${PREFIX}-%'`));

	for (const { name, width, height } of WIDTHS) {
		test(`the panel with a found bill, and its confirm dialog, have no axe violations at ${name}`, async ({
			page
		}) => {
			await page.setViewportSize({ width, height });
			const id = await sharedKeyInvoice(page, `${PREFIX}-${width}-${Date.now()}`);
			const verdict = page.waitForResponse(
				(r) => r.url().endsWith(`/api/invoices/${id}/review-actions`) && r.status() === 200
			);
			await page.goto(`/invoices?id=${id}`);
			await verdict;

			const panel = page.getByTestId('invoice-erp-bill-confirm');
			await panel.getByTestId('invoice-erp-bill-confirm-find').click();
			await expect(panel.getByTestId('invoice-erp-bill-confirm-found')).toContainText('SUP-77');
			await expectNoA11yViolations(page);

			await panel.getByTestId('invoice-erp-bill-confirm-open').click();
			const dialog = page.getByRole('dialog', {
				name: en['invoices.modal.erpBillConfirm.confirmTitle']
			});
			await expect(dialog.getByTestId('invoice-erp-bill-confirm-reason')).toBeFocused();
			await expectNoA11yViolations(page);
		});
	}
});
