import { expect, test } from '../fixtures/helpers';
import type { Page, Route } from '@playwright/test';

/**
 * `/contracts` — the detail modal's lifecycle buttons and the bulk "Change
 * status" read each row's server verdict (`actions`, backend
 * `services/contract_controls`, decisions §507), never a restated status
 * rule. The modal's old copy hid Activate / Terminate on an expired contract
 * and Create PO on a draft, all of which the server accepts; the bulk bar
 * offered all three actions whatever was selected.
 *
 * Every contract refusal is a status or role one, so a refused action is
 * hidden (frontend/docs/ui-patterns.md § Whether to offer an action). Stubbed,
 * exact-pathname guarded (`**\/api/contracts**` also matches Vite's module URL
 * for `/src/lib/api/contracts.ts`).
 */

const LIST = '/api/contracts';

const ALLOW_ALL = {
	activate: null,
	terminate: null,
	cancel: null,
	renew: null,
	create_po: null,
	delete: null
};

function refused(code: string, params: Record<string, unknown> = {}) {
	return { code, message: `Refused: ${code}`, params };
}

const STATUS_REFUSED = refused('contract_status_refused', { status: 'terminated' });
const REFUSE_ALL = Object.fromEntries(Object.keys(ALLOW_ALL).map((k) => [k, STATUS_REFUSED]));

function contract(id: string, number: string, overrides: Record<string, unknown> = {}) {
	return {
		id,
		contract_number: number,
		title: `Verdict fixture ${number}`,
		description: null,
		contract_type: 'service',
		status: 'expired',
		vendor_id: '22222222-2222-4222-8222-222222222222',
		vendor_name: 'Verdict Vendor',
		currency: 'USD',
		total_value: '1000.00',
		spend_limit: null,
		not_to_exceed: false,
		start_date: null,
		end_date: '2026-01-01',
		signed_date: null,
		auto_renew: false,
		renewal_term_months: null,
		renewal_notice_days: 30,
		renewal_alert_sent_at: null,
		payment_terms: null,
		owner_user_id: null,
		file_url: null,
		file_key: null,
		terms: null,
		line_items: [],
		spend: null,
		created_at: '2026-11-01T10:00:00+00:00',
		updated_at: '2026-11-01T10:00:00+00:00',
		...overrides
	};
}

function json(route: Route, body: unknown) {
	return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
}

async function stubContracts(page: Page, items: Array<{ id: string }>, total = items.length) {
	await page.route('**/api/contracts**', async (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname;
		if (req.method() === 'GET' && path === LIST) {
			return json(route, { items, total, page: 1, page_size: 100 });
		}
		const one = items.find((c) => path === `${LIST}/${c.id}`);
		if (req.method() === 'GET' && one) return json(route, one);
		return route.continue();
	});
}

const LIFECYCLE = [/^Activate$/, /^Terminate$/, /^Cancel Contract$/, /^Renew$/, /^Create PO$/];

async function openModal(page: Page, number: string) {
	await page.goto('/contracts');
	await page.getByRole('button', { name: `Open contract ${number}` }).click();
	const dialog = page.getByRole('dialog');
	await expect(dialog).toBeVisible({ timeout: 15_000 });
	return dialog;
}

test.describe('/contracts — lifecycle controls follow the server verdict', () => {
	test('an allowed verdict offers each button, even on a status the old page rule refused', async ({
		page
	}) => {
		// `expired`: the old modal showed only Renew here.
		await stubContracts(page, [
			contract('c0000000-0000-4000-8000-000000000001', 'CT-V-0001', { actions: ALLOW_ALL })
		]);
		const dialog = await openModal(page, 'CT-V-0001');
		for (const name of LIFECYCLE) {
			await expect(dialog.getByRole('button', { name })).toBeVisible();
		}
	});

	test('a refused verdict hides its button and leaves the rest', async ({ page }) => {
		await stubContracts(page, [
			contract('c0000000-0000-4000-8000-000000000002', 'CT-V-0002', {
				status: 'draft',
				actions: { ...REFUSE_ALL, activate: null, create_po: null }
			})
		]);
		const dialog = await openModal(page, 'CT-V-0002');
		await expect(dialog.getByRole('button', { name: /^Activate$/ })).toBeVisible();
		await expect(dialog.getByRole('button', { name: /^Create PO$/ })).toBeVisible();
		for (const name of [/^Terminate$/, /^Cancel Contract$/, /^Renew$/]) {
			await expect(dialog.getByRole('button', { name })).toHaveCount(0);
		}
	});

	test('a contract whose verdict has not arrived offers no lifecycle button (fail closed)', async ({
		page
	}) => {
		await stubContracts(page, [
			contract('c0000000-0000-4000-8000-000000000003', 'CT-V-0003', { status: 'active' })
		]);
		const dialog = await openModal(page, 'CT-V-0003');
		for (const name of LIFECYCLE) {
			await expect(dialog.getByRole('button', { name })).toHaveCount(0);
		}
	});

	test('every verdict refused renders no lifecycle block at all (no empty divider)', async ({
		page
	}) => {
		await stubContracts(page, [
			contract('c0000000-0000-4000-8000-000000000004', 'CT-V-0004', {
				status: 'terminated',
				actions: REFUSE_ALL
			})
		]);
		const dialog = await openModal(page, 'CT-V-0004');
		await expect(dialog.getByTestId('contract-lifecycle-actions')).toHaveCount(0);
		// The footer still renders: only the empty action row is gone.
		await expect(dialog.locator('.modal-footer')).toBeVisible();
	});
});

