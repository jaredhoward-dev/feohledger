import type { Locator, Page } from '@playwright/test';

import { NAV } from '#lib/nav.ts';
import { CARD_STATUSES } from '#lib/types/payment.ts';

import { expect, stubPartnerCapability, test } from '../fixtures/helpers';
import { exceptionSummary } from '../exceptions/summary';

/**
 * A list page must not move under the reader when its data lands.
 *
 * The operator report was "there are always glitches when I click on different
 * pages, especially the invoices page". Measured on a production build with a
 * PerformanceObserver: three sidebar pages drew their table, then pushed it
 * down once a late response arrived —
 *
 *   - /invoices: the status chips sat inline beside the search box. Their
 *     number depends on the active workflow and their width on the tallies,
 *     both of which load after first paint, so the row fitted on one line at
 *     mount and wrapped once they landed: the table dropped ~44px (a
 *     layout-shift score of 0.33 on a cold visit).
 *   - /payments: the four summary cards rendered only once
 *     `/api/payments/summary` answered, inserting a ~94px strip above the tabs
 *     and the table on every visit.
 *   - /exceptions: the type-filter row rendered only once the summary said
 *     which types exist, inserting ~40px above the table on every visit.
 *
 * Each case holds the late response(s) behind a gate, so the page is pinned in
 * its loading state for as long as the test needs, and compares where the
 * table (or tab bar) sits before and after the gate opens. The responses that
 * decide the late layout are stubbed, so the outcome does not depend on what
 * the shard's tenant holds: the stubs describe the layout that used to shift.
 *
 * Navigation is a sidebar click from the dashboard — the client-side route
 * change the operator was making — not a `page.goto`.
 */

type Hold = { release: () => void };

/**
 * Hold every request whose pathname is `pathname` until `release()`, then
 * answer with `body` (or let it through to the backend when `body` is
 * omitted). Matched on the exact pathname — a glob would also catch the
 * app's own `src/lib/api/*.ts` modules under `vite dev`
 * (tests-e2e/README.md § Stubbing an API route).
 */
async function hold(page: Page, pathname: string, body?: unknown): Promise<Hold> {
	let release!: () => void;
	const gate = new Promise<void>((resolve) => (release = resolve));
	await page.route(
		(url) => url.pathname === pathname,
		async (route) => {
			await gate;
			if (body === undefined) await route.fallback();
			else
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify(body)
				});
		}
	);
	return { release };
}

async function openFromSidebar(page: Page, href: string) {
	await page.goto('/');
	const link = page.locator(`aside.sidebar a[href="${href}"]`).first();
	await expect(link).toBeVisible();
	await link.click();
	await expect(page).toHaveURL((url) => url.pathname === href);
}

async function top(locator: Locator): Promise<number> {
	const box = await locator.boundingBox();
	expect(box, 'the anchor element must be laid out').not.toBeNull();
	return box!.y;
}

test.describe('list pages hold their layout while data loads', () => {
	test('/invoices: the table does not drop when the workflow and chip tallies land', async ({
		page
	}) => {
		const holds = [
			// An approval step adds the Ready for Review + Approved chips …
			await hold(page, '/api/workflows/active/steps', {
				extraction: true,
				approval: true,
				erp_export: true,
				approval_config: null
			}),
			// … and four-digit tallies widen every chip.
			await hold(page, '/api/invoices/counts', {
				counts: { new: 1234, ready_for_review: 2345, approved: 3456, failed: 4567 },
				total: 11602
			}),
			await hold(page, '/api/invoices')
		];

		await openFromSidebar(page, '/invoices');
		const table = page.locator('table').first();
		await expect(table).toBeVisible();
		await expect(page.locator('nav.filters').getByRole('button', { name: /^New/ })).toBeVisible();
		const before = await top(table);

		for (const h of holds) h.release();
		await expect(
			page.locator('nav.filters').getByRole('button', { name: /Ready for Review\s*2,?345/ })
		).toBeVisible();

		expect(await top(table)).toBe(before);
	});

	test('/payments: the tabs and table do not drop when the summary lands', async ({ page }) => {
		const summary = await hold(page, '/api/payments/summary', {
			total_paid: '123456.78',
			total_pending: '9876.54',
			payment_count: 42,
			total_rebates: '0',
			queue_count: 7,
			currency: 'USD',
			unconverted_payment_count: 0
		});

		await openFromSidebar(page, '/payments');
		const tabs = page.getByRole('tablist', { name: 'Payments' });
		await expect(tabs).toBeVisible();
		const strip = page.locator('.summary-cards');
		// Drawn while pending, and says so.
		await expect(strip).toHaveAttribute('aria-busy', 'true');
		await expect(strip.locator('.scard-value').first()).toHaveText('—');
		const before = await top(tabs);

		summary.release();
		await expect(strip).not.toHaveAttribute('aria-busy', 'true');
		await expect(
			strip.locator('.scard').filter({ hasText: 'Ready to Pay' }).locator('.scard-value')
		).toHaveText('7');

		expect(await top(tabs)).toBe(before);
	});

	test('/exceptions: the table does not drop when the type roster lands', async ({ page }) => {
		const summary = await hold(
			page,
			'/api/exceptions/summary',
			exceptionSummary({
				open: 5,
				by_type: { fraud_flag: 2, duplicate: 3 },
				by_severity: { error: 5 }
			})
		);

		await openFromSidebar(page, '/exceptions');
		const typeRow = page.getByTestId('exception-type-filters');
		// The row holds its place with "All types" alone until the roster lands.
		await expect(typeRow.locator('.filter-chip')).toHaveCount(1);
		const table = page.locator('#exc-panel-queue table').first();
		await expect(table).toBeVisible();
		const before = await top(table);

		summary.release();
		await expect(typeRow.locator('.filter-chip', { hasText: 'Fraud Flag' })).toBeVisible();

		expect(await top(table)).toBe(before);
	});
});

