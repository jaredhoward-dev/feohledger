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

/** Per-request hooks for the list read: `hold` returns a promise the stub
 *  awaits before answering (so a spec decides the order responses land in,
 *  with no sleeps); `fail` answers a 500 instead. */
type ListHooks = {
	hold?: (url: URL) => Promise<void> | undefined;
	fail?: (url: URL) => boolean;
	/** Held before the Verify POST answers, so a spec can act mid-decision. */
	holdDecision?: () => Promise<void> | undefined;
};

/** A promise a spec resolves by hand: `release()` lets the held response go. */
function gate() {
	let release!: () => void;
	const opened = new Promise<void>((resolve) => (release = resolve));
	return { opened, release };
}

/** Stub the three endpoints over `vendors`; returns the list URLs requested. */
async function stubVendors(
	page: Page,
	vendors: MockVendor[],
	hooks: ListHooks = {}
): Promise<URL[]> {
	const listReads: URL[] = [];
	await page.route('**/api/vendors**', async (route) => {
		const url = new URL(route.request().url());
		const method = route.request().method();
		if (url.pathname === '/api/vendors' && method === 'GET') {
			listReads.push(url);
			const held = hooks.hold?.(url);
			if (held) await held;
			if (hooks.fail?.(url)) return route.fulfill({ status: 500, json: { detail: 'boom' } });
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
			const heldDecision = hooks.holdDecision?.();
			if (heldDecision) await heldDecision;
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

	test('a decision while Load more is in flight keeps the appended page', async ({ page }) => {
		const vendors = seed(45, 'unverified');
		const page2 = gate();
		await stubVendors(page, vendors, {
			hold: (url) => (url.searchParams.get('page') === '2' ? page2.opened : undefined)
		});
		await page.goto('/vendors');
		const loadMore = page.locator('.btn-load-more');
		await expect(loadMore).toContainText('20 of 45');

		const page2Requested = page.waitForRequest((r) => r.url().includes('page=2'));
		await loadMore.click();
		await page2Requested;

		// Decide a page-1 row while page 2 is still held.
		const target = vendors[4];
		const row = page.locator('table tbody tr', { hasText: target.name });
		const reread = page.waitForResponse(
			(r) => new URL(r.url()).searchParams.get('id') === target.id
		);
		await row.getByRole('button', { name: 'Verify' }).click();
		await reread;
		await expect(row.getByRole('button', { name: 'Reject' })).toBeVisible();

		// Page 2 now lands: it is appended, not dropped, and the decided row
		// keeps its fresh verdict.
		const page2Landed = page.waitForResponse((r) => r.url().includes('page=2'));
		page2.release();
		await page2Landed;
		await expect(page.locator('table tbody tr')).toHaveCount(40);
		await expect(page.locator('.btn-load-more')).toContainText('40 of 45');
		await expect(row.getByRole('button', { name: 'Reject' })).toBeVisible();
		await expect(row.getByRole('button', { name: 'Verify' })).toHaveCount(0);
	});

	test('Load more during the one-row re-read still lets the re-read commit', async ({
		page
	}) => {
		const vendors = seed(45, 'unverified');
		const target = vendors[4];
		const rowRead = gate();
		await stubVendors(page, vendors, {
			hold: (url) => (url.searchParams.get('id') === target.id ? rowRead.opened : undefined)
		});
		await page.goto('/vendors');
		const loadMore = page.locator('.btn-load-more');
		await expect(loadMore).toContainText('20 of 45');

		const row = page.locator('table tbody tr', { hasText: target.name });
		const rereadRequested = page.waitForRequest(
			(r) => new URL(r.url()).searchParams.get('id') === target.id
		);
		await row.getByRole('button', { name: 'Verify' }).click();
		await rereadRequested;
		// Withheld (fail closed) while the re-read is out.
		await expect(row.getByRole('button', { name: 'Verify' })).toHaveCount(0);

		// Load more lands in full while the re-read is still held.
		const page2Landed = page.waitForResponse((r) => r.url().includes('page=2'));
		await loadMore.click();
		await page2Landed;
		await expect(page.locator('table tbody tr')).toHaveCount(40);

		// The re-read now lands and still commits its verdict.
		const reread = page.waitForResponse(
			(r) => new URL(r.url()).searchParams.get('id') === target.id
		);
		rowRead.release();
		await reread;
		await expect(row.getByRole('button', { name: 'Reject' })).toBeVisible();
		await expect(page.locator('table tbody tr')).toHaveCount(40);
	});

	test('a failed re-read says so in the row and Retry restores its actions', async ({
		page
	}) => {
		const vendors = seed(45, 'unverified');
		const target = vendors[4];
		let failRowRead = true;
		await stubVendors(page, vendors, {
			fail: (url) => url.searchParams.get('id') === target.id && failRowRead
		});
		await page.goto('/vendors');
		await expect(page.locator('.btn-load-more')).toContainText('20 of 45');

		const row = page.locator('table tbody tr', { hasText: target.name });
		await row.getByRole('button', { name: 'Verify' }).click();

		// Fail closed: the new status, no actions, and a note saying why.
		await expect(row).toContainText('Couldn’t refresh this row');
		await expect(row.getByRole('button', { name: 'Verify' })).toHaveCount(0);
		await expect(row.getByRole('button', { name: /^Reject/ })).toHaveCount(0);
		const retry = row.getByRole('button', { name: `Retry for ${target.name}` });
		await expect(retry).toBeVisible();

		failRowRead = false;
		await retry.click();
		await expect(row).not.toContainText('Couldn’t refresh this row');
		await expect(row.getByRole('button', { name: 'Reject' })).toBeVisible();
		await expect(page.locator('table tbody tr')).toHaveCount(20);
	});

	test('a filter change during a decision still lands the new filter\'s list', async ({
		page
	}) => {
		// The filter's whole-list read is in flight when the decision patches
		// the list. It used to be discarded as pre-decision, leaving the old
		// filter's rows, total and page under the new filter's chip and URL.
		const vendors = seed(45, 'unverified');
		const target = vendors[4];
		const decision = gate();
		const filterRead = gate();
		let heldFilterRead = false;
		await stubVendors(page, vendors, {
			holdDecision: () => decision.opened,
			hold: (url) => {
				if (
					heldFilterRead ||
					url.searchParams.get('status') !== 'unverified' ||
					url.searchParams.has('id')
				)
					return undefined;
				heldFilterRead = true;
				return filterRead.opened;
			}
		});
		await page.goto('/vendors');
		await expect(page.locator('.btn-load-more')).toContainText('20 of 45');

		// Verify a row; its POST is held.
		const verifyRequested = page.waitForRequest((r) => r.url().includes(`${target.id}/verify`));
		await page
			.locator('table tbody tr', { hasText: target.name })
			.getByRole('button', { name: 'Verify' })
			.click();
		await verifyRequested;

		// Change the filter while the decision is out; its list read is held.
		const filterRequested = page.waitForRequest(
			(r) =>
				new URL(r.url()).searchParams.get('status') === 'unverified' &&
				!new URL(r.url()).searchParams.has('id')
		);
		await page.locator('.filter-chip', { hasText: /^Unverified/ }).click();
		await filterRequested;

		// The decision lands (patching the list), then the filter's read does.
		const reread = page.waitForResponse(
			(r) => new URL(r.url()).searchParams.get('id') === target.id
		);
		decision.release();
		await reread;
		filterRead.release();

		// The list is the new filter's: a full first page of the 44 still
		// unverified, the decided vendor gone, and the URL agrees.
		await expect(page.locator('.btn-load-more')).toContainText('20 of 44');
		await expect(page.locator('table tbody tr')).toHaveCount(20);
		await expect(page.locator('table tbody tr', { hasText: target.name })).toHaveCount(0);
		await expect(page.locator('table tbody tr', { hasText: vendors[20].name })).toBeVisible();
		expect(new URL(page.url()).searchParams.get('status')).toBe('unverified');
	});
});
