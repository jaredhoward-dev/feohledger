import {
	API_BASE,
	authedTenantHeaders,
	expect,
	signInAndWait,
	tenantPsql,
	test
} from '../fixtures/helpers';
import { expectNoA11yViolations } from '../a11y/axe-helper';

// An approved pre-approval is a budget the lines it covers draw down
// (decisions §563). One 500 USD estimate covers a 400 USD line; a second
// 400 USD line on another report is refused `preapproval_required`, and the
// Pre-approvals tab shows what is left. A second, 400 USD estimate then covers
// that line exactly and reads "Used up"; a pending and a rejected request
// cover nothing and show no balance ("—").

const DESKTOP = { width: 1280, height: 800 };
const PHONE = { width: 320, height: 800 };

/** One axe pass at desktop and one at phone width (SC 1.4.10 reflow), leaving
 *  the page at desktop width. */
async function expectAccessibleAtBothWidths(page: import('@playwright/test').Page) {
	await expectNoA11yViolations(page);
	await page.setViewportSize(PHONE);
	await expectNoA11yViolations(page);
	await page.setViewportSize(DESKTOP);
}

interface Created {
	id: string;
}

function cleanup(ids: { reports: string[]; expenses: string[]; pas: string[]; policy: string | null }) {
	for (const id of ids.reports) {
		tenantPsql(`UPDATE expenses SET report_id=NULL WHERE report_id='${id}'`);
		tenantPsql(`DELETE FROM expense_reports WHERE id='${id}'`);
	}
	for (const id of ids.expenses) tenantPsql(`DELETE FROM expenses WHERE id='${id}'`);
	for (const id of ids.pas) tenantPsql(`DELETE FROM expense_preapprovals WHERE id='${id}'`);
	if (ids.policy) tenantPsql(`DELETE FROM expense_policies WHERE id='${ids.policy}'`);
}