/*
 * The layout-shift budget, table-driven over every main list page.
 *
 * The three cases above pin the specific shifts #644 fixed, with stubbed
 * bodies. This half measures what a reader sees on every list page against
 * the tenant's real data: each page is opened by a client-side route change
 * with every API response held back, then released and answered after
 * `LATENCY_MS` — a production-like round trip, not zero, because a response
 * that lands in the same frame as the first paint hides exactly the shift a
 * customer sees. A `PerformanceObserver` on `layout-shift` sums every shift
 * from the click until the network goes quiet, and the total must stay under
 * 0.1, the Core Web Vitals "good" line. Entries are summed whether or not they
 * follow input: the navigation is a synthetic `click()` from script, so no
 * entry is excused as input-driven, and a stricter sum can only catch more.
 *
 * A page whose table is `fixed` (DataTable's `fixed` prop, frontend/docs/
 * ui-patterns.md § Data tables) also has its header cells' x positions
 * compared before and after the data lands: under `table-layout: auto` the
 * columns are sized against the "Loading…" row and then re-sized against the
 * real rows, which slides every column sideways. A vertical-only shift score
 * can stay small while that happens, so it gets its own exact check.
 *
 * `LIST_PAGES` plus `NOT_LIST_PAGES` must classify every sidebar destination
 * (`NAV`, lib/nav.ts): a new page fails the guard test below until it is put
 * in one or the other, the way lib/help/pages.ts must hold a row for every
 * NAV href (content.test.ts).
 */

/** A production-like API round trip. No per-endpoint timings were recorded
 *  on 2026-10-22, so this is one uniform figure at the slow end of what a
 *  customer's browser sees from the VM — slow enough that no response lands in
 *  the first paint. */
const LATENCY_MS = 400;

/** The Core Web Vitals "good" threshold for cumulative layout shift. */
const CLS_BUDGET = 0.1;

/** A neutral page with no list of its own to start every navigation from. */
const START = '/help';

interface ListPage {
	href: string;
	/** Selector of the `fixed` DataTable whose header columns must not move. */
	fixedTable?: string;
}

const LIST_PAGES: ListPage[] = [
	{ href: '/' },
	{ href: '/invoices', fixedTable: '.workspace table' },
	{ href: '/payments', fixedTable: '.pay-queue table' },
	{ href: '/exceptions', fixedTable: '#exc-panel-queue table' },
	{ href: '/vendors', fixedTable: '.vendor-table table' },
	{ href: '/purchase-orders', fixedTable: '.po-table table' },
	{ href: '/gl-accounts', fixedTable: '.gl-table table' },
	{ href: '/goods-receipts', fixedTable: '.receipts-table table' },
	{ href: '/credit-memos' },
	{ href: '/expenses' },
	{ href: '/contracts' },
	{ href: '/bank-reconciliation' },
	{ href: '/vendors/screening', fixedTable: '.screening-table table' },
	{ href: '/vendors/change-requests', fixedTable: '.change-table table' },
	{ href: '/requisitions', fixedTable: '.req-table table' },
	{ href: '/intake', fixedTable: '.intake-table table' },
	{ href: '/catalogs', fixedTable: '.catalog-table table' },
	{ href: '/budgets', fixedTable: '.budget-table table' },
	{ href: '/recurring', fixedTable: '.recurring-table table' },
	{ href: '/vendor-statements', fixedTable: '.recon-table table' },
	{ href: '/positive-pay', fixedTable: '.ppay-table table' },
	{ href: '/experiments', fixedTable: '.experiment-table table' },
	{ href: '/adaptive', fixedTable: '.suggestions-table table' },
	{ href: '/workflows', fixedTable: '.wf-table table' },
	{ href: '/admin?tab=users', fixedTable: '.users-table table' },
	{ href: '/admin?tab=roles', fixedTable: '.system-roles table' },
	{ href: '/admin/entities', fixedTable: '.entities-table table' },
	{ href: '/admin/api-keys', fixedTable: '.keys-table table' },
	{ href: '/admin/access-review', fixedTable: '.review-table table' },
	{ href: '/admin/webhooks', fixedTable: '.subs-table table' },
	{ href: '/audit', fixedTable: '.audit-trail table' },
	{ href: '/discounts', fixedTable: '.offers-table table' },
	{ href: '/tax', fixedTable: '.tax-table table' },
	{ href: '/admin/partner', fixedTable: '.partner-table table' }
];

/**
 * Second tabs of a measured page, each its own table: a sidebar link opens a
 * page on its first tab, so `LIST_PAGES` alone would never measure these.
 * Measured exactly like `LIST_PAGES`; not part of the sidebar classification
 * (their pathname must be a `LIST_PAGES` page — the guard test checks).
 */
const TAB_PAGES: ListPage[] = [
	{ href: '/goods-receipts?tab=inspections', fixedTable: '.inspections-table table' }
];

const MEASURED_PAGES: ListPage[] = [...LIST_PAGES, ...TAB_PAGES];

