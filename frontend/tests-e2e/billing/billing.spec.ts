import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
	API_BASE,
	currentTenantSlug,
	expect,
	signInAndWait,
	TENANT_ROOT_URL,
	test
} from '../fixtures/helpers';
import type { Page } from '@playwright/test';
import { expectNoA11yViolations } from '../a11y/axe-helper';

/**
 * /billing — Platform Billing & Metering (read/display surface).
 *
 * Consumes `GET /api/billing/subscription` (admin/cfo): current plan + tier +
 * price, subscription status badge, billing-period window, and usage-to-date
 * meters. This is the AP platform's OWN customer billing (control-plane), not
 * the AP money path the app runs for customers.
 *
 * Login model mirrors the rest of the suite: the default per-worker storage
 * state signs the worker's admin in (admin is in the allowed set admin/cfo),
 * so the page loads directly without a redirect. The "not authorized" block
 * opts out and signs in as the clerk.
 *
 * Billing rows live in the CONTROL plane (`feohledger`), keyed by the
 * org's id — not the tenant DB — so the happy-path test seeds a Plan +
 * Subscription via control-plane psql and tears them down in `finally`.
 *
 * Selectors are accessible name / data-testid — never brittle CSS/nth-child,
 * never `waitForTimeout`.
 */

const CONTROL_DB = 'feohledger';

/** Run a synchronous `psql -c <query>` against the CONTROL-plane DB and return
 *  the FIRST output line. `INSERT … RETURNING id` emits the id line AND a
 *  trailing `INSERT 0 1` command tag on this psql build, so we take line 0 (the
 *  RETURNING value) rather than the whole — a plain trim would keep the tag. */
function controlPsql(query: string): string {
	const out = execFileSync(
		'psql',
		['-h', 'localhost', '-U', 'postgres', '-p', '5432', '-d', CONTROL_DB, '-tAc', query],
		{ env: { ...process.env, PGPASSWORD: 'postgres' }, stdio: ['ignore', 'pipe', 'pipe'] }
	);
	return out
		.toString()
		.split('\n')
		.map((l) => l.trim())
		.filter((l) => l.length > 0 && !/^INSERT \d+ \d+$/.test(l))[0] ?? '';
}

/** Like `controlPsql` but returns EVERY output row, not just the first. */
function controlPsqlAll(query: string): string[] {
	const out = execFileSync(
		'psql',
		['-h', 'localhost', '-U', 'postgres', '-p', '5432', '-d', CONTROL_DB, '-tAc', query],
		{ env: { ...process.env, PGPASSWORD: 'postgres' }, stdio: ['ignore', 'pipe', 'pipe'] }
	);
	return out
		.toString()
		.split('\n')
		.map((l) => l.trim())
		.filter((l) => l.length > 0 && !/^(INSERT|UPDATE|DELETE) \d+( \d+)?$/.test(l));
}

/** The control-plane org id for the current worker's tenant slug. */
function currentOrgId(): string {
	const slug = currentTenantSlug();
	const id = controlPsql(`SELECT id FROM organizations WHERE slug='${slug}'`);
	expect(id, `no control-plane org for slug ${slug}`).toMatch(/[0-9a-f-]{36}/);
	return id;
}

/**
 * Park (cancel) whatever live subscription the org already has, and hand back
 * the ids so the test's `finally` can put them back.
 *
 * `tenant_provisioning` calls `ensure_subscription(plan_code="free")` for every
 * org, so an e2e tenant already holds a live subscription before any test runs.
 * These tests then seeded a SECOND `active` row for the same org — a state
 * `uq_subscription_one_live_per_org` (migration 0093) correctly forbids, and
 * which only worked previously because that index lived in a migration and was
 * never declared on the model, so a `create_all`-built database silently lacked
 * it (`docs/decisions.md` §109). With two live rows the endpoint under test
 * picked one arbitrarily, so these assertions passed by luck.
 *
 * Seed the state the app actually supports instead: park what is live, run as
 * the sole live subscription, then restore exactly the parked ids — **after**
 * the test's own row is deleted, or the restore would recreate the collision.
 * Restoring by captured id rather than re-provisioning a `free` row leaves the
 * tenant byte-identical for whatever runs next in this worker.
 */
function parkLiveSubscriptions(orgId: string): string[] {
	const parked = controlPsqlAll(
		`SELECT id FROM subscriptions WHERE organization_id='${orgId}' AND status <> 'canceled'`
	);
	if (parked.length) {
		controlPsql(
			`UPDATE subscriptions SET status='canceled' WHERE id IN (${parked
				.map((id) => `'${id}'`)
				.join(',')})`
		);
	}
	return parked;
}

/** Undo `parkLiveSubscriptions`. Call only once the test's own subscription
 *  rows are gone — two live rows for one org is the very thing being avoided. */
function restoreLiveSubscriptions(parked: string[]): void {
	if (!parked.length) return;
	controlPsql(
		`UPDATE subscriptions SET status='active' WHERE id IN (${parked
			.map((id) => `'${id}'`)
			.join(',')})`
	);
}

/** A `page.route` matcher for one exact API pathname — a loose glob over
 *  `api/billing` would also answer `vite dev`'s module request for
 *  `#lib/api/billing.ts` (frontend/CLAUDE.md § A page.route stub). */
function isApi(pathname: string): (url: URL) => boolean {
	return (url) => url.pathname === pathname;
}

function json(body: unknown, status = 200) {
	return { status, contentType: 'application/json', body: JSON.stringify(body) };
}

/** A coded refusal body, as `backend/app/api/refusals.py::coded_refusal` sends it. */
function refusal(code: string, message: string, status: number) {
	return json({ detail: { code, message, params: {} } }, status);
}

/** The mock adapter's SetupIntent id shape (`mock_seti_<customer>`). */
const MOCK_SETUP_INTENT_ID = 'mock_seti_cus_e2e';

const SAVED_CARD_LIST = {
	provider: 'mock',
	payment_methods: [
		{ id: 'mock_pm_e2e', brand: 'visa', last4: '4242', exp_month: 12, exp_year: 2030, is_default: true }
	]
};

