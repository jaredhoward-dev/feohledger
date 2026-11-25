import type { Page } from '@playwright/test';
import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	expect,
	tenantHeaders,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * Attaching expenses to a report without ever seeing an expense id
 * (decisions §515). The report's attach control used to be a free-text
 * "Expense ID to attach" box, and no screen shows an id.
 *
 * - The picker lists the reader's own loose draft lines
 *   (`GET /api/expenses?attachable=true`) and never a colleague's, and
 *   picking one and pressing Attach puts it on the report.
 * - "Add to report" on the expenses list's bulk bar adds the selection to one
 *   of the reader's own draft reports. Only the lines an attach would take
 *   are sent: a line already on another report is skipped, never moved, and
 *   a colleague's line is skipped rather than sent to be refused; the toast
 *   says how many were added and how many skipped.
 * - With no draft report to add to, the bar says so and links to the Reports
 *   tab.
 *
 * Real stack: the colleague's line is created by the worker's clerk through
 * the API; the page is read as the worker admin (the default storage state),
 * who owns the report. The one stub is the no-draft-reports case, which
 * answers the bar's own `mine=true&status=draft` read with an empty page:
 * the shared worker admin may own drafts other specs left, and deleting them
 * would race those specs.
 */

interface Created {
	id: string;
}

async function clerkHeaders(
	page: Page,
	creds: { email: string; password: string }
): Promise<Record<string, string>> {
	const slug = currentTenantSlug();
	const res = await page.request.post(`${API_BASE}/api/auth/login`, {
		headers: { 'X-Tenant-Slug': slug },
		data: { email: creds.email, password: creds.password }
	});
	expect(res.status()).toBe(200);
	const body = (await res.json()) as { access_token?: string; token?: string };
	const token = body.access_token ?? body.token;
	expect(token).toBeTruthy();
	return tenantHeaders(token as string, slug);
}

async function createExpense(
	page: Page,
	headers: Record<string, string>,
	merchant: string
): Promise<string> {
	const res = await page.request.post(`${API_BASE}/api/expenses`, {
		headers,
		data: { merchant, amount: '42.50', currency: 'USD', expense_date: '2026-03-01', category: 'travel' }
	});
	expect(res.status()).toBe(201);
	return ((await res.json()) as Created).id;
}

async function createReport(page: Page, reportNumber: string): Promise<string> {
	const res = await page.request.post(`${API_BASE}/api/expense-reports`, {
		headers: await authedTenantHeaders(page),
		data: { report_number: reportNumber, title: 'E2E attach picker', currency: 'USD' }
	});
	expect(res.status()).toBe(201);
	return ((await res.json()) as Created).id;
}

function reportIdOf(expenseId: string): string {
	return tenantPsql(`SELECT coalesce(report_id::text, '') FROM expenses WHERE id='${expenseId}'`).trim();
}

function cleanup(reportId: string | null, expenseIds: string[]): void {
	if (reportId) tenantPsql(`UPDATE expenses SET report_id=NULL WHERE report_id='${reportId}'`);
	for (const id of expenseIds) tenantPsql(`DELETE FROM expenses WHERE id='${id}'`);
	if (reportId) tenantPsql(`DELETE FROM expense_reports WHERE id='${reportId}'`);
}

function isAttachPost(reportId: string) {
	return (r: import('@playwright/test').Response) =>
		r.request().method() === 'POST' &&
		new URL(r.url()).pathname === `/api/expense-reports/${reportId}/expenses`;
}