/**
 * The fixed tables whose widths are sized to the longest locale
 * (frontend/docs/ui-patterns.md § Data tables).
 */
const LOCALE_SIZED = new Set([
	'/invoices',
	'/payments',
	'/exceptions',
	'/vendors',
	'/purchase-orders',
	'/gl-accounts',
	'/goods-receipts',
	'/goods-receipts?tab=inspections',
	'/vendors/screening',
	'/vendors/change-requests',
	'/requisitions',
	'/intake',
	'/catalogs',
	'/budgets',
	'/recurring',
	'/vendor-statements',
	'/positive-pay',
	'/experiments',
	'/adaptive',
	'/workflows',
	'/admin?tab=users',
	'/admin?tab=roles',
	'/admin/entities',
	'/admin/api-keys',
	'/admin/access-review',
	'/admin/webhooks',
	'/audit',
	'/discounts',
	'/tax',
	'/admin/partner'
]);

/**
 * Locale-sized tables a sidebar link does not open by itself: /payments' other
 * tabs (the cards tab holds two tables). Held to the same per-locale check as
 * the `LOCALE_SIZED` pages; `label` names the test. The e2e seed holds no
 * virtual card or rebate: the cards table is answered with `CARD_PER_STATUS`
 * below, so its status badges are measured; the rebates table is measured on
 * its header and empty row only, its badge width resting on the page's own
 * measurement.
 */
const LOCALE_SIZED_VIEWS: (ListPage & { fixedTable: string; label: string })[] = [
	{ href: '/payments?tab=history', fixedTable: '.pay-history table', label: '/payments history' },
	{ href: '/payments?tab=runs', fixedTable: '.pay-runs table', label: '/payments runs' },
	{ href: '/payments?tab=cards', fixedTable: '.pay-rebates table', label: '/payments rebates' },
	{ href: '/payments?tab=cards', fixedTable: '.pay-cards table', label: '/payments cards' },
	{
		href: '/admin/webhooks',
		fixedTable: '.deliveries-table table',
		label: '/admin/webhooks deliveries'
	},
	{
		href: '/admin?tab=roles',
		fixedTable: '.custom-roles table',
		label: '/admin?tab=roles custom roles'
	}
];

/**
 * The locales whose strings run longest against the English widths: de and es
 * for most headers and buttons, and the ones the widest badges come from
 * (fr "COMPTABILISÉE DANS L'ERP"; pt-BR "PARCIALMENTE RECEBIDO", "PRECISA DE
 * UMA DECISÃO", "COMPARTILHADA"; ja "エスカレーション済み").
 */
const OVERFLOW_LOCALES = ['de', 'es', 'fr', 'pt-BR', 'ja'] as const;

/**
 * Sidebar destinations deliberately outside the budget, each with its reason.
 * A list page belongs in `LIST_PAGES`, not here: a page whose table the e2e
 * tenant cannot fill is measured against a fixture (`PAGE_FIXTURES`), as
 * /admin/partner is.
 */
const NOT_LIST_PAGES: Record<string, string> = {
	'/assistant': 'A conversation, not a list',
	'/cfo': 'Charts and KPI cards, not a list',
	'/cash-flow': 'A planning workspace, not a list',
	'/reports': 'A report builder; its table is the output of a run',
	'/admin/retention': 'A settings form',
	'/admin/privacy': 'Request forms and a short history, not a list page',
	'/organization': 'A settings form',
	'/billing': 'A plan summary, not a list',
	'/admin/health': 'A status board of fixed rows'
};

const NAV_HREFS = [
	...new Set(NAV.flatMap((e) => (e.kind === 'link' ? [e.href] : e.children.map((c) => c.href))))
];

declare global {
	interface Window {
		__shifts: { value: number; sources: string[] }[];
		__shiftObserver?: PerformanceObserver;
	}
}

/** Collect every layout-shift entry, with the nodes it moved, from page load. */
async function observeLayoutShifts(page: Page) {
	await page.addInitScript(() => {
		window.__shifts = [];
		type Shift = PerformanceEntry & {
			value: number;
			sources?: { node?: Node | null }[];
		};
		const record = (entries: PerformanceEntryList) => {
			for (const e of entries as Shift[]) {
				window.__shifts.push({
					value: e.value,
					sources: (e.sources ?? []).map((s) => {
						const el = s.node instanceof Element ? s.node : null;
						return el ? `${el.tagName.toLowerCase()}.${[...el.classList].join('.')}` : '#text';
					})
				});
			}
		};
		const obs = new PerformanceObserver((list) => record(list.getEntries()));
		obs.observe({ type: 'layout-shift', buffered: true });
		window.__shiftObserver = obs;
	});
}

/**
 * Hold every API response behind a gate; once released, answer each after
 * `LATENCY_MS`. Matched on a pathname under `/api/`, which never catches the
 * app's own `src/lib/api*.ts` modules under `vite dev`. `inflight()` counts
 * API requests the page has issued and not yet had answered.
 */
async function delayApi(page: Page) {
	let release!: () => void;
	const gate = new Promise<void>((resolve) => (release = resolve));
	const pending = new Set<unknown>();
	const isApi = (url: string) => new URL(url).pathname.startsWith('/api/');
	page.on('request', (r) => {
		if (isApi(r.url())) pending.add(r);
	});
	page.on('requestfinished', (r) => pending.delete(r));
	page.on('requestfailed', (r) => pending.delete(r));
	await page.route(
		(url) => url.pathname.startsWith('/api/'),
		async (route) => {
			await gate;
			await new Promise((resolve) => setTimeout(resolve, LATENCY_MS));
			await route.fallback();
		}
	);
	return { release, inflight: () => pending.size };
}