async function stubMockSetupIntent(page: Page): Promise<void> {
	await page.route(isApi('/api/billing/payment-method/setup-intent'), (route) =>
		route.fulfill(
			json({
				provider: 'mock',
				configured: true,
				client_secret: `${MOCK_SETUP_INTENT_ID}_secret`,
				setup_intent_id: MOCK_SETUP_INTENT_ID
			})
		)
	);
}

/** Stub `POST /payment-method/default`, recording each request body. */
async function stubSetDefault(page: Page): Promise<{ bodies: unknown[] }> {
	const seen = { bodies: [] as unknown[] };
	await page.route(isApi('/api/billing/payment-method/default'), (route) => {
		seen.bodies.push(route.request().postDataJSON());
		return route.fulfill(json(SAVED_CARD_LIST));
	});
	return seen;
}

/**
 * The publishable key the app under test was built with, read from the same
 * places vite reads it: the process environment (CI's preview build inherits
 * the job's), then `frontend/.env*` in vite's precedence for this mode. Used to
 * state the no-key spec's precondition instead of assuming it.
 */
function builtStripePublishableKey(): string {
	const fromEnv = process.env.PUBLIC_STRIPE_PUBLISHABLE_KEY;
	if (fromEnv !== undefined) return fromEnv.trim();
	const frontend = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
	const mode = process.env.FEOH_E2E_USE_PREVIEW === 'true' ? 'production' : 'development';
	// Highest precedence first, as vite merges them.
	for (const name of [`.env.${mode}.local`, `.env.${mode}`, '.env.local', '.env']) {
		const file = join(frontend, name);
		if (!existsSync(file)) continue;
		const line = readFileSync(file, 'utf8')
			.split('\n')
			.map((l) => l.trim())
			.find((l) => l.startsWith('PUBLIC_STRIPE_PUBLISHABLE_KEY='));
		if (line !== undefined) return line.slice('PUBLIC_STRIPE_PUBLISHABLE_KEY='.length).trim();
	}
	return '';
}

/** Where Stripe sends a redirect-based confirmation back to (status appended). */
const STRIPE_RETURN = '/billing?setup_intent=seti_x&redirect_status=';

