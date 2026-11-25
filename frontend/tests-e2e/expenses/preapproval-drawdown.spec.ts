import {
	API_BASE,
	authedTenantHeaders,
	expect,
	signInAndWait,
	tenantPsql,
	test
} from '../fixtures/helpers';

// An approved pre-approval is a budget the lines it covers draw down
// (decisions §563). One 500 USD estimate covers a 400 USD line; a second
// 400 USD line on another report is refused `preapproval_required`, and the
// Pre-approvals tab shows what is left.

interface Created {
	id: string;
}

function cleanup(ids: { reports: string[]; expenses: string[]; pa: string | null; policy: string | null }) {
	for (const id of ids.reports) {
		tenantPsql(`UPDATE expenses SET report_id=NULL WHERE report_id='${id}'`);
		tenantPsql(`DELETE FROM expense_reports WHERE id='${id}'`);
	}
	for (const id of ids.expenses) tenantPsql(`DELETE FROM expenses WHERE id='${id}'`);
	if (ids.pa) tenantPsql(`DELETE FROM expense_preapprovals WHERE id='${ids.pa}'`);
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
			pa: null as string | null,
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
			ids.pa = ((await pa.json()) as Created).id;

			await signInAndWait(page, tenantAdmin);
			const approved = await page.request.post(
				`${API_BASE}/api/expense-preapprovals/${ids.pa}/approve`,
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

			await page.goto('/expenses?tab=preapprovals');
			const row = page.getByRole('row').filter({ hasText: title });
			await expect(row.getByTestId('preapproval-remaining')).toContainText('$100.00');
			await expect(row.getByTestId('preapproval-remaining')).toContainText('$400.00');

			// The covered line says what it draws.
			await page.goto('/expenses');
			await expect(page.getByTestId('expense-preapproval-draw').first()).toBeVisible();
		} finally {
			cleanup(ids);
		}
	});
});