/** Two animation frames: everything the last response changed is laid out. */
async function nextFrames(page: Page) {
	await page.evaluate(
		() => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r())))
	);
}

async function headerXs(table: Locator): Promise<number[]> {
	return table.locator('thead th').evaluateAll((ths) => ths.map((th) => th.getBoundingClientRect().x));
}

/** /admin/partner's children table: a partner with one child per plan tier,
 *  one carrying a 63-character slug (the longest a slug may be) and one with
 *  no brand name (the "—" cell). The e2e tenant is not a partner, so without
 *  this (and the capability stubbed on `/api/auth/me`) the page would not even
 *  open, and no child row would be measured. */
const PARTNER_OVERVIEW = {
	organization_id: '00000000-0000-4000-8000-000000000430',
	name: 'Layout Fixture Partner',
	is_partner: true,
	children: [
		{
			id: '00000000-0000-4000-8000-000000000431',
			name: 'Layout Fixture Subsidiary Holdings International',
			slug: 'layout-fixture-subsidiary-holdings-international-and-partners-x',
			plan: 'growth',
			product_name: 'Layout Fixture Accounts Payable Suite'
		},
		{
			id: '00000000-0000-4000-8000-000000000432',
			name: 'Layout Fixture Retail',
			slug: 'layout-fixture-retail',
			plan: 'scale',
			product_name: ''
		},
		{
			id: '00000000-0000-4000-8000-000000000433',
			name: 'Layout Fixture Startup',
			slug: 'layout-fixture-startup',
			plan: 'free',
			product_name: 'Fixture AP'
		}
	]
};

/**
 * Responses a measured page needs that the e2e tenant cannot give, answered
 * by route interception for both the layout-shift budget and the per-locale
 * check (the latter answers more pages' lists inline below).
 */
const PAGE_FIXTURES: Record<string, (page: Page) => Promise<void>> = {
	'/admin/partner': async (page) => {
		// The page needs the operator-granted partner capability (decisions §574).
		await stubPartnerCapability(page);
		await page.route(
			(url) => url.pathname === '/api/partner',
			(route) =>
				route.request().method() === 'GET'
					? route.fulfill({ json: PARTNER_OVERVIEW })
					: route.fallback()
		);
	}
};

test.describe('list pages stay within the layout-shift budget', () => {
	test('every sidebar page is classified as a measured list page or not', () => {
		const measured = new Set(LIST_PAGES.map((p) => p.href));
		for (const href of NAV_HREFS) {
			expect(
				measured.has(href) || href in NOT_LIST_PAGES,
				`${href} is in the sidebar but in neither LIST_PAGES nor NOT_LIST_PAGES`
			).toBe(true);
			expect(
				measured.has(href) && href in NOT_LIST_PAGES,
				`${href} is in both LIST_PAGES and NOT_LIST_PAGES`
			).toBe(false);
		}
		for (const href of [...measured, ...Object.keys(NOT_LIST_PAGES)]) {
			expect(NAV_HREFS, `${href} is no longer a sidebar destination`).toContain(href);
		}
		for (const { href } of TAB_PAGES) {
			const pathname = new URL(href, 'http://x').pathname;
			expect([...measured], `${href} is a tab of a page LIST_PAGES does not measure`).toContain(
				pathname
			);
		}
	});

	for (const { href, fixedTable } of MEASURED_PAGES) {
		test(`${href}: cumulative layout shift stays under ${CLS_BUDGET}`, async ({ page }) => {
			// Registered before `delayApi`, so the delay (the later route, run
			// first) still holds the fixture's answer behind the gate.
			await PAGE_FIXTURES[href]?.(page);
			await observeLayoutShifts(page);
			await page.goto(START);
			await expect(page.locator('h1').first()).toBeVisible();
			const api = await delayApi(page);

			// A client-side route change, like a sidebar click, from script so
			// the shifts that follow are not excused as following user input.
			await page.evaluate((to) => {
				window.__shifts = [];
				const a = document.createElement('a');
				a.href = to;
				a.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0';
				document.body.appendChild(a);
				a.click();
				a.remove();
			}, href);
			// `href` may carry a query (`/admin?tab=users` picks the tab).
			await expect(page).toHaveURL((url) => url.pathname + url.search === href);
			await expect(page.locator('h1').first()).toBeVisible();

			// The page's loading state, drawn while every response is held.
			let before: number[] | undefined;
			if (fixedTable) {
				const table = page.locator(fixedTable).first();
				await expect(table.locator('thead th').first()).toBeVisible();
				before = await headerXs(table);
			}

			api.release();
			// Settled: no API request outstanding across two frames.
			await expect.poll(api.inflight, { timeout: 20_000 }).toBe(0);
			await nextFrames(page);
			await expect.poll(api.inflight, { timeout: 20_000 }).toBe(0);
			await nextFrames(page);

			if (fixedTable) {
				const table = page.locator(fixedTable).first();
				// Data, or a settled empty / error row, has replaced "Loading…".
				await expect(table.locator('tbody tr').first()).toBeVisible();
				expect(await headerXs(table), 'the header columns slid sideways').toEqual(before);
			}

			const shifts = await page.evaluate(() => {
				const late = window.__shiftObserver?.takeRecords() ?? [];
				const all = [
					...window.__shifts,
					...late.map((e) => ({ value: (e as PerformanceEntry & { value: number }).value, sources: [] }))
				];
				return all;
			});
			const cls = shifts.reduce((sum, s) => sum + s.value, 0);
			expect(
				cls,
				`layout shifts on ${href}: ${JSON.stringify(shifts.map((s) => [s.value.toFixed(3), s.sources]))}`
			).toBeLessThan(CLS_BUDGET);
		});
	}
});

