import type { Page } from '@playwright/test';

import {
	API_BASE,
	authedTenantHeaders,
	currentTenantSlug,
	deleteInvoicesWhere,
	expect,
	signInAndWait,
	tenantBase,
	tenantPsql,
	test
} from '../fixtures/helpers';
import { SERVICES, skipUnlessReachable } from '../fixtures/services';
import {
	FAKE_ERP_BASE,
	type Inv,
	createApprovedInvoice,
	erpReferenceFromAudit,
	getInvoice,
	loginAsSecondActor,
	qboStatusSyncEnabled,
	sendToErpAndAwaitTerminal,
	setErpSettings,
	syncErpGlAccounts,
	syncErpVendors
} from './helpers';

/**
 * NetSuite's status sync-back, end to end against the fake ERP (decisions
 * §536, §541, §547). NetSuite has no webhook and no OAuth connection: the
 * tenant reaches it with stored credentials and opts in per tenant
 * (`settings.erp.netsuite_status_sync`), and a pass is the sweep's own,
 * triggered here the way an admin triggers it: the ERP panel's "Check
 * payments now" button, drawn for a stored-credential ERP from the status
 * read's verdict (§551), which posts `POST
 * /api/organization/erp/oauth/status-sync` (not OAuth-only since §547). The
 * pass runs after the 202; its end is the `organization.erp_status_synced`
 * audit row it writes, which the spec waits on (never a timer).
 *
 *   1. A payment a person keyed in NetSuite (`POST /__netsuite/apply`, a
 *      `VendPymt` for the whole bill) moves the invoice, left payable at
 *      `posted_in_erp` by the push, to `paid`, booked at exactly the cash
 *      NetSuite states; a partial one leaves the invoice payable and opens
 *      the payment-blocking `payment_reconciliation` exception instead.
 *   2. FeohLedger pays an invoice through a run, its write-back lands in
 *      NetSuite as `feohledger-payment-<id>`, and a person deletes it there.
 *   3. The next pass changes nothing: the paid and flagged invoices stay as
 *      they were, and the deleted write-back is neither flagged nor posted
 *      again. REST lists no deleted record, so §536 accepts this residual: a
 *      write-back is retried only until it first succeeds.
 *   4. Without the opt-in a push closes at `done` as before §536, the panel
 *      offers no check, and the on-request pass is refused
 *      `status_sync_unsupported`.
 *
 * The on-request pass is rate limited to four an hour per tenant. The e2e
 * backend runs with the limiter off (`FEOH_RATE_LIMIT_ENABLED=false`, which
 * CI's `erp-e2e` job sets and `frontend/tests-e2e/README.md` requires
 * locally), so a retry or a re-run is never answered 429. A backend started
 * with the limiter on fails this spec on its third pass within the hour, and
 * the assertion says why.
 *
 * Needs the backend started with `FEOH_ERP_STATUS_SYNC_ENABLED=true` (CI's
 * `erp-e2e` job sets it); the spec skips, saying so, when status sync is off.
 */

const NETSUITE_ERP_CONFIG = {
	type: 'netsuite',
	integration_method: 'direct',
	account_id: 'FAKE123',
	consumer_key: 'fake-consumer-key',
	consumer_secret: 'fake-consumer-secret',
	token_id: 'fake-token-id',
	token_secret: 'fake-token-secret',
	// The GL code the write-back pays from, mapped to its NetSuite id by the
	// chart sync. fake-erp's chart has no bank account and does not check the
	// account's type, so an expense code stands in for one.
	bill_payment_bank_account: '6200'
};
const OPTED_IN = { ...NETSUITE_ERP_CONFIG, netsuite_status_sync: 'on' };
const VENDOR = 'Fake NetSuite Vendor A';

/** The fake checks a TBA header's shape, not its signature: enough to read
 *  and delete a record the way a person in NetSuite would. */
const NETSUITE_AUTH = {
	Authorization: 'OAuth oauth_consumer_key="e2e", oauth_token="e2e", oauth_signature="e2e"'
};
const NETSUITE_RECORDS = `${FAKE_ERP_BASE}/netsuite/services/rest/record/v1`;

