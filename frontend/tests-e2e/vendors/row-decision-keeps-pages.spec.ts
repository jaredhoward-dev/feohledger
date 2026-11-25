import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';

/**
 * A single-row Verify on /vendors keeps the reviewer's loaded pages
 * (decisions §506). The page used to refetch page 1 after each row decision,
 * so a reviewer working down pages added by Load more was thrown back to the
 * first 20 rows. It now re-reads only that row through `GET /api/vendors?id=`
 * under the page's own filters: the row is patched in place with the server's
 * fresh `status_actions` verdict, or dropped (and the total falls by one) when
 * it has left the active filter.
 *
 * The list, counts and verify endpoints are stubbed over one in-memory vendor
 * set, so the spec needs no seeded rows and shares nothing with other workers.
 * The stub's verdicts stand in for the server's; the page decides none.
 */

type MockVendor = { id: string; name: string; code: string; status: string };

const NAME = (i: number) => `RowKeep Vendor ${String(i).padStart(3, '0')}`;

function seed(n: number, status: string): MockVendor[] {
	return Array.from({ length: n }, (_, i) => ({
		id: `00000000-0000-4000-8000-${String(i + 1).padStart(12, '0')}`,
		name: NAME(i + 1),
		code: `RK${i + 1}`,
		status
	}));
}

const refused = (message: string) => ({ code: 'vendor_status_not_allowed', message });

function listRow(v: MockVendor) {
	return {
		...v,
		source: 'manual',
		invoice_count: 0,
		status_actions: {
			verify: v.status === 'unverified' ? null : refused('Vendor is not in unverified status'),
			reject: ['unverified', 'active'].includes(v.status)
				? null
				: refused('Vendor cannot be rejected from this status')
		}
	};
}

/** Stub the three endpoints over `vendors`; returns the list URLs requested. */
async function stubVendors(page: Page, vendors: MockVendor[]): Promise<URL[]> {
	const listReads: URL[] = [];
	await page.route('**/api/vendors**', async (route) => {
		const url = new URL(route.request().url());
		const method = route.request().method();
		if (url.pathname === '/api/vendors' && method === 'GET') {
			listReads.push(url);
			const status = url.searchParams.get('status');
			const id = url.searchParams.get('id');
			const size = Number(url.searchParams.get('page_size') ?? 20);
			const pageNo = Number(url.searchParams.get('page') ?? 1);
			const matching = vendors.filter(
				(v) => (!status || v.status === status) && (!id || v.id === id)
			);
			const items = matching.slice((pageNo - 1) * size, pageNo * size).map(listRow);
			return route.fulfill({
				json: { items, total: matching.length, page: pageNo, page_size: size }
			});
		}
		if (url.pathname === '/api/vendors/counts') {
			const by_status: Record<string, number> = {};
			for (const v of vendors) by_status[v.status] = (by_status[v.status] ?? 0) + 1;
			return route.fulfill({ json: { total: vendors.length, by_status } });
		}
		const verify = url.pathname.match(/^\/api\/vendors\/([^/]+)\/verify$/);
		if (verify && method === 'POST') {
			const v = vendors.find((x) => x.id === verify[1])!;
			v.status = 'active';
			return route.fulfill({ json: { ...v, source: 'manual', invoice_count: 0 } });
		}
		return route.fallback();
	});
	return listReads;
}

async function loadTwoPages(page: Page, path: string) {
	await page.goto(path);
	const loadMore = page.locator('.btn-load-more');
	await expect(loadMore).toContainText('20 of 45');
	const second = page.waitForResponse(
		(r) => new URL(r.url()).pathname === '/api/vendors' && r.url().includes('page=2')
	);
	await loadMore.click();
	await second;
	await expect(page.locator('table tbody tr')).toHaveCount(40);
}

test.describe('/vendors row decision keeps loaded pages', () => {
	test('verifying a page-2 row keeps both pages and shows its new next actions', async ({
		page
	}) => {
		const vendors = seed(45, 'unverified');
		const listReads = await stubVendors(page, vendors);
		await loadTwoPages(page, '/vendors');

		const target = vendors[29]; // on page 2
		const row = page.locator('table tbody tr', { hasText: target.name });
		await expect(row.getByRole('button', { name: 'Verify' })).toBeVisible();
		const readsBefore = listReads.length;

		const reread = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname === '/api/vendors' &&
				new URL(r.url()).searchParams.get('id') === target.id
		);
		await row.getByRole('button', { name: 'Verify' }).click();
		await reread;

		// Its next action is the server's verdict for `active`: Reject, not Verify.
		await expect(row.getByRole('button', { name: 'Reject' })).toBeVisible();
		await expect(row.getByRole('button', { name: 'Verify' })).toHaveCount(0);
		await expect(row).not.toHaveClass(/unverified/);
		// Both pages are still on screen, and the whole-set total is unchanged.
		await expect(page.locator('table tbody tr')).toHaveCount(40);
		await expect(page.locator('.btn-load-more')).toContainText('40 of 45');
		// The only list read after the decision was the one-row re-read.
		expect(listReads.slice(readsBefore).map((u) => u.searchParams.get('id'))).toEqual([
			target.id
		]);
		// The Unverified chip re-asked the server's tallies (45 → 44).
		await expect(page.locator('.filter-chip', { hasText: 'Unverified' })).toContainText('44');
	});

	test('a row that leaves the Unverified filter disappears and the total drops', async ({
		page
	}) => {
		const vendors = seed(45, 'unverified');
		await stubVendors(page, vendors);
		await loadTwoPages(page, '/vendors?status=unverified');

		const target = vendors[24]; // on page 2
		const row = page.locator('table tbody tr', { hasText: target.name });
		await row.getByRole('button', { name: 'Verify' }).click();

		await expect(row).toHaveCount(0);
		// The other 39 loaded rows stay, page 2's neighbours included.
		await expect(page.locator('table tbody tr')).toHaveCount(39);
		await expect(page.locator('table tbody tr', { hasText: vendors[39].name })).toBeVisible();
		await expect(page.locator('.btn-load-more')).toContainText('39 of 44');
		await expect(page.locator('.filter-chip', { hasText: 'Unverified' })).toContainText('44');
	});
});