/**
 * A fixed column cannot grow, so a header, a status badge or a row's buttons
 * wider than it overprint the next column — measured in es/fr/de/pt-BR on the
 * pages above after they were sized to English ("FECHA DEL ESTADO DE CUENTA"
 * at ≈200px in a 130px column, the badge "En attente d’approbation" at
 * ≈190px in 150px). Fixed headers and action cells now wrap; a badge does
 * not, so its column is sized to the longest locale. Overflow is read on the
 * cell: a `nowrap` badge is exactly as wide as its text, so only the `<td>`
 * holding it shows the overflow.
 */
async function overflowingCells(table: Locator): Promise<string[]> {
	return table.evaluate((t) => {
		const cells = new Set<Element>();
		for (const el of t.querySelectorAll('th, td.actions')) cells.add(el);
		for (const el of t.querySelectorAll('td .badge')) {
			const td = el.closest('td');
			if (td) cells.add(td);
		}
		return [...cells]
			.filter((el) => el.scrollWidth > el.clientWidth)
			.map(
				(el) =>
					`${el.tagName.toLowerCase()}.${[...el.classList].join('.')} "${(el.textContent ?? '').replace(/\s+/g, ' ').trim()}" ${el.scrollWidth}px in ${el.clientWidth}px`
			);
	});
}

/**
 * The row each page's widest badge needs, which the shard's tenant need not
 * hold: the page's real list response, with its first row turned into that
 * row. Patching a real row keeps the response's shape the backend's own.
 * `badge` is the badge that must then be on screen, so a tenant with no row
 * to patch fails here instead of passing without measuring anything.
 */
type Row = Record<string, unknown>;
const WIDEST_ROW: Record<string, { pathname: string; patch: (row: Row) => Row; badge: string }> = {
	'/invoices': {
		pathname: '/api/invoices',
		patch: (row) => ({ ...row, status: 'posted_in_erp' }),
		badge: '.badge.posted_in_erp'
	},
	'/payments': {
		pathname: '/api/payments/queue',
		patch: (row) => ({ ...row, status: 'posted_in_erp' }),
		badge: '.badge.posted_in_erp'
	},
	'/payments?tab=history': {
		pathname: '/api/payments',
		patch: (row) => ({ ...row, status: 'pending_compliance' }),
		badge: '.badge.pending_compliance'
	},
	'/payments?tab=runs': {
		pathname: '/api/payments/runs/',
		patch: (row) => ({ ...row, status: 'executing' }),
		badge: '.badge.executing'
	},
	'/exceptions': {
		pathname: '/api/exceptions',
		patch: (row) => ({ ...row, status: 'escalated' }),
		badge: '.badge.badge-escalated'
	},
	'/vendors': {
		pathname: '/api/vendors',
		// `status_actions` offers Verify + Reject, so the row carries its full
		// set of buttons (and a Verify to put it in the refresh-failed state).
		patch: (row) => ({
			...row,
			status: 'unverified',
			screening_status: 'review',
			source: 'erp_sync',
			status_actions: { verify: null, reject: null }
		}),
		badge: '.badge.unverified'
	},
	'/purchase-orders': {
		pathname: '/api/purchase-orders',
		patch: (row) => ({
			...row,
			status: 'partially_received',
			total: null,
			total_unverifiable_reason: 'unpriceable'
		}),
		badge: '.badge.partially_received'
	},
	'/gl-accounts': {
		pathname: '/api/gl-accounts',
		patch: (row) => ({
			...row,
			entity_id: null,
			erp_account_id: null,
			erp_link_candidates: {
				reason: 'duplicate_code',
				accounts: [{ erp_account_id: 'erp-layout-1', code: '6000', name: 'Layout fixture' }]
			}
		}),
		badge: '.badge.needs-decision'
	},
	'/goods-receipts': {
		pathname: '/api/goods-receipts',
		patch: (row) => ({ ...row, status: 'reversed' }),
		badge: '.badge.reversed'
	}
};

/** Answer `pathname` with the backend's own list, its first row patched. */
async function patchFirstRow(page: Page, pathname: string, patch: (row: Row) => Row) {
	await page.route(
		(url) => url.pathname === pathname,
		async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const response = await route.fetch();
			const body = await response.json();
			// /api/gl-accounts is a bare array; every other list is `{ items }`.
			const rows: Row[] = Array.isArray(body) ? body : body.items;
			if (rows.length > 0) rows[0] = patch(rows[0]);
			await route.fulfill({ response, json: body });
		}
	);
}

/** One pending bank change the caller may not approve: the row that carries
 *  the separation-of-duties note above its buttons. */
const REFUSED_CHANGE_REQUEST = {
	id: '00000000-0000-4000-8000-000000000321',
	vendor_id: '00000000-0000-4000-8000-000000000322',
	vendor_name: 'Layout Fixture Supplies',
	change_type: 'bank_details',
	status: 'pending',
	proposed_value: { bank_name: 'Fixture Bank', account_number_last4: '8888' },
	requested_by_vendor_user_id: null,
	requested_by_user_id: '00000000-0000-4000-8000-000000000323',
	reviewed_by_user_id: null,
	reviewed_at: null,
	review_note: null,
	created_at: '2026-11-01T09:00:00Z',
	approve_refusal: {
		code: 'vendor_change_self_requested',
		message: 'You cannot approve a bank-detail change you requested.'
	}
};