function today(): string {
	return new Date().toISOString().slice(0, 10);
}

/** First non-empty line of a `psql -tA` answer. */
function firstLine(out: string): string {
	return out.split('\n').map((l) => l.trim()).filter(Boolean)[0] ?? '';
}

function syncedPasses(): number {
	return Number(
		firstLine(
			tenantPsql(`SELECT count(*) FROM audit_log WHERE action='organization.erp_status_synced'`)
		)
	);
}

type PassOutcome = {
	outcome: string;
	provider?: string;
	refusal?: string;
	error?: string;
	invoices_changed?: number;
};

/** Open the ERP section and wait for its status read, which carries the
 *  payment-check verdict the panel draws the button from (§551). */
async function openErpPanel(page: Page): Promise<{ status_sync_refusal: string | null }> {
	const statusRead = page.waitForResponse(
		(r) =>
			r.url().endsWith('/api/organization/erp/oauth/status') && r.request().method() === 'GET'
	);
	await page.goto('/organization?section=erp');
	const resp = await statusRead;
	expect(resp.status()).toBe(200);
	await expect(page.getByTestId('erp-provider-select')).toHaveValue('netsuite');
	return (await resp.json()) as { status_sync_refusal: string | null };
}

/** Ask for an on-request pass the way an admin does, with the panel's
 *  button, and wait for the audit row the pass ends with. */
async function checkNow(page: Page): Promise<PassOutcome> {
	const before = syncedPasses();
	await openErpPanel(page);
	const button = page.getByTestId('erp-check-payments');
	await expect(button).toBeEnabled();
	const answered = page.waitForResponse(
		(r) =>
			r.url().endsWith('/api/organization/erp/oauth/status-sync') &&
			r.request().method() === 'POST'
	);
	await button.click();
	const resp = await answered;
	expect(
		resp.status(),
		resp.status() === 429
			? 'the on-request limiter is on: start the e2e backend with FEOH_RATE_LIMIT_ENABLED=false'
			: await resp.text()
	).toBe(202);
	expect(((await resp.json()) as { started: boolean }).started).toBe(true);
	await expect(page.getByTestId('erp-status')).toContainText(
		'Checking Oracle NetSuite for payments made there.'
	);
	await expect
		.poll(syncedPasses, { message: 'the on-request pass never finished' })
		.toBeGreaterThan(before);
	return JSON.parse(
		firstLine(
			tenantPsql(
				`SELECT details FROM audit_log WHERE action='organization.erp_status_synced' ` +
					`ORDER BY created_at DESC LIMIT 1`
			)
		)
	) as PassOutcome;
}

/** A person in NetSuite applies a payment or credit to a bill. */
async function applyInNetSuite(billId: string, amount: string): Promise<void> {
	const resp = await fetch(`${FAKE_ERP_BASE}/__netsuite/apply`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ bill: billId, type: 'VendPymt', amount, tranDate: today() })
	});
	expect(resp.ok, `apply in NetSuite (${resp.status})`).toBe(true);
}

async function postedBill(page: Page, prefix: string, amount: string) {
	const inv = await createApprovedInvoice(page, {
		prefix,
		amount,
		vendor: VENDOR,
		glAccount: '6100'
	});
	// Opted in, with status sync on: a confirmed push leaves the bill payable.
	expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('posted_in_erp');
	return { inv, billId: await erpReferenceFromAudit(page, inv.id) };
}

/** Completed payments booked against an invoice: `[amount, provider]`. */
function payments(invoiceId: string): string[][] {
	return tenantPsql(
		`SELECT amount::text || '|' || coalesce(provider, '') FROM payments ` +
			`WHERE invoice_id='${invoiceId}' AND status='completed' ORDER BY created_at`
	)
		.split('\n')
		.map((l) => l.trim())
		.filter(Boolean)
		.map((l) => l.split('|'));
}