test.describe('/expenses — pre-approval draw-down', () => {
	test('one estimate covers lines only up to its amount', async ({
		page,
		tenantAdmin,
		tenantClerk
	}) => {
		const stamp = Date.now();
		const category = `e2e-draw-${stamp}`;
		const title = `E2E Draw ${stamp}`;
		const ids = {
			reports: [] as string[],
			expenses: [] as string[],
			pas: [] as string[],
			policy: null as string | null
		};
		try {
			await signInAndWait(page, tenantAdmin);
			const adminH = await authedTenantHeaders(page);
			const pol = await page.request.post(`${API_BASE}/api/expense-policies`, {
				headers: adminH,
				data: { name: `Draw ${stamp}`, category, requires_preapproval_above: '100.00' }
			});
			expect(pol.ok()).toBe(true);
			ids.policy = ((await pol.json()) as Created).id;

			await signInAndWait(page, tenantClerk);
			const clerkH = await authedTenantHeaders(page);
			const pa = await page.request.post(`${API_BASE}/api/expense-preapprovals`, {
				headers: clerkH,
				data: { title, estimated_amount: '500.00', currency: 'USD', category }
			});
			expect(pa.status()).toBe(201);
			const paId = ((await pa.json()) as Created).id;
			ids.pas.push(paId);

			await signInAndWait(page, tenantAdmin);
			const approved = await page.request.post(
				`${API_BASE}/api/expense-preapprovals/${paId}/approve`,
				{ headers: await authedTenantHeaders(page), data: {} }
			);
			expect(approved.status()).toBe(200);

			// Two reports of one 400 USD line each, built and submitted by the clerk.
			await signInAndWait(page, tenantClerk);
			const submits: number[] = [];
			for (const n of [1, 2]) {
				const exp = await page.request.post(`${API_BASE}/api/expenses`, {
					headers: clerkH,
					data: { amount: '400.00', currency: 'USD', expense_date: '2026-06-01', category }
				});
				expect(exp.status()).toBe(201);
				const expenseId = ((await exp.json()) as Created).id;
				ids.expenses.push(expenseId);
				const rpt = await page.request.post(`${API_BASE}/api/expense-reports`, {
					headers: clerkH,
					data: { report_number: `E2E-DRAW-${stamp}-${n}`, currency: 'USD' }
				});
				expect(rpt.status()).toBe(201);
				const reportId = ((await rpt.json()) as Created).id;
				ids.reports.push(reportId);
				const attach = await page.request.post(
					`${API_BASE}/api/expense-reports/${reportId}/expenses`,
					{ headers: clerkH, data: { expense_ids: [expenseId], detach: false } }
				);
				expect(attach.status()).toBe(200);
				const submit = await page.request.post(
					`${API_BASE}/api/expense-reports/${reportId}/submit`,
					{ headers: clerkH, data: {} }
				);
				submits.push(submit.status());
			}
			// The first draws 400 of 500; the second no longer fits.
			expect(submits).toEqual([200, 422]);

			// Three more requests: one approved for exactly 400 (the refused line
			// then draws all of it), one left pending, one rejected.
			const usedUpTitle = `${title} used-up`;
			const pendingTitle = `${title} pending`;
			const rejectedTitle = `${title} rejected`;
			const more: Record<string, string> = {};
			for (const [t, amount] of [
				[usedUpTitle, '400.00'],
				[pendingTitle, '300.00'],
				[rejectedTitle, '200.00']
			] as const) {
				const res = await page.request.post(`${API_BASE}/api/expense-preapprovals`, {
					headers: clerkH,
					data: { title: t, estimated_amount: amount, currency: 'USD', category }
				});
				expect(res.status()).toBe(201);
				more[t] = ((await res.json()) as Created).id;
				ids.pas.push(more[t]);
			}
			await signInAndWait(page, tenantAdmin);
			const adminH2 = await authedTenantHeaders(page);
			for (const [t, verb] of [
				[usedUpTitle, 'approve'],
				[rejectedTitle, 'reject']
			] as const) {
				const res = await page.request.post(
					`${API_BASE}/api/expense-preapprovals/${more[t]}/${verb}`,
					{ headers: adminH2, data: {} }
				);
				expect(res.status()).toBe(200);
			}

			// The refused report now fits the 400 estimate, and spends all of it.
			await signInAndWait(page, tenantClerk);
			const resubmit = await page.request.post(
				`${API_BASE}/api/expense-reports/${ids.reports[1]}/submit`,
				{ headers: await authedTenantHeaders(page), data: {} }
			);
			expect(resubmit.status()).toBe(200);

			await page.goto('/expenses?tab=preapprovals');
			// Matched on the exact title cell: every title here starts with `title`.
			const rowTitled = (t: string) =>
				page.getByRole('row').filter({ has: page.getByRole('cell', { name: t, exact: true }) });
			const row = rowTitled(title);
			await expect(row.getByTestId('preapproval-remaining')).toContainText('$100.00');
			await expect(row.getByTestId('preapproval-remaining')).toContainText('$400.00');
			const usedUp = rowTitled(usedUpTitle);
			await expect(usedUp.getByTestId('preapproval-remaining')).toContainText('$0.00');
			await expect(usedUp.getByTestId('preapproval-remaining')).toContainText('Used up');
			for (const t of [pendingTitle, rejectedTitle]) {
				const r = rowTitled(t);
				await expect(r.getByTestId('preapproval-remaining')).toHaveText('—');
			}
			await expectAccessibleAtBothWidths(page);

			// Each covered line says what it draws, as text, in its own currency.
			await page.goto('/expenses');
			const draws = page
				.getByRole('row')
				.filter({ hasText: category })
				.getByTestId('expense-preapproval-draw');
			await expect(draws.first()).toBeVisible();
			await expect(draws.filter({ hasText: '$400.00' })).toHaveCount(2);
			await expectAccessibleAtBothWidths(page);
		} finally {
			cleanup(ids);
		}
	});
});