/** One partially accepted inspection: the Result column's widest badge (fr
 *  "ACCEPTATION PARTIELLE"). The lean e2e seed records no inspections, so the
 *  tab's list is answered with this row rather than patched. */
const PARTIAL_INSPECTION = {
	id: '00000000-0000-4000-8000-000000000331',
	inspection_number: 'INS-2026-00001',
	po_id: null,
	gr_id: '00000000-0000-4000-8000-000000000332',
	gr_number: 'GR-2026-00001',
	result: 'partial',
	inspected_date: '2026-11-01',
	inspector: 'Layout Fixture',
	accepted_quantity: 8,
	rejected_quantity: 2,
	deviation_notes: 'Two cartons crushed in transit',
	status: 'completed',
	created_at: '2026-11-01T09:00:00Z'
};

/** One virtual card per `CardStatus`: the cards table's status column holds
 *  the widest label in each locale (de "ABGESCHLOSSEN", es "COMPLETADA", ja
 *  "キャンセル済み", …), and which status is widest differs by locale, so every
 *  status is on screen rather than one picked for one catalogue. The lean e2e
 *  seed issues no cards, so the tab's list is answered with these rows rather
 *  than patched. */
const CARD_PER_STATUS = CARD_STATUSES.map((status, i) => ({
	id: `00000000-0000-4000-8000-00000000034${i}`,
	invoice_id: '00000000-0000-4000-8000-000000000350',
	card_provider: 'lithic',
	last_four: '4242',
	amount_limit: 1250,
	amount_charged: status === 'charged' || status === 'completed' ? 1250 : null,
	currency: 'USD',
	status,
	expires_at: '2026-12-31T00:00:00Z',
	created_at: '2026-11-01T09:00:00Z',
	vendor_name: 'Layout Fixture Supplies',
	invoice_number: `INV-LAYOUT-${i}`
}));

/** /admin/webhooks' widest rows: a paused subscription (es "INACTIVO") whose
 *  rotation overlap is live, so the secret cell holds its pill (fr "Secret
 *  précédent jusqu’à …", which wraps in its column), and one delivery per
 *  status (de "FEHLGESCHLAGEN"), the failed and dead ones carrying the
 *  Redeliver button. The e2e seed holds no webhook, so both lists are
 *  answered with these rows. */
const WEBHOOK_SUBS = [
	{
		id: '00000000-0000-4000-8000-000000000361',
		name: 'Layout Fixture ERP',
		target_url: 'https://hooks.example.com/layout-fixture/receiver',
		event_types: ['invoice.approved', 'payment.sent'],
		secret_prefix: 'whsec_lay',
		active: false,
		created_at: '2026-11-01T09:00:00Z',
		updated_at: '2026-11-01T09:00:00Z',
		previous_secret_expires_at: '2099-12-31T23:00:00Z'
	}
];
const WEBHOOK_DELIVERIES = ['pending', 'delivered', 'failed', 'dead'].map((status, i) => ({
	id: `00000000-0000-4000-8000-00000000037${i}`,
	subscription_id: WEBHOOK_SUBS[0].id,
	event_id: `00000000-0000-4000-8000-00000000038${i}`,
	event_type: 'invoice.approved',
	status,
	attempt_count: 3,
	response_code: 500,
	next_attempt_at: null,
	last_attempt_at: '2026-11-01T09:00:00Z',
	created_at: '2026-11-01T09:00:00Z'
}));

/** /adaptive's widest suggestion rows: one per status (pt-BR "DESATUALIZADA"),
 *  the open and stale ones carrying their Dismiss button (the backend's
 *  `dismiss_refusal` verdicts). The e2e seed need not hold a
 *  suggestion, so the list is answered with these rows. */
const SUGGESTION_PER_STATUS = (['open', 'dismissed', 'applied', 'stale'] as const).map(
	(status, i) => ({
		id: `00000000-0000-4000-8000-00000000039${i}`,
		kind: 'auto_approve_threshold',
		vendor_id: '00000000-0000-4000-8000-000000000400',
		vendor_name: 'Layout Fixture Supplies International',
		title: 'Auto-approve invoices from this vendor under the usual amount',
		rationale: 'Every one of the last 40 invoices was approved unchanged.',
		payload: {},
		confidence_pct: '97.5',
		status,
		created_at: '2026-11-01T09:00:00Z',
		dismissed_at: null,
		actions: {
			dismiss:
				status === 'open' || status === 'stale'
					? null
					: {
							code: 'workflow_suggestion_not_dismissable',
							message: `A suggestion that is ${status} cannot be dismissed.`,
							params: { status }
						}
		}
	})
);

/** A custom role for /admin?tab=roles' second table, appended to the real
 *  roles list (the e2e seed holds only the four system roles), so its Edit /
 *  Delete actions cell is on screen. */
const CUSTOM_ROLE = {
	id: '00000000-0000-4000-8000-000000000410',
	name: 'Layout Fixture Reviewer',
	description: 'Reviews vendor bank changes',
	is_system: false,
	permissions: ['vendor.bank_change.approve', 'vendor.block']
};