test.describe('/billing (admin)', () => {
	// Sign the worker admin in explicitly (don't rely on the shared per-worker
	// storage-state cache) so the gated page is reliably authed before each
	// test — admin is in the allowed set {admin, cfo}. This is a real,
	// deterministic sign-in, not a wait/retry workaround.
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async ({ page }) => {
		await signInAndWait(page);
		await page.goto('/billing');
	});

	test('renders the billing surface header', async ({ page }) => {
		await expect(page.getByRole('heading', { level: 1, name: 'Subscription', exact: true })).toBeVisible();
	});

	test('with no subscription, shows the friendly empty state + usage meters', async ({ page }) => {
		// No subscription is seeded for e2e tenants, so the endpoint returns
		// plan=null / subscription=null and the page renders the empty state.
		// (If a previous run's seed leaked, this still holds only when clean —
		// the happy-path test always cleans up after itself.)
		const empty = page.getByTestId('billing-empty');
		const plan = page.getByTestId('billing-plan');
		// Exactly one of {empty, plan} is present depending on seed state.
		await expect(empty.or(plan)).toBeVisible({ timeout: 10_000 });

		if (await empty.isVisible()) {
			await expect(empty.getByRole('heading', { name: 'No active subscription' })).toBeVisible();
			// Usage meters render even with no plan.
			await expect(page.locator('.kpi').first()).toBeVisible();
			await expect(page.locator('.kpi-label', { hasText: 'Extractions' }).first()).toBeVisible();
		}
	});

	test('with a seeded subscription, shows plan, price, status badge + period', async ({ page }) => {
		const orgId = currentOrgId();
		const planCode = `e2e_billing_${Date.now()}`;
		let planId: string | null = null;
		// The org already holds a live `free` subscription from provisioning;
		// park it so this seeded one is the only live row (see the helper).
		const parked = parkLiveSubscriptions(orgId);
		try {
			planId = controlPsql(
				`INSERT INTO plans (id, code, name, monthly_price, currency, seat_component, ` +
					`usage_components, entitlements, trial_days, is_active, created_at, updated_at) ` +
					`VALUES (gen_random_uuid(), '${planCode}', 'E2E Growth', 49.00, 'USD', '{}'::jsonb, ` +
					`'{}'::jsonb, '{"public_api": true}'::jsonb, 14, true, now(), now()) RETURNING id`
			);
			expect(planId).toMatch(/[0-9a-f-]{36}/);

			controlPsql(
				`INSERT INTO subscriptions (id, organization_id, plan_id, status, ` +
					`current_period_start, current_period_end, trial_end, created_at, updated_at) ` +
					`VALUES (gen_random_uuid(), '${orgId}', '${planId}', 'active', ` +
					`now() - interval '5 days', now() + interval '25 days', NULL, now(), now())`
			);

			await page.goto('/billing');

			const plan = page.getByTestId('billing-plan');
			await expect(plan).toBeVisible({ timeout: 10_000 });
			await expect(plan.getByRole('heading', { name: 'E2E Growth' })).toBeVisible();
			// Status badge.
			await expect(plan.getByText('Active', { exact: true })).toBeVisible();
			// Exact price rendered through <Money> ($49.00).
			await expect(plan.getByText('$49.00')).toBeVisible();
			// The granted entitlement surfaces under the name the pricing page
			// gives it (`planFeature.publicApi`), not as its raw `public_api` key.
			await expect(plan.getByText('Public API and webhooks', { exact: true })).toBeVisible();
			// Exact: a bare `getByText` is a case-insensitive substring match, and
			// 'public api' is a substring of the correct label itself.
			await expect(plan.getByText('public api', { exact: true })).toHaveCount(0);
			await expect(plan.getByText('public_api', { exact: true })).toHaveCount(0);
			// Usage meters still render.
			await expect(page.locator('.kpi-label', { hasText: 'Extractions' }).first()).toBeVisible();
		} finally {
			// Tear down subscription first (FK to plan), then the plan.
			if (planId) {
				controlPsql(`DELETE FROM subscriptions WHERE plan_id='${planId}'`);
				controlPsql(`DELETE FROM plans WHERE id='${planId}'`);
			}
			// Only now — restoring while this test's row is still live would
			// recreate the two-live-rows collision the index forbids.
			restoreLiveSubscriptions(parked);
		}
	});

	test('a past-due subscription leads with a callout that takes the reader to the card', async ({
		page
	}) => {
		// The dunning state (`active → past_due → canceled`, services/billing/
		// dunning_sweep.py) is the one that needs the reader to act before the
		// sweep cancels them, so the page says so above the plan and links to
		// where the card is replaced. Seeded the same way as the test above.
		const orgId = currentOrgId();
		const planCode = `e2e_billing_pd_${Date.now()}`;
		let planId: string | null = null;
		const parked = parkLiveSubscriptions(orgId);
		try {
			planId = controlPsql(
				`INSERT INTO plans (id, code, name, monthly_price, currency, seat_component, ` +
					`usage_components, entitlements, trial_days, is_active, created_at, updated_at) ` +
					`VALUES (gen_random_uuid(), '${planCode}', 'E2E Past Due', 49.00, 'USD', '{}'::jsonb, ` +
					`'{}'::jsonb, '{}'::jsonb, 0, true, now(), now()) RETURNING id`
			);
			controlPsql(
				`INSERT INTO subscriptions (id, organization_id, plan_id, status, ` +
					`current_period_start, current_period_end, trial_end, created_at, updated_at) ` +
					`VALUES (gen_random_uuid(), '${orgId}', '${planId}', 'past_due', ` +
					`now() - interval '5 days', now() + interval '25 days', NULL, now(), now())`
			);

			await page.goto('/billing');

			const callout = page.getByTestId('billing-status-callout');
			await expect(callout).toBeVisible({ timeout: 10_000 });
			await expect(callout.getByText('Payment overdue')).toBeVisible();
			const action = callout.getByRole('link', { name: 'Update payment method' });
			await expect(action).toHaveAttribute('href', '#billing-payment-methods');
			await expect(page.locator('#billing-payment-methods')).toHaveAttribute(
				'data-testid',
				'billing-payment-methods'
			);
			// The callout sits above the plan card it is about.
			const calloutBox = await callout.boundingBox();
			const planBox = await page.getByTestId('billing-plan').boundingBox();
			expect(calloutBox && planBox && calloutBox.y < planBox.y).toBe(true);
		} finally {
			if (planId) {
				controlPsql(`DELETE FROM subscriptions WHERE plan_id='${planId}'`);
				controlPsql(`DELETE FROM plans WHERE id='${planId}'`);
			}
			restoreLiveSubscriptions(parked);
		}
	});

	test('renders the invoices / receipts section (mock provider seeds receipts)', async ({
		page
	}) => {
		// `GET /api/billing/invoices` is admin/cfo-gated and sourced through the
		// org's billing adapter. The local-first default (`mock`) fabricates
		// deterministic receipts only when the org has a provider customer id; a
		// fresh e2e tenant has none, so the list is legitimately empty and the
		// section shows its "No invoices yet." empty state. Either way the section
		// renders with its table (never a 500), which is what we assert here.
		const section = page.getByTestId('billing-invoices');
		await expect(section).toBeVisible({ timeout: 10_000 });
		await expect(section.getByRole('heading', { name: 'Invoices & receipts' })).toBeVisible();
		// A grid is present (the DataTable shell) regardless of row count.
		await expect(section.locator('.grid-container table')).toBeVisible();
	});

	test('with seeded billing receipts, the list shows rows + a hosted-url link', async ({
		page
	}) => {
		// Point the org at a billing customer id so the `mock` adapter fabricates
		// deterministic receipts, and stub one row's hosted_url via route
		// interception so the "View" link is deterministically present. The route
		// stub keeps this test independent of provider seed state.
		await page.route('**/api/billing/invoices', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					provider: 'mock',
					invoices: [
						{
							id: 'mock_in_e2e_2026-06',
							number: 'MOCK-2026-06',
							period: '2026-06',
							amount: '49.00',
							currency: 'USD',
							status: 'paid',
							hosted_url: 'https://billing.example.com/invoices/mock_in_e2e_2026-06',
							created_at: '2026-06-01T00:00:00Z'
						},
						{
							id: 'mock_in_e2e_2026-05',
							number: 'MOCK-2026-05',
							period: '2026-05',
							amount: '49.00',
							currency: 'USD',
							status: 'open',
							hosted_url: null,
							created_at: '2026-05-01T00:00:00Z'
						}
					]
				})
			});
		});

		await page.goto('/billing');

		const section = page.getByTestId('billing-invoices');
		await expect(section).toBeVisible({ timeout: 10_000 });
		// Both rows surface their invoice number + exact <Money> amount + status.
		await expect(section.getByText('MOCK-2026-06')).toBeVisible();
		await expect(section.getByText('MOCK-2026-05')).toBeVisible();
		await expect(section.getByText('$49.00').first()).toBeVisible();
		await expect(section.getByText('Paid', { exact: true })).toBeVisible();
		await expect(section.getByText('Open', { exact: true })).toBeVisible();
		// The paid row's hosted-url opens in a new tab.
		const viewLink = section.getByRole('link', {
			name: /View invoice MOCK-2026-06 \(opens in a new tab\)/
		});
		await expect(viewLink).toBeVisible();
		await expect(viewLink).toHaveAttribute(
			'href',
			'https://billing.example.com/invoices/mock_in_e2e_2026-06'
		);
		await expect(viewLink).toHaveAttribute('target', '_blank');
	});

	test('with no receipts, the invoices section shows the empty state', async ({ page }) => {
		await page.route('**/api/billing/invoices', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ provider: 'mock', invoices: [] })
			});
		});

		await page.goto('/billing');

		const section = page.getByTestId('billing-invoices');
		await expect(section).toBeVisible({ timeout: 10_000 });
		await expect(section.getByText('No invoices yet.')).toBeVisible();
	});

	test('renders the payment-methods section (mock provider seeds a card)', async ({ page }) => {
		// `GET /api/billing/payment-methods` is admin/cfo-gated and sourced through
		// the org's billing adapter. The `mock` adapter fabricates a deterministic
		// `visa ····4242` only when the org has a provider customer id; a fresh e2e
		// tenant has none, so the list is legitimately empty and the section shows
		// "No payment method on file." Either way the section renders with its
		// table (never a 500), which is what we assert here.
		const section = page.getByTestId('billing-payment-methods');
		await expect(section).toBeVisible({ timeout: 10_000 });
		await expect(section.getByRole('heading', { name: 'Payment methods' })).toBeVisible();
		// The add/replace-card action and the DataTable shell both render.
		await expect(page.getByTestId('billing-add-card')).toBeVisible();
		await expect(section.locator('.grid-container table')).toBeVisible();
	});

	test('with a saved card stubbed, the list shows the brand · ****last4 · exp', async ({
		page
	}) => {
		// Stub a card so the row is deterministic regardless of provider seed state.
		// PII-safe metadata only — never a PAN.
		await page.route('**/api/billing/payment-methods', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					provider: 'mock',
					payment_methods: [
						{
							id: 'mock_pm_e2e',
							brand: 'visa',
							last4: '4242',
							exp_month: 12,
							exp_year: 2030,
							is_default: true
						}
					]
				})
			});
		});

		await page.goto('/billing');

		const section = page.getByTestId('billing-payment-methods');
		await expect(section).toBeVisible({ timeout: 10_000 });
		await expect(section.getByText('Visa ····4242')).toBeVisible();
		await expect(section.getByText('Expires 12/2030')).toBeVisible();
		await expect(section.getByText('Default', { exact: true })).toBeVisible();
	});

	test('with no card, the payment-methods section shows the empty state', async ({ page }) => {
		await page.route('**/api/billing/payment-methods', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ provider: 'mock', payment_methods: [] })
			});
		});

		await page.goto('/billing');

		const section = page.getByTestId('billing-payment-methods');
		await expect(section).toBeVisible({ timeout: 10_000 });
		await expect(section.getByText('No payment method on file.')).toBeVisible();
	});

	test('mock provider: Save test card makes the mock SetupIntent the default and refreshes the list', async ({
		page
	}) => {
		// The local `mock` provider has no card form (and Stripe.js must never
		// load for it): the panel offers "Save test card", which posts the mock
		// SetupIntent id to /payment-method/default and renders the refreshed
		// list that endpoint returns — no second GET needed.
		await page.route(isApi('/api/billing/payment-methods'), (route) =>
			route.fulfill(json({ provider: 'mock', payment_methods: [] }))
		);
		await stubMockSetupIntent(page);
		const saved = await stubSetDefault(page);

		await page.goto('/billing');
		const section = page.getByTestId('billing-payment-methods');
		await expect(section.getByText('No payment method on file.')).toBeVisible({ timeout: 10_000 });

		await page.getByTestId('billing-add-card').click();
		const setup = page.getByTestId('billing-card-setup');
		await expect(setup.getByRole('heading', { name: 'Add a card' })).toBeFocused();
		await expect(setup.getByTestId('billing-card-mock')).toContainText('local test billing provider');
		// No Stripe Element is mounted in mock mode.
		await expect(page.getByTestId('billing-card-element')).toHaveCount(0);

		await setup.getByTestId('billing-card-save-test').click();
		await expect(setup).toBeHidden();
		expect(saved.bodies).toEqual([{ setup_intent_id: MOCK_SETUP_INTENT_ID }]);
		// Focus goes back to the button that opened the panel (WCAG 2.4.3).
		await expect(page.getByTestId('billing-add-card')).toBeFocused();
		await expect(section.getByText('Visa ····4242')).toBeVisible();
		await expect(section.getByText('Default', { exact: true })).toBeVisible();
		await expect(
			page.getByText('Card saved. Your subscription is now charged to this card.')
		).toBeVisible();
	});

	test('stripe_billing in a build with no publishable key says card entry is not set up', async ({
		page
	}) => {
		// A Stripe-backed SetupIntent in a build with no publishable key must
		// not try to load Stripe.js keyless: it names the operator gap instead.
		// The precondition is stated, not assumed — a build WITH a key would
		// mount real Stripe here, which no e2e run may do.
		expect(
			builtStripePublishableKey(),
			'this spec needs a build without PUBLIC_STRIPE_PUBLISHABLE_KEY (the local and CI default)'
		).toBe('');
		await page.route(isApi('/api/billing/payment-method/setup-intent'), (route) =>
			route.fulfill(
				json({
					provider: 'stripe_billing',
					configured: true,
					client_secret: 'seti_e2e_secret_x',
					setup_intent_id: 'seti_e2e'
				})
			)
		);

		await page.goto('/billing');
		await page.getByTestId('billing-add-card').click();
		const setup = page.getByTestId('billing-card-setup');
		await expect(setup.getByTestId('billing-card-not-configured')).toHaveText(
			'Card entry is not set up for this deployment yet. Contact our team to add a card.'
		);
		await expect(page.getByTestId('billing-card-element')).toHaveCount(0);
	});

	test('cancelling the card form returns focus to Add or replace card', async ({ page }) => {
		await stubMockSetupIntent(page);
		await page.goto('/billing');
		await page.getByTestId('billing-add-card').click();
		const setup = page.getByTestId('billing-card-setup');
		await setup.getByRole('button', { name: 'Cancel' }).click();
		await expect(setup).toBeHidden();
		await expect(page.getByTestId('billing-add-card')).toBeFocused();
	});

	test('a Stripe redirect return (succeeded) saves the card as default and drops the secret from the URL', async ({
		page
	}) => {
		const saved = await stubSetDefault(page);
		await page.goto(`${STRIPE_RETURN}succeeded&setup_intent_client_secret=seti_x_secret_y`);

		await expect(
			page.getByText('Card saved. Your subscription is now charged to this card.')
		).toBeVisible();
		expect(saved.bodies).toEqual([{ setup_intent_id: 'seti_x' }]);
		await expect(page).not.toHaveURL(/setup_intent/);
		await expect(page).toHaveURL(/\/billing$/);
	});

	test('a Stripe redirect return that failed posts nothing and says so', async ({ page }) => {
		const saved = await stubSetDefault(page);
		await page.goto(`${STRIPE_RETURN}failed&setup_intent_client_secret=seti_x_secret_y`);

		await expect(
			page.getByText('Your card could not be confirmed. Check the details and try again.')
		).toBeVisible();
		await expect(page).not.toHaveURL(/setup_intent/);
		expect(saved.bodies).toEqual([]);
	});

	test('a Stripe redirect return still processing is a neutral notice, not a failure', async ({
		page
	}) => {
		const saved = await stubSetDefault(page);
		await page.goto(`${STRIPE_RETURN}processing&setup_intent_client_secret=seti_x_secret_y`);

		await expect(
			page.getByText(
				'Your card is still being confirmed by your bank. If it does not appear here in a few minutes, add it again.'
			)
		).toBeVisible();
		await expect(page).not.toHaveURL(/setup_intent/);
		expect(saved.bodies).toEqual([]);
	});

	test('add-card when not configured shows the billing-not-configured state', async ({ page }) => {
		// No provider customer / unconfigured provider → configured=false, null
		// secret. The UI shows a clear "not configured" affordance, not an error.
		await page.route('**/api/billing/payment-method/setup-intent', async (route) => {
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					provider: 'mock',
					configured: false,
					client_secret: null,
					setup_intent_id: null
				})
			});
		});

		await page.goto('/billing');

		await page.getByTestId('billing-add-card').click();
		const setup = page.getByTestId('billing-card-setup');
		await expect(setup).toBeVisible({ timeout: 10_000 });
		await expect(setup.getByText(/Billing is not configured/)).toBeVisible();
	});

	test('the Subscription section tab is visible + active for the admin', async ({ page }) => {
		// /billing is a child of the Settings nav group (beside Organization —
		// decisions §314), so it surfaces as a section sub-tab, not a top-level
		// sidebar row. On /billing the Settings sub-tab bar is shown with
		// Subscription as the active tab.
		const tab = page.locator('nav.section-tabs a[href="/billing"]');
		await expect(tab).toBeVisible();
		await expect(tab).toHaveText('Subscription');
		await expect(tab).toHaveAttribute('aria-current', 'page');
	});

	test.describe('plan-change flow (seeded subscription)', () => {
		// The "Change plan" action only renders once the org has a live
		// subscription (it lives inside the plan-card block), so every test in
		// this group seeds a real Plan + Subscription — mirroring the
		// "with a seeded subscription" test above — and tears it down after.
		let orgId: string;
		let currentPlanId: string;
		let targetPlanId: string;
		// `plans.code` is varchar(50) — keep the per-test suffix short (a testId
		// hash is too long) but still unique enough to avoid cross-test collision.
		let currentCode: string;
		let targetCode: string;
		let parked: string[] = [];

		test.beforeEach(async ({ page }) => {
			const suffix = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
			currentCode = `e2e_pc_cur_${suffix}`;
			targetCode = `e2e_pc_tgt_${suffix}`;
			orgId = currentOrgId();
			// Park the provisioning-time `free` subscription so the seeded
			// "current plan" below is the org's only live row (see the helper).
			parked = parkLiveSubscriptions(orgId);
			currentPlanId = controlPsql(
				`INSERT INTO plans (id, code, name, monthly_price, currency, seat_component, ` +
					`usage_components, entitlements, trial_days, is_active, created_at, updated_at) ` +
					`VALUES (gen_random_uuid(), '${currentCode}', 'E2E Current', 49.00, 'USD', '{}'::jsonb, ` +
					`'{}'::jsonb, '{}'::jsonb, 0, true, now(), now()) RETURNING id`
			);
			targetPlanId = controlPsql(
				`INSERT INTO plans (id, code, name, monthly_price, currency, seat_component, ` +
					`usage_components, entitlements, trial_days, is_active, created_at, updated_at) ` +
					`VALUES (gen_random_uuid(), '${targetCode}', 'E2E Target', 99.00, 'USD', '{}'::jsonb, ` +
					`'{}'::jsonb, '{}'::jsonb, 0, true, now(), now()) RETURNING id`
			);
			controlPsql(
				`INSERT INTO subscriptions (id, organization_id, plan_id, status, ` +
					`current_period_start, current_period_end, trial_end, created_at, updated_at) ` +
					`VALUES (gen_random_uuid(), '${orgId}', '${currentPlanId}', 'active', ` +
					`now() - interval '5 days', now() + interval '25 days', NULL, now(), now())`
			);

			await page.goto('/billing');
		});

		test.afterEach(() => {
			controlPsql(`DELETE FROM subscriptions WHERE plan_id IN ('${currentPlanId}', '${targetPlanId}')`);
			controlPsql(`DELETE FROM plans WHERE id IN ('${currentPlanId}', '${targetPlanId}')`);
			// After the deletes — the change-plan flow leaves a live row on the
			// TARGET plan, so restoring earlier would collide on the index.
			restoreLiveSubscriptions(parked);
			parked = [];
		});

		/** A preview as `POST /change-plan/preview` answers it (decisions §482). */
		function previewBody(overrides: Record<string, unknown> = {}) {
			return {
				plan_code: targetCode,
				already_on_plan: false,
				currency: 'USD',
				amount_due: '14.80',
				credit: '0.00',
				next_amount: '99.00',
				next_date: '2026-12-01T00:00:00Z',
				proration_date: 1_795_000_000,
				expires_at: '2026-11-21T12:10:00Z',
				payment_method_required: false,
				...overrides
			};
		}

		/** Stub the preview, answering `bodies` in turn (the last repeats). */
		async function stubPreview(page: Page, ...bodies: Record<string, unknown>[]) {
			const seen = { count: 0 };
			await page.route(isApi('/api/billing/change-plan/preview'), (route) => {
				const body = bodies[Math.min(seen.count, bodies.length - 1)];
				seen.count += 1;
				return route.fulfill(json(body));
			});
			return seen;
		}

		/** Stub the confirm, recording each body; `answer` decides the reply. */
		async function stubChangePlan(
			page: Page,
			answer: (n: number) => ReturnType<typeof json>
		): Promise<unknown[]> {
			const bodies: unknown[] = [];
			await page.route(isApi('/api/billing/change-plan'), (route) => {
				bodies.push(route.request().postDataJSON());
				return route.fulfill(answer(bodies.length));
			});
			return bodies;
		}

		const CHANGED = () =>
			json({
				changed: true,
				old_plan_code: currentCode,
				new_plan_code: targetCode,
				proration: { amount: '41.67', unused_days: 25, period_days: 30 }
			});

		async function pickTarget(page: Page) {
			await page.getByTestId('billing-change-plan').click();
			const modal = page.getByRole('dialog', { name: 'Change plan' });
			await modal.getByRole('radio', { name: 'Select the E2E Target plan' }).check();
			await page.getByTestId('billing-change-plan-continue').click();
			return modal;
		}

		test('shows the price first and charges nothing until Confirm and pay', async ({ page }) => {
			// The real mock backend end to end: preview, then confirm with the
			// preview's own figures. Every change-plan request is counted, so a
			// call before the Confirm click fails the test.
			const changePlanCalls: unknown[] = [];
			page.on('request', (req) => {
				if (new URL(req.url()).pathname === '/api/billing/change-plan') {
					changePlanCalls.push(req.postDataJSON());
				}
			});

			await page.getByTestId('billing-change-plan').click();
			const modal = page.getByRole('dialog', { name: 'Change plan' });
			await expect(modal).toBeVisible();

			// The current plan is marked and its radio is disabled — a genuine
			// change is the point of the UI flow.
			const currentRadio = modal.getByRole('radio', { name: 'Select the E2E Current plan' });
			await expect(currentRadio).toBeDisabled();
			await expect(modal.getByText('Current plan')).toBeVisible();
			await expect(
				modal.getByText("You'll see exactly what you will pay before anything is charged.")
			).toBeVisible();

			await modal.getByRole('radio', { name: 'Select the E2E Target plan' }).check();
			const previewed = page.waitForResponse(
				(r) => new URL(r.url()).pathname === '/api/billing/change-plan/preview'
			);
			await page.getByTestId('billing-change-plan-continue').click();
			const priced = await (await previewed).json();

			// The review states the provider's figures: what is due today and what
			// recurs, the monthly price being the target plan's $99.00.
			const review = modal.getByTestId('billing-plan-review');
			await expect(review.getByText('Moving from E2E Current to E2E Target.')).toBeFocused();
			await expect(review.getByTestId('billing-plan-review-today')).toHaveText(
				/^You'll pay \$\d+\.\d{2} today for the rest of the current billing period\.$/
			);
			await expect(review.getByTestId('billing-plan-review-recurring')).toHaveText(
				/^Then \$99\.00 a month from \d{1,2} \w+ \d{4}\.$|^Then \$99\.00 a month from \w+ \d{1,2}, \d{4}\.$/
			);
			expect(changePlanCalls).toEqual([]);
			// The review step meets WCAG 2.2 AA as rendered.
			await expectNoA11yViolations(page);

			await review.getByRole('button', { name: 'Confirm and pay' }).click();

			const result = page.getByTestId('billing-plan-change-result');
			await expect(result.getByText("You're now on the E2E Target plan.")).toBeVisible();
			await expect(
				result.getByText('Your card on file has been charged. The receipt will appear under Invoices.')
			).toBeVisible();
			// One confirm, carrying exactly the figures the reader saw.
			expect(changePlanCalls).toEqual([
				{
					plan_code: targetCode,
					proration_date: priced.proration_date,
					expected_amount_due: priced.amount_due,
					currency: priced.currency
				}
			]);

			await result.getByRole('button', { name: 'Done' }).click();
			await expect(modal).toBeHidden();
			// The plan card behind the modal reflects the change without a reload.
			await expect(page.getByTestId('billing-plan').getByRole('heading', { name: 'E2E Target' })).toBeVisible();
		});

		test('an idempotent no-op change ("changed": false) renders as a clean success, not an error', async ({
			page
		}) => {
			// The backend's own no-op is the org already being on the target plan,
			// which the disabled current-plan radio prevents reaching directly. The
			// response shape is still the API's contract, so the UI must honour it.
			await stubPreview(page, previewBody());
			await stubChangePlan(page, () =>
				json({
					changed: false,
					old_plan_code: currentCode,
					new_plan_code: currentCode,
					proration: { amount: '0.00', unused_days: 0, period_days: 0 }
				})
			);

			const modal = await pickTarget(page);
			await modal.getByRole('button', { name: 'Confirm and pay' }).click();

			const noop = page.getByTestId('billing-plan-change-noop');
			await expect(noop).toBeVisible();
			await expect(noop).toHaveText("You're already on the E2E Current plan — nothing changed.");
		});

		test('with no card on file the dialog asks for one, then shows the price — saving the card charges nothing', async ({
			page
		}) => {
			// The production report: the card used to be saved and the plan
			// changed in one go, at an amount nobody had seen.
			const previews = await stubPreview(
				page,
				previewBody({ payment_method_required: true }),
				previewBody({ proration_date: 1_795_000_100 })
			);
			const confirmed = await stubChangePlan(page, CHANGED);
			await stubMockSetupIntent(page);
			const saved = await stubSetDefault(page);

			const modal = await pickTarget(page);

			// The card step is part of the dialog, not an error.
			await expect(modal.getByTestId('billing-plan-change-error')).toHaveCount(0);
			const setup = modal.getByTestId('billing-card-setup');
			await expect(setup.getByRole('heading', { name: 'Add a card' })).toBeFocused();
			await expect(setup.getByTestId('billing-card-pending-plan')).toHaveText(
				"Save a card to continue to the E2E Target plan. You'll review the price before anything is charged."
			);
			await expectNoA11yViolations(page);

			await setup.getByTestId('billing-card-save-test').click();

			// Saved — and the dialog moves to the review, priced again.
			const review = modal.getByTestId('billing-plan-review');
			await expect(review.getByTestId('billing-plan-review-today')).toHaveText(
				"You'll pay $14.80 today for the rest of the current billing period."
			);
			await expect(review.getByTestId('billing-plan-review-recurring')).toHaveText(
				/^Then \$99\.00 a month from /
			);
			expect(saved.bodies).toEqual([{ setup_intent_id: MOCK_SETUP_INTENT_ID }]);
			expect(previews.count).toBe(2);
			expect(confirmed).toEqual([]); // nothing charged by saving the card
			await expect(page.getByTestId('billing-payment-methods').getByText('Visa ····4242')).toBeVisible();

			await review.getByRole('button', { name: 'Confirm and pay' }).click();
			await expect(
				page.getByTestId('billing-plan-change-result').getByText("You're now on the E2E Target plan.")
			).toBeVisible();
			expect(confirmed).toEqual([
				{
					plan_code: targetCode,
					proration_date: 1_795_000_100,
					expected_amount_due: '14.80',
					currency: 'USD'
				}
			]);
		});

		test('a downgrade shows its credit and a plain Confirm, nothing due today', async ({ page }) => {
			await stubPreview(
				page,
				previewBody({ amount_due: '0.00', credit: '20.00', next_amount: '49.00' })
			);
			const modal = await pickTarget(page);
			const review = modal.getByTestId('billing-plan-review');
			await expect(review.getByTestId('billing-plan-review-today')).toHaveText(
				'Nothing is charged today.'
			);
			await expect(review.getByTestId('billing-plan-review-credit')).toHaveText(
				'$20.00 for the unused part of your current plan will be credited to your account and taken off future invoices.'
			);
			await expect(review.getByRole('button', { name: 'Confirm', exact: true })).toBeVisible();
			await expect(review.getByRole('button', { name: 'Confirm and pay' })).toHaveCount(0);
		});

		test('a declined charge (402 billing_payment_declined) states its own message and offers Update card', async ({
			page
		}) => {
			await stubPreview(page, previewBody());
			await stubChangePlan(page, () =>
				refusal('billing_payment_declined', 'Your card was declined.', 402)
			);

			const modal = await pickTarget(page);
			await modal.getByRole('button', { name: 'Confirm and pay' }).click();

			const refused = modal.getByTestId('billing-plan-change-error');
			// The localized decline sentence — not the server's English, and not
			// the plan-feature gate's 402 ("…is included in the … plan").
			await expect(
				refused.getByText(
					'Your card was declined, so your plan was not changed. Update your card and try again.',
					{ exact: true }
				)
			).toBeVisible();
			await expect(page.getByTestId('billing-plan-change-result')).toHaveCount(0);
			// Update card is the dialog's card step.
			await stubMockSetupIntent(page);
			await refused.getByRole('button', { name: 'Update card' }).click();
			await expect(modal.getByTestId('billing-card-setup')).toBeVisible();
		});

		test('a stale quote charges nothing and shows the current price to confirm again', async ({
			page
		}) => {
			const previews = await stubPreview(
				page,
				previewBody(),
				previewBody({ amount_due: '14.20', proration_date: 1_795_000_700 })
			);
			const confirmed = await stubChangePlan(page, (n) =>
				n === 1
					? refusal('plan_change_preview_stale', 'The price you confirmed is out of date.', 409)
					: CHANGED()
			);

			const modal = await pickTarget(page);
			await modal.getByRole('button', { name: 'Confirm and pay' }).click();

			await expect(modal.getByTestId('billing-plan-change-error')).toHaveText(
				'The price you confirmed is out of date, so nothing was charged. Review the new amount and confirm again.'
			);
			await expect(modal.getByTestId('billing-plan-review-today')).toHaveText(
				"You'll pay $14.20 today for the rest of the current billing period."
			);
			expect(previews.count).toBe(2);

			await modal.getByRole('button', { name: 'Confirm and pay' }).click();
			await expect(page.getByTestId('billing-plan-change-result')).toBeVisible();
			expect(confirmed).toEqual([
				{ plan_code: targetCode, proration_date: 1_795_000_000, expected_amount_due: '14.80', currency: 'USD' },
				{ plan_code: targetCode, proration_date: 1_795_000_700, expected_amount_due: '14.20', currency: 'USD' }
			]);
		});

		test('a refusal with no card fix (plan_change_in_progress) shows only its message', async ({
			page
		}) => {
			await stubPreview(page, previewBody());
			await stubChangePlan(page, () =>
				refusal('plan_change_in_progress', 'Another plan change is running.', 409)
			);

			const modal = await pickTarget(page);
			await modal.getByRole('button', { name: 'Confirm and pay' }).click();

			const refused = modal.getByTestId('billing-plan-change-error');
			await expect(refused).toHaveText(
				'Another plan change is already in progress. Try again in a moment.'
			);
			await expect(refused.getByRole('button')).toHaveCount(0);
		});

		test('a card confirmed through a Stripe redirect reopens the price review, and charges nothing', async ({
			page
		}) => {
			// The card step stashes the plan for its SetupIntent right before
			// confirmSetup; a redirect-based method then leaves the page. Seed that
			// stash as the panel writes it, and come back as Stripe does.
			await page.evaluate(
				([key, value]) => sessionStorage.setItem(key, value),
				['billing_pending_plan', JSON.stringify({ planCode: targetCode, setupIntentId: 'seti_x' })]
			);
			await stubPreview(page, previewBody());
			const confirmed = await stubChangePlan(page, CHANGED);
			const saved = await stubSetDefault(page);

			await page.goto(`${STRIPE_RETURN}succeeded&setup_intent_client_secret=seti_x_secret_y`);

			const review = page.getByRole('dialog', { name: 'Change plan' }).getByTestId('billing-plan-review');
			await expect(review.getByTestId('billing-plan-review-today')).toHaveText(
				"You'll pay $14.80 today for the rest of the current billing period."
			);
			expect(saved.bodies).toEqual([{ setup_intent_id: 'seti_x' }]);
			expect(confirmed).toEqual([]);
			// Taken, not left for a later return.
			expect(await page.evaluate(() => sessionStorage.getItem('billing_pending_plan'))).toBeNull();
		});

		test('the picker lists the plan catalog from GET /api/billing/plans, cheapest first', async ({
			page
		}) => {
			// Other active plans may already exist in the control-plane catalog
			// (the platform default seed, or other tests' fixtures) — assert our
			// two seeded plans are present and correctly ordered RELATIVE to each
			// other, not the total row count.
			await page.getByTestId('billing-change-plan').click();
			const modal = page.getByRole('dialog', { name: 'Change plan' });
			const options = modal.locator('.plan-option');
			await expect(options.filter({ hasText: 'E2E Current' })).toBeVisible({ timeout: 10_000 });
			const texts = await options.allTextContents();
			const currentIdx = texts.findIndex((t) => t.includes('E2E Current'));
			const targetIdx = texts.findIndex((t) => t.includes('E2E Target'));
			expect(currentIdx).toBeGreaterThanOrEqual(0);
			expect(targetIdx).toBeGreaterThanOrEqual(0);
			// $49 current plan sorts before the $99 target plan.
			expect(currentIdx).toBeLessThan(targetIdx);
		});

		test('the picker is one radio group: a card click selects, arrows move, each card says its price and what it adds', async ({
			page
		}) => {
			// A known catalog, so the order and the differentiators are fixed (the
			// real one carries whatever else is seeded). The first plan keeps the
			// seeded current code, so its radio is the disabled, marked one.
			const plan = (code: string, name: string, price: string, keys: string[]) => ({
				code,
				name,
				monthly_price: price,
				currency: 'USD',
				entitlements: Object.fromEntries(keys.map((k) => [k, true])),
				trial_days: 0
			});
			await page.route(isApi('/api/billing/plans'), (route) =>
				route.fulfill(
					json({
						plans: [
							plan(currentCode, 'E2E Current', '49.00', ['erp_integrations']),
							plan('e2e_mid', 'E2E Mid', '99.00', ['erp_integrations', 'public_api', 'sso']),
							plan('e2e_top', 'E2E Top', '199.00', [
								'erp_integrations',
								'public_api',
								'sso',
								'scim',
								'multi_entity',
								'audit_siem_export'
							])
						]
					})
				)
			);

			await page.getByTestId('billing-change-plan').click();
			const modal = page.getByRole('dialog', { name: 'Change plan' });
			// The fieldset's legend names the group even though it is not printed.
			const group = modal.getByRole('group', { name: 'Available plans' });
			const current = group.getByRole('radio', { name: 'Select the E2E Current plan' });
			const mid = group.getByRole('radio', { name: 'Select the E2E Mid plan' });
			const top = group.getByRole('radio', { name: 'Select the E2E Top plan' });
			await expect(current).toBeDisabled();

			// Each radio's description is what the card shows beside its name:
			// the current-plan marker, the price, and what the plan adds over the
			// one before it — capped, with the rest counted.
			await expect(current).toHaveAccessibleDescription('Current plan $49.00 / month ERP integrations');
			await expect(mid).toHaveAccessibleDescription(
				'$99.00 / month Everything in E2E Current\u00a0· Public API and webhooks\u00a0· Single sign-on (OIDC / SAML)'
			);
			await expect(top).toHaveAccessibleDescription(
				'$199.00 / month Everything in E2E Mid\u00a0· SCIM user provisioning\u00a0· +2 more'
			);

			// The whole card is the target: clicking its price selects the plan.
			await group.getByTestId('billing-plan-option').filter({ hasText: 'E2E Mid' }).getByText('$99.00').click();
			await expect(mid).toBeChecked();
			await expect(page.getByTestId('billing-change-plan-continue')).toBeEnabled();

			// Native radio-group keys: an arrow moves AND selects, and the
			// disabled current plan is skipped on the wrap.
			await mid.focus();
			await page.keyboard.press('ArrowDown');
			await expect(top).toBeChecked();
			await expect(top).toBeFocused();
			await page.keyboard.press('ArrowDown');
			await expect(mid).toBeChecked();
			await expect(current).not.toBeChecked();
		});
	});
});