test.describe('/contracts — bulk Change status follows the selected rows’ verdicts', () => {
	const A = 'c0000000-0000-4000-8000-000000000011';
	const B = 'c0000000-0000-4000-8000-000000000012';

	test.beforeEach(async ({ page }) => {
		await stubContracts(page, [
			contract(A, 'CT-V-0011', {
				status: 'draft',
				actions: { ...REFUSE_ALL, activate: null, cancel: null }
			}),
			contract(B, 'CT-V-0012', { status: 'terminated', actions: REFUSE_ALL })
		]);
		await page.goto('/contracts');
		await expect(page.locator('tbody tr', { hasText: 'CT-V-0011' })).toBeVisible({
			timeout: 15_000
		});
	});

	function select(page: Page, number: string) {
		return page.locator('tbody tr', { hasText: number }).getByRole('checkbox').check();
	}

	test('offers only the actions some selected row allows, and counts the skips', async ({
		page
	}) => {
		// BulkBar's one live region is mounted, empty, before any selection,
		// so the first selection's text lands in an existing region (4.1.3).
		const status = page.getByTestId('bulk-bar-status');
		await expect(status).toHaveAttribute('role', 'status');
		await expect(status).toHaveText('');
		await select(page, 'CT-V-0011');
		await expect(status).toHaveText('1 selected');
		await select(page, 'CT-V-0012');
		// The skip note is spoken by that region, after the count — and only
		// there: the visible copy in the bar carries no live role.
		await expect(status).toHaveText(
			'2 selected 1 selected contract can’t take this action and will be skipped.'
		);
		await expect(page.locator('[role="status"]', { hasText: 'will be skipped' })).toHaveCount(1);

		const action = page.getByTestId('contracts-bulk-action');
		await expect(action.locator('option')).toHaveText(['Activate', 'Cancel Contract']);
		const note = page.getByTestId('contracts-bulk-skip-note');
		await expect(note).toHaveText('1 selected contract can’t take this action and will be skipped.');
		await expect(action).toHaveAttribute('aria-describedby', 'contracts-bulk-skip-note');
	});

	test('a selection no action fits says so instead of offering one', async ({ page }) => {
		await select(page, 'CT-V-0012');
		await expect(page.getByTestId('contracts-bulk-action')).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Change Status' })).toHaveCount(0);
		await expect(page.getByTestId('contracts-bulk-no-action')).toBeVisible();
	});
});

test.describe('/contracts — bulk skip count after "select all N matching"', () => {
	const A = 'c0000000-0000-4000-8000-000000000021';
	const B = 'c0000000-0000-4000-8000-000000000022';
	const C = 'c0000000-0000-4000-8000-000000000023';
	const D = 'c0000000-0000-4000-8000-000000000024';

	test('ids beyond the loaded rows make the skip count a minimum', async ({ page }) => {
		// Two rows loaded of four matching: C and D's verdicts are unknown.
		await stubContracts(
			page,
			[
				contract(A, 'CT-V-0021', {
					status: 'draft',
					actions: { ...REFUSE_ALL, activate: null }
				}),
				contract(B, 'CT-V-0022', { status: 'terminated', actions: REFUSE_ALL })
			],
			4
		);
		// Registered after the stub, so it answers `/ids` first.
		await page.route('**/api/contracts/ids*', (route) =>
			json(route, { ids: [A, B, C, D], total: 4, truncated: false })
		);
		await page.goto('/contracts');
		await expect(page.locator('tbody tr', { hasText: 'CT-V-0021' })).toBeVisible();

		await page.getByRole('checkbox', { name: 'Select all contracts on this page' }).check();
		const note = page.getByTestId('contracts-bulk-skip-note');
		await expect(note).toHaveText('1 selected contract can’t take this action and will be skipped.');
		await page.getByRole('button', { name: 'Select all 4 matching' }).click();
		await expect(note).toHaveText(
			'At least 1 selected contract can’t take this action and will be skipped.'
		);
		// The described-by target survives the switch — it is never re-created.
		await expect(page.getByTestId('contracts-bulk-action')).toHaveAttribute(
			'aria-describedby',
			'contracts-bulk-skip-note'
		);
	});
});