/** One discount offer per status: the status column holds the widest badge in
 *  each locale (de "ANGENOMMEN", …), and the open offer carries Accept and
 *  Decline, the widest action cell. The lean e2e seed holds no offer, so the
 *  list is answered with these rows rather than patched. */
const DISCOUNT_STATUSES = ['offered', 'accepted', 'captured', 'declined', 'expired'] as const;
const DISCOUNT_PER_STATUS = DISCOUNT_STATUSES.map((status, i) => ({
	id: `00000000-0000-4000-8000-00000000039${i}`,
	scope: 'invoice',
	invoice_id: `00000000-0000-4000-8000-00000000040${i}`,
	vendor_id: '00000000-0000-4000-8000-000000000410',
	source: 'supplier',
	status,
	tiers: [
		{ days: 10, percent: 2 },
		{ days: 20, percent: 1 }
	],
	base_amount: '12500.00',
	currency: 'USD',
	valid_from: '2026-11-01',
	valid_until: '2099-12-31',
	accepted_tier: status === 'accepted' || status === 'captured' ? { days: 10, percent: 2 } : null,
	accepted_at: null,
	captured_amount: status === 'captured' ? '250.00' : null,
	captured_at: null,
	financing_provider: null,
	notes: null,
	created_at: '2026-11-01T09:00:00Z',
	updated_at: '2026-11-01T09:00:00Z',
	vendor_name: 'Layout Fixture Supplies',
	invoice_number: `INV-LAYOUT-D${i}`,
	actions:
		status === 'offered'
			? { accept: null, decline: null }
			: {
					accept: { code: 'offer_not_open', message: 'Not open.' },
					decline: { code: 'offer_not_open', message: 'Not open.' }
				},
	capturable_tier_days: status === 'offered' ? [10, 20] : []
}));

/** One 1099 vendor per badge in each of /tax's three badge columns: eligible
 *  with a W-9 and a verified TIN, then ineligible with no W-9 and a TIN on
 *  file but unverified (de "NICHT VERIFIZIERT"), then no TIN at all. The lean
 *  e2e seed pays no 1099 vendor, so the report is answered with these rows. */
const TAX_ROW = {
	tax_classification: 'individual',
	w9_received_date: null,
	ytd_paid: '1500.00',
	over_threshold: true,
	payment_count: 3,
	card_paid: '0.00',
	card_payment_count: 0,
	box_allocations: [],
	unmapped_paid: '0.00',
	unmapped_payment_count: 0,
	box_unallocated: '0.00',
	required_forms: ['1099-NEC']
};
const TAX_REPORT = {
	year: 2026,
	threshold_usd: '600',
	currency: 'USD',
	vendor_count_total: 3,
	vendor_count_eligible_over_threshold: 1,
	vendor_count_over_threshold_without_w9: 0,
	total_reportable: '1500.00',
	total_reportable_usd: '1500.00',
	total_card_excluded: '0.00',
	box_allocations: [],
	total_unmapped: '0.00',
	box_unallocated: '0.00',
	box_allocation_reconciled: true,
	generated_at: '2026-11-01T09:00:00Z',
	rows: [
		{
			...TAX_ROW,
			vendor_id: '00000000-0000-4000-8000-000000000421',
			vendor_name: 'Layout Fixture Eligible',
			tax_id: '12-3456789',
			is_1099_eligible: true,
			w9_received_date: '2026-01-15',
			w9_on_file: true,
			tin_verified: true
		},
		{
			...TAX_ROW,
			vendor_id: '00000000-0000-4000-8000-000000000422',
			vendor_name: 'Layout Fixture Unverified',
			tax_id: '98-7654321',
			is_1099_eligible: false,
			w9_on_file: false,
			tin_verified: false
		},
		{
			...TAX_ROW,
			vendor_id: '00000000-0000-4000-8000-000000000423',
			vendor_name: 'Layout Fixture No TIN',
			tax_id: null,
			is_1099_eligible: true,
			w9_on_file: false,
			tin_verified: false
		}
	]
};