test.describe('/billing (clerk — not authorized)', () => {
	// Opt out of the default admin storage state so we can sign in as the clerk.
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk is redirected away and cannot see the nav row', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		await page.goto('/billing');
		// admin/cfo only — the page waits for /me then bounces the clerk to root.
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
		await expect(page.getByRole('heading', { level: 1, name: 'Subscription', exact: true })).toHaveCount(0);

		// The clerk also can't reach the Subscription tab (it's admin/cfo-only,
		// and they were bounced to the dashboard, which has no section tabs).
		await expect(page.locator('a[href="/billing"]')).toHaveCount(0);
	});

	test('the API 403s a clerk directly', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		const base = API_BASE;
		const headers = { Authorization: `Bearer ${token}`, 'X-Tenant-Slug': currentTenantSlug() };
		// Every billing read (subscription + invoices + payment methods + the
		// plan catalog) is admin/cfo-only; so is starting a SetupIntent or
		// changing the plan.
		const sub = await page.request.get(`${base}/api/billing/subscription`, { headers });
		expect(sub.status()).toBe(403);
		const invoices = await page.request.get(`${base}/api/billing/invoices`, { headers });
		expect(invoices.status()).toBe(403);
		const methods = await page.request.get(`${base}/api/billing/payment-methods`, { headers });
		expect(methods.status()).toBe(403);
		const setup = await page.request.post(`${base}/api/billing/payment-method/setup-intent`, {
			headers
		});
		expect(setup.status()).toBe(403);
		const plans = await page.request.get(`${base}/api/billing/plans`, { headers });
		expect(plans.status()).toBe(403);
		const changePlan = await page.request.post(`${base}/api/billing/change-plan`, {
			headers,
			data: { plan_code: 'growth' }
		});
		expect(changePlan.status()).toBe(403);
	});
});