test.describe('/expenses — attach by picking, not by id (§515)', () => {
	test("the report's picker offers only the reader's own loose lines and attaches the one picked", async ({
		page,
		tenantClerk
	}) => {
		const tag = `E2E-Pick-${Date.now()}`;
		const mine = `${tag} Mine`;
		const theirs = `${tag} Theirs`;
		const reportNumber = `E2E-PICK-${Date.now()}`;
		const ids: string[] = [];
		let reportId: string | null = null;
		try {
			await page.goto('/expenses');
			ids.push(await createExpense(page, await authedTenantHeaders(page), mine));
			ids.push(await createExpense(page, await clerkHeaders(page, tenantClerk), theirs));
			reportId = await createReport(page, reportNumber);

			await page.goto('/expenses?tab=reports');
			await page.getByRole('button', { name: `Open report ${reportNumber}` }).click();

			// No free-text id box any more: a labelled combobox.
			const picker = page.getByRole('combobox', { name: 'Expense to attach' });
			await expect(picker).toBeVisible();
			await picker.fill(tag);

			const ownOption = page.getByRole('option', { name: new RegExp(mine) });
			await expect(ownOption).toBeVisible();
			// The answer for this term has landed (the own line is in it), so the
			// colleague's absence is the server's set, not a pending fetch.
			await expect(page.getByRole('option', { name: new RegExp(theirs) })).toHaveCount(0);

			await ownOption.click();
			const attached = page.waitForResponse(isAttachPost(reportId));
			await page.getByTestId('expense-attach-submit').click();
			const res = await attached;
			expect(res.status()).toBe(200);
			expect(res.request().postDataJSON()).toEqual({ expense_ids: [ids[0]], detach: false });

			await expect(page.locator('table').getByText(mine)).toBeVisible();
			expect(reportIdOf(ids[0])).toBe(reportId);
			expect(reportIdOf(ids[1])).toBe('');
		} finally {
			cleanup(reportId, ids);
		}
	});

	test('"Add to report" sends only attachable lines and reports the rest as skipped', async ({
		page,
		tenantClerk
	}) => {
		const tag = `E2E-Bulk-${Date.now()}`;
		const mine = `${tag} Mine`;
		const theirs = `${tag} Theirs`;
		const placed = `${tag} Placed`;
		const reportNumber = `E2E-BULK-${Date.now()}`;
		const otherNumber = `E2E-BULK-OTHER-${Date.now()}`;
		const ids: string[] = [];
		let reportId: string | null = null;
		let otherId: string | null = null;
		try {
			await page.goto('/expenses');
			const own = await authedTenantHeaders(page);
			ids.push(await createExpense(page, own, mine));
			ids.push(await createExpense(page, await clerkHeaders(page, tenantClerk), theirs));
			ids.push(await createExpense(page, own, placed));
			reportId = await createReport(page, reportNumber);
			// The reader's own line, already on their OTHER draft report: the
			// endpoint would move it; the bulk add must leave it where it is.
			otherId = await createReport(page, otherNumber);
			const onOther = await page.request.post(`${API_BASE}/api/expense-reports/${otherId}/expenses`, {
				headers: own,
				data: { expense_ids: [ids[2]], detach: false }
			});
			expect(onOther.status()).toBe(200);

			await page.goto(`/expenses?search=${encodeURIComponent(tag)}`);

			// Only lines an attach would refuse or move: nothing to send, and the
			// bar says why instead of offering the button.
			await page.getByRole('checkbox', { name: `Select expense ${theirs}` }).check();
			await page.getByRole('checkbox', { name: `Select expense ${placed}` }).check();
			await expect(page.getByTestId('expense-bulk-none-attachable')).toHaveText(
				"None of the selected lines can be added: each is already on a report, not a draft, or someone else's."
			);
			await expect(page.getByTestId('expense-bulk-add-submit')).toHaveCount(0);

			// Add the reader's own loose line: one is sent, two are skipped.
			await page.getByRole('checkbox', { name: `Select expense ${mine}` }).check();
			const submit = page.getByTestId('expense-bulk-add-submit');
			await expect(submit).toHaveText('Add 1 to report');
			await expect(page.getByTestId('expense-bulk-skip-note')).toHaveText(
				'2 will be skipped (already on a report, not a draft, or not yours)'
			);
			const select = page.getByTestId('expense-bulk-report-select');
			await expect(select).toBeEnabled();
			await select.selectOption(reportId);
			const added = page.waitForResponse(isAttachPost(reportId));
			await submit.click();
			const res = await added;
			expect(res.status()).toBe(200);
			expect(res.request().postDataJSON()).toEqual({ expense_ids: [ids[0]], detach: false });
			await expect(
				page.getByText(
					`1 of 3 added to ${reportNumber}; 2 skipped (already on a report, not a draft, or not yours)`
				)
			).toBeVisible();
			expect(reportIdOf(ids[0])).toBe(reportId);
			expect(reportIdOf(ids[1])).toBe('');
			// Skipped, not moved.
			expect(reportIdOf(ids[2])).toBe(otherId);
		} finally {
			cleanup(otherId, []);
			cleanup(reportId, ids);
		}
	});

	test('with no draft report the bar says so and links to the Reports tab', async ({ page }) => {
		const tag = `E2E-NoDraft-${Date.now()}`;
		const ids: string[] = [];
		try {
			await page.goto('/expenses');
			ids.push(await createExpense(page, await authedTenantHeaders(page), `${tag} Mine`));
			// The bar's own read of the reader's draft reports, answered empty.
			await page.route(
				(url) =>
					url.pathname === '/api/expense-reports' &&
					url.searchParams.get('mine') === 'true' &&
					url.searchParams.get('status') === 'draft',
				(route) =>
					route.fulfill({
						status: 200,
						contentType: 'application/json',
						body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 100 })
					})
			);

			await page.goto(`/expenses?search=${encodeURIComponent(tag)}`);
			await page.getByRole('checkbox', { name: `Select expense ${tag} Mine` }).check();

			const empty = page.getByTestId('expense-bulk-no-draft-reports');
			await expect(empty).toHaveText('You have no draft reports. Create one on the Reports tab.');
			await expect(page.getByTestId('expense-bulk-report-select')).toHaveCount(0);

			await empty.getByRole('link', { name: 'Create one on the Reports tab' }).click();
			await expect(page).toHaveURL(/[?&]tab=reports(&|$)/);
			await expect(page.getByRole('tab', { name: 'Reports' })).toHaveAttribute('aria-selected', 'true');
		} finally {
			cleanup(null, ids);
		}
	});
});