test.describe('fixed list tables fit their longest locale', () => {
	for (const locale of OVERFLOW_LOCALES) {
		const tables = [
			...MEASURED_PAGES.flatMap(({ href, fixedTable }) =>
				fixedTable && LOCALE_SIZED.has(href) ? [{ href, fixedTable, label: href }] : []
			),
			...LOCALE_SIZED_VIEWS
		];
		for (const { href, fixedTable, label } of tables) {
			test(`${label} (${locale}): no header, badge or action cell overflows its column`, async ({
				page
			}) => {
				if (href === '/vendors/change-requests') {
					// The refusal note is the widest thing an actions cell holds, and
					// the shard's tenant need not have a request to refuse.
					await page.route(
						(url) => url.pathname === '/api/vendors/change-requests',
						(route) =>
							route.fulfill({
								json: { items: [REFUSED_CHANGE_REQUEST], total: 1, page: 1, page_size: 25 }
							})
					);
				}
				if (href === '/goods-receipts?tab=inspections') {
					await page.route(
						(url) => url.pathname === '/api/inspections',
						(route) =>
							route.request().method() === 'GET'
								? route.fulfill({
										json: { items: [PARTIAL_INSPECTION], total: 1, page: 1, page_size: 20 }
									})
								: route.fallback()
					);
				}
				if (label === '/payments cards') {
					await page.route(
						(url) => url.pathname === '/api/cards',
						(route) =>
							route.request().method() === 'GET'
								? route.fulfill({
										json: { items: CARD_PER_STATUS, total: CARD_PER_STATUS.length }
									})
								: route.fallback()
					);
				}
				if (href === '/admin/webhooks') {
					await page.route(
						(url) => url.pathname === '/api/webhooks',
						(route) =>
							route.request().method() === 'GET'
								? route.fulfill({ json: WEBHOOK_SUBS })
								: route.fallback()
					);
					await page.route(
						(url) => url.pathname === '/api/webhooks/deliveries',
						(route) =>
							route.request().method() === 'GET'
								? route.fulfill({ json: WEBHOOK_DELIVERIES })
								: route.fallback()
					);
				}
				if (href === '/adaptive') {
					await page.route(
						(url) => url.pathname === '/api/adaptive/suggestions',
						(route) =>
							route.request().method() === 'GET'
								? route.fulfill({ json: { suggestions: SUGGESTION_PER_STATUS } })
								: route.fallback()
					);
				}
				if (label === '/admin?tab=roles custom roles') {
					await page.route(
						(url) => url.pathname === '/api/admin/roles',
						async (route) => {
							if (route.request().method() !== 'GET') return route.fallback();
							const response = await route.fetch();
							const roles = await response.json();
							await route.fulfill({ response, json: [...roles, CUSTOM_ROLE] });
						}
					);
				}
				if (href === '/discounts') {
					await page.route(
						(url) => url.pathname === '/api/discounts/offers',
						(route) =>
							route.request().method() === 'GET'
								? route.fulfill({
										json: {
											items: DISCOUNT_PER_STATUS,
											total: DISCOUNT_PER_STATUS.length,
											page: 1,
											page_size: 20
										}
									})
								: route.fallback()
					);
				}
				if (href === '/tax') {
					await page.route(
						(url) => url.pathname === '/api/tax/1099-report',
						(route) =>
							route.request().method() === 'GET'
								? route.fulfill({ json: TAX_REPORT })
								: route.fallback()
					);
				}
				await PAGE_FIXTURES[href]?.(page);
				const widest = WIDEST_ROW[href];
				if (widest) await patchFirstRow(page, widest.pathname, widest.patch);
				await page.addInitScript((lc) => localStorage.setItem('feoh_locale', lc), locale);
				await page.goto(href);
				await expect(page.locator('html')).toHaveAttribute('lang', locale);

				const table = page.locator(fixedTable).first();
				await expect(table.locator('tbody tr').first()).toBeVisible();
				// Settled: the "Loading…" row (the only empty row with a status
				// region) has given way to data or a settled empty / error row.
				await expect(table.locator('[data-testid="table-empty"] [role="status"]')).toHaveCount(0);
				if (href === '/vendors/change-requests') {
					await expect(table.locator('.refusal-note')).toBeVisible();
				}
				if (widest) await expect(table.locator(widest.badge).first()).toBeVisible();
				if (href === '/vendors') {
					// A decided row whose re-read failed carries a note + Retry in
					// its actions cell — the longest thing that cell holds in de /
					// fr. Reach that state with nothing written: the Verify is
					// stubbed, and the one-row re-read (`?id=`) fails.
					await page.route(
						(url) => /^\/api\/vendors\/[^/]+\/verify$/.test(url.pathname),
						(route) =>
							route.fulfill({
								json: { id: route.request().url().split('/').at(-2), status: 'active' }
							})
					);
					await page.route(
						(url) => url.pathname === '/api/vendors' && url.searchParams.has('id'),
						(route) => route.fulfill({ status: 500, json: { detail: 'layout fixture' } })
					);
					const first = table.locator('tbody tr').first();
					await first.locator('td.actions button.variant-success').click();
					await expect(first.locator('.row-refresh-failed')).toBeVisible();
				}
				if (href === '/goods-receipts?tab=inspections') {
					await expect(table.locator('.badge.partial')).toBeVisible();
				}
				if (href === '/admin/webhooks') {
					await expect(page.getByTestId('overlap-pill')).toBeVisible();
					await expect(page.locator('.deliveries-table .badge.dead')).toBeVisible();
				}
				if (href === '/adaptive') {
					for (const { status } of SUGGESTION_PER_STATUS) {
						await expect(table.locator(`.badge.${status}`)).toBeVisible();
					}
					await expect(table.locator('td.actions button')).toHaveCount(2);
				}
				if (label === '/admin?tab=roles custom roles') {
					await expect(table.locator('td.actions button')).toHaveCount(2);
				}
				if (href === '/discounts') {
					for (const status of DISCOUNT_STATUSES) {
						await expect(table.locator(`.badge.${status}`)).toBeVisible();
					}
				}
				if (href === '/tax') {
					await expect(table.locator('.badge.chip-warn')).toHaveCount(4);
				}
				if (href === '/admin/partner') {
					await expect(table.locator('tbody tr')).toHaveCount(PARTNER_OVERVIEW.children.length);
					// Arm the first Detach: its confirm label is the widest thing an
					// actions cell holds (fr "Confirmer le détachement").
					await table.locator('td.actions button').first().click();
					await expect(table.locator('td.actions button.armed')).toHaveCount(1);
				}
				if (label === '/payments cards') {
					for (const status of CARD_STATUSES) {
						await expect(table.locator(`.badge.${status}`)).toBeVisible();
					}
				}
				await nextFrames(page);

				expect(await overflowingCells(table), `cells wider than their column on ${href}`).toEqual([]);
			});
		}
	}
});
