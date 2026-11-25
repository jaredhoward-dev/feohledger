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
 *   of the reader's own draft reports, and a line the server refuses (a
 *   colleague's) is refused whole, with the server's own sentence on screen.
 *
 * Real stack, no stubs: the colleague's line is created by the worker's clerk
 * through the API; the page is read as the worker admin (the default storage
 * state), who owns the report.
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

	test('"Add to report" adds the selection, and a colleague\'s line is refused whole with the server\'s reason', async ({
		page,
		tenantClerk
	}) => {
		const tag = `E2E-Bulk-${Date.now()}`;
		const mine = `${tag} Mine`;
		const theirs = `${tag} Theirs`;
		const reportNumber = `E2E-BULK-${Date.now()}`;
		const ids: string[] = [];
		let reportId: string | null = null;
		try {
			await page.goto('/expenses');
			ids.push(await createExpense(page, await authedTenantHeaders(page), mine));
			ids.push(await createExpense(page, await clerkHeaders(page, tenantClerk), theirs));
			reportId = await createReport(page, reportNumber);

			await page.goto(`/expenses?search=${encodeURIComponent(tag)}`);

			// The colleague's line in the selection: the server refuses the whole
			// attach, and its sentence is what the reader sees.
			await page.getByRole('checkbox', { name: `Select expense ${mine}` }).check();
			await page.getByRole('checkbox', { name: `Select expense ${theirs}` }).check();
			const select = page.getByTestId('expense-bulk-report-select');
			await expect(select).toBeEnabled();
			await select.selectOption(reportId);
			const refused = page.waitForResponse(isAttachPost(reportId));
			await page.getByTestId('expense-bulk-add-submit').click();
			expect((await refused).status()).toBe(403);
			await expect(
				page.getByText('This expense was created by someone else. Only its creator can put it on a report.')
			).toBeVisible();
			expect(reportIdOf(ids[0])).toBe('');

			// Just the reader's own line: added.
			await page.getByRole('checkbox', { name: `Select expense ${theirs}` }).uncheck();
			await select.selectOption(reportId);
			const added = page.waitForResponse(isAttachPost(reportId));
			await page.getByTestId('expense-bulk-add-submit').click();
			const res = await added;
			expect(res.status()).toBe(200);
			expect(res.request().postDataJSON()).toEqual({ expense_ids: [ids[0]], detach: false });
			await expect(page.getByText(`1 added to ${reportNumber}`)).toBeVisible();
			expect(reportIdOf(ids[0])).toBe(reportId);
			expect(reportIdOf(ids[1])).toBe('');
		} finally {
			cleanup(reportId, ids);
		}
	});
});