/** Open exceptions of `type` on the invoice (`GET /api/exceptions`). */
async function openExceptions(page: Page, inv: Inv, type: string): Promise<number> {
	const resp = await page.request.get(`${API_BASE}/api/exceptions`, {
		headers: await authedTenantHeaders(page),
		params: { type, search: inv.invoice_number }
	});
	expect(resp.status()).toBe(200);
	const body = (await resp.json()) as {
		items?: { invoice_id: string | null; status: string }[];
	};
	return (body.items ?? []).filter(
		(e) => e.invoice_id === inv.id && (e.status === 'open' || e.status === 'escalated')
	).length;
}

/** Our write-back as NetSuite holds it, by its externalId; null once gone. */
async function writeBack(paymentId: string): Promise<{ apply: { amount: unknown }[] } | null> {
	const resp = await fetch(`${NETSUITE_RECORDS}/vendorPayment/eid:feohledger-payment-${paymentId}`, {
		headers: NETSUITE_AUTH
	});
	if (resp.status === 404) return null;
	expect(resp.status, 'read the write-back in NetSuite').toBe(200);
	return (await resp.json()) as { apply: { amount: unknown }[] };
}

async function afterAllReset(browser: import('@playwright/test').Browser): Promise<void> {
	// Everything this spec created hangs off its own `E2E-NSS-*` invoices: the
	// helper takes their payments, exceptions and workflow rows, and the
	// payment run that paid only them (scoped to this worker's tenant).
	deleteInvoicesWhere(`invoice_number LIKE 'E2E-NSS-%'`);
	// Other suites assume the seeded org has no ERP configured.
	const context = await browser.newContext({ baseURL: tenantBase(currentTenantSlug()) });
	const page = await context.newPage();
	await signInAndWait(page);
	await setErpSettings(page, null);
	await context.close();
}

test.describe('/erp netsuite status sync-back against fake-erp', () => {
	// The tests share the bills they push and the passes they run: a re-run
	// is a pass over what the first one settled.
	test.describe.configure({ mode: 'serial' });

	let paid: Inv;
	let partial: Inv;
	let ours: Inv;
	let oursPaymentId: string;

	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		test.skip(
			!(await qboStatusSyncEnabled(page)),
			'Status sync is off — start the backend with FEOH_ERP_STATUS_SYNC_ENABLED=true'
		);
		await setErpSettings(page, OPTED_IN);
	});

	test.afterAll(async ({ browser }) => {
		await afterAllReset(browser);
	});

	test("a person's payment in NetSuite is booked at its exact amount; a partial one is flagged", async ({
		page
	}) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const whole = await postedBill(page, 'E2E-NSS-PAID', '612.34');
		const part = await postedBill(page, 'E2E-NSS-PART', '400.00');
		paid = whole.inv;
		partial = part.inv;
		await applyInNetSuite(whole.billId, '612.34');
		await applyInNetSuite(part.billId, '150.00');

		const pass = await checkNow(page);
		expect(pass.outcome, JSON.stringify(pass)).toBe('completed');
		expect(pass.provider).toBe('netsuite');

		expect((await getInvoice(page, paid.id)).status).toBe('paid');
		// One payment, recorded as made outside FeohLedger, at exactly the
		// cash NetSuite states.
		expect(payments(paid.id)).toEqual([['612.34', 'external']]);

		// Part-paid: still payable here, nothing booked, and no run pays it in
		// full while a person reconciles it.
		expect((await getInvoice(page, partial.id)).status).toBe('posted_in_erp');
		expect(payments(partial.id)).toEqual([]);
		expect(await openExceptions(page, partial, 'payment_reconciliation')).toBe(1);
	});

	test('our write-back deleted in NetSuite', async ({ page }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const posted = await postedBill(page, 'E2E-NSS-OURS', '275.50');
		ours = posted.inv;

		// FeohLedger pays it: the admin builds the run, the CFO executes it
		// (maker-checker), and the payment sync marks it paid and writes the
		// payment back to the bill.
		const headers = await authedTenantHeaders(page);
		const created = await page.request.post(`${API_BASE}/api/payments/runs`, {
			headers,
			data: { items: [{ invoice_id: ours.id, method: 'ach' }] }
		});
		expect(created.status(), `create run: ${await created.text()}`).toBe(201);
		const runId = ((await created.json()) as { id: string }).id;
		const executed = await page.request.post(`${API_BASE}/api/payments/runs/${runId}/execute`, {
			headers: await loginAsSecondActor(page)
		});
		expect(executed.status(), `execute run: ${await executed.text()}`).toBe(200);
		await expect
			.poll(async () => (await getInvoice(page, ours.id)).status, {
				message: 'the run never paid the invoice'
			})
			.toBe('paid');
		const run = await page.request.get(`${API_BASE}/api/payments/runs/${runId}`, { headers });
		oursPaymentId = ((await run.json()) as { payments: { id: string }[] }).payments[0].id;

		await expect
			.poll(async () => (await writeBack(oursPaymentId))?.apply.map((a) => Number(a.amount)), {
				message: 'the payment was never written back to NetSuite'
			})
			.toEqual([275.5]);

		// A person deletes our payment in NetSuite: the bill is open again there.
		const deleted = await fetch(
			`${NETSUITE_RECORDS}/vendorPayment/eid:feohledger-payment-${oursPaymentId}`,
			{ method: 'DELETE', headers: NETSUITE_AUTH }
		);
		expect(deleted.status, 'delete the write-back in NetSuite').toBe(204);
		expect(await writeBack(oursPaymentId)).toBeNull();
	});

	test('a re-run changes nothing, and a deleted write-back is not posted again', async ({
		page
	}) => {
		const pass = await checkNow(page);
		expect(pass.outcome, JSON.stringify(pass)).toBe('completed');
		expect(pass.invoices_changed).toBe(0);

		expect((await getInvoice(page, paid.id)).status).toBe('paid');
		expect(payments(paid.id)).toEqual([['612.34', 'external']]);

		expect((await getInvoice(page, partial.id)).status).toBe('posted_in_erp');
		expect(await openExceptions(page, partial, 'payment_reconciliation')).toBe(1);

		// §536's accepted residual: REST lists no deleted record, so the bill
		// reads open on an invoice FeohLedger paid, which changes nothing here
		// (the invoice stays paid, by its one payment, unflagged), and the
		// write-back is not retried after it first succeeded.
		expect((await getInvoice(page, ours.id)).status).toBe('paid');
		expect(payments(ours.id)).toHaveLength(1);
		expect(await openExceptions(page, ours, 'payment_reconciliation')).toBe(0);
		expect(await openExceptions(page, ours, 'erp_reconciliation')).toBe(0);
		expect(await writeBack(oursPaymentId)).toBeNull();
	});
});

test.describe('/erp netsuite without the status-sync opt-in', () => {
	test.beforeEach(async ({ page }) => {
		await skipUnlessReachable(SERVICES.fakeErp);
		test.skip(
			!(await qboStatusSyncEnabled(page)),
			'Status sync is off — start the backend with FEOH_ERP_STATUS_SYNC_ENABLED=true'
		);
		await setErpSettings(page, NETSUITE_ERP_CONFIG);
	});

	test.afterAll(async ({ browser }) => {
		await afterAllReset(browser);
	});

	test('a push closes at done as before, and no pass runs on request', async ({ page }) => {
		await syncErpVendors(page);
		await syncErpGlAccounts(page);
		const inv = await createApprovedInvoice(page, {
			prefix: 'E2E-NSS-OFF',
			amount: '88.20',
			vendor: VENDOR,
			glAccount: '6100'
		});
		expect(await sendToErpAndAwaitTerminal(page, inv.id)).toBe('done');

		// The panel offers no check: the status read states the POST's refusal,
		// and a refused verdict hides the button (§551). The opted-in tests
		// above are the positive control: the same read draws it.
		expect((await openErpPanel(page)).status_sync_refusal).toBe('status_sync_unsupported');
		await expect(page.getByTestId('erp-check-payments')).toHaveCount(0);

		const resp = await page.request.post(`${API_BASE}/api/organization/erp/oauth/status-sync`, {
			headers: await authedTenantHeaders(page)
		});
		expect(resp.status()).toBe(409);
		expect(((await resp.json()) as { detail: { code: string } }).detail.code).toBe(
			'status_sync_unsupported'
		);
	});
});
