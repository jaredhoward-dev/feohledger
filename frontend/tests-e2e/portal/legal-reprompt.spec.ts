import type { Page, Route } from '@playwright/test';

import { acceptConsent, expect, tenantPsql, test } from '../fixtures/helpers';
import { PRIVACY_VERSION } from '#lib/legal/legalVersions.generated.ts';

/**
 * The supplier portal's Privacy Policy re-prompt (`layout/LegalAcceptStep`
 * with `documents="privacy"`, `docs/decisions.md` §499, §500).
 *
 * While `GET /api/portal/auth/me` says `legal_acceptance_required`, the portal
 * layout draws the acknowledgement step in place of the portal shell: no
 * header, no nav, no page (so none of the page's reads run), the URL left as
 * asked. Ticking the box and continuing posts the Privacy Policy version this
 * build carries to `POST /api/portal/auth/legal-acknowledgement`, which answers
 * with the profile; the shell then renders the page the supplier was going to.
 *
 * The first test drives the real endpoint: the seeded supplier's
 * acknowledgement row (`scripts/seed.py` records one for every seeded portal
 * user, source `seed`) is removed for the test and restored after, the same
 * flip-and-restore `portal.spec.ts` uses for `must_change_password`. The
 * others stub only what a live backend cannot be made to answer on demand —
 * the stale-versions 422 and the per-account 429 — over the real `/me` with
 * the flag forced on, so the seeded row is never touched.
 */

const PORTAL_EMAIL = 'supplier@portal.test';
const PORTAL_PASSWORD = 'demo';
const STEP = '[data-testid="legal-accept-step"]';
const HEADING = "Confirm you have read the platform's Privacy Policy";
const ACK_BOX = /I have read the Privacy Policy/;
const CONTINUE = 'Confirm and continue';

test.use({ storageState: { cookies: [], origins: [] } });

function isAckPost(url: string, method: string): boolean {
	return new URL(url).pathname === '/api/portal/auth/legal-acknowledgement' && method === 'POST';
}

/** Sign in on the portal form; lands wherever the login page sends a supplier. */
async function portalSignIn(page: Page) {
	await acceptConsent(page);
	await page.goto('/portal/login');
	await page.locator('input[type="email"]').fill(PORTAL_EMAIL);
	await page.locator('input[type="password"]').fill(PORTAL_PASSWORD);
	await page.locator('button[type="submit"]').click();
	await expect(page).toHaveURL(/\/portal\/?$/);
}

/** The real portal `/me`, with `legal_acceptance_required` forced on. */
async function forceStep(page: Page): Promise<void> {
	await page.route(
		(url) => url.pathname === '/api/portal/auth/me',
		async (route: Route) => {
			const real = await route.fetch();
			const body = (await real.json()) as Record<string, unknown>;
			await route.fulfill({ response: real, json: { ...body, legal_acceptance_required: true } });
		}
	);
}

async function stubAck(page: Page, status: number, detail: unknown): Promise<void> {
	await page.route(
		(url) => url.pathname === '/api/portal/auth/legal-acknowledgement',
		(route) => route.fulfill({ status, json: { detail } })
	);
}

function seededVendorUserId(): string {
	const id = tenantPsql(`SELECT id FROM vendor_users WHERE email='${PORTAL_EMAIL}'`).trim();
	expect(id, 'no seeded portal user').not.toEqual('');
	return id;
}

test.describe('Supplier portal — Privacy Policy re-prompt', () => {
	test('an unacknowledged supplier is held at the step, cannot reach the page behind it, and continues there', async ({
		page
	}) => {
		const vuId = seededVendorUserId();
		tenantPsql(`DELETE FROM vendor_user_legal_acceptances WHERE vendor_user_id='${vuId}'`);
		try {
			let pageReads = 0;
			page.on('request', (r) => {
				if (new URL(r.url()).pathname.startsWith('/api/portal/invoices')) pageReads++;
			});
			await portalSignIn(page);
			await page.goto('/portal/invoices');

			const step = page.locator(STEP);
			await expect(step).toBeVisible();
			await expect(page).toHaveURL(/\/portal\/invoices$/);
			await expect(page.getByRole('heading', { level: 1, name: HEADING })).toBeFocused();
			// Nothing of the portal behind it: no header, no nav, no page read.
			await expect(page.locator('.portal-header')).toHaveCount(0);
			await expect(page.getByRole('navigation', { name: 'Supplier portal' })).toHaveCount(0);
			expect(pageReads).toBe(0);
			// No Terms: a Supplier User is not a party to them (§499).
			await expect(step.getByRole('link', { name: 'Terms of Service' })).toHaveCount(0);

			// A reload does not get past it either.
			await page.reload();
			await expect(step).toBeVisible();
			await expect(page.locator('.portal-header')).toHaveCount(0);

			// Continue without ticking: the box is marked and focused, no request.
			let posts = 0;
			page.on('request', (r) => {
				if (isAckPost(r.url(), r.method())) posts++;
			});
			await page.getByRole('button', { name: CONTINUE }).click();
			const box = page.getByRole('checkbox', { name: ACK_BOX });
			await expect(box).toBeFocused();
			await expect(box).toHaveAttribute('aria-invalid', 'true');
			await expect(step.getByRole('alert')).toHaveText(
				'Tick the box to confirm you have read the Privacy Policy.'
			);
			expect(posts).toBe(0);

			await box.check();
			const acknowledged = page.waitForResponse((r) => isAckPost(r.url(), r.request().method()));
			await page.getByRole('button', { name: CONTINUE }).click();
			const resp = await acknowledged;
			expect(resp.status()).toBe(200);
			expect(resp.request().postDataJSON()).toEqual({ accepted_privacy_version: PRIVACY_VERSION });

			// The page the supplier asked for, at the address they asked for.
			await expect(step).toHaveCount(0);
			await expect(page.locator('.portal-header')).toBeVisible();
			await expect(page).toHaveURL(/\/portal\/invoices$/);

			// Recorded server-side: a reload does not ask again.
			await page.reload();
			await expect(page.locator('.portal-header')).toBeVisible();
			await expect(page.locator(STEP)).toHaveCount(0);
			expect(
				tenantPsql(
					`SELECT source FROM vendor_user_legal_acceptances WHERE vendor_user_id='${vuId}' AND document='privacy'`
				).trim()
			).toBe('reprompt');
		} finally {
			// Back to the seeded state, whatever the test reached.
			tenantPsql(
				`INSERT INTO vendor_user_legal_acceptances (id, vendor_user_id, document, version, source) ` +
					`VALUES (gen_random_uuid(), '${vuId}', 'privacy', '${PRIVACY_VERSION}', 'seed') ` +
					`ON CONFLICT ON CONSTRAINT uq_vendor_user_legal_acceptances_doc_version DO NOTHING`
			);
		}
	});

	test('a ticked box refused as stale offers the reload, not another tick', async ({ page }) => {
		await forceStep(page);
		await stubAck(page, 422, {
			code: 'privacy_acknowledgement_required',
			message: 'Please confirm you have read the current Privacy Policy to continue.',
			params: {}
		});
		await portalSignIn(page);
		await expect(page.locator(STEP)).toBeVisible();

		const box = page.getByRole('checkbox', { name: ACK_BOX });
		await box.check();
		await page.getByRole('button', { name: CONTINUE }).click();

		const changed = page.locator(STEP).getByRole('alert');
		await expect(changed.getByRole('button', { name: 'Reload page' })).toBeVisible();
		await expect(box).toBeFocused();
		await expect(page.locator('.portal-header')).toHaveCount(0);
	});

	test('a rate-limited acknowledgement says how long to wait and keeps the step', async ({
		page
	}) => {
		await forceStep(page);
		await stubAck(page, 429, {
			code: 'rate_limited',
			message: 'Too many requests. Please wait 45 seconds before trying again.',
			params: { retry_after_seconds: 45 }
		});
		await portalSignIn(page);
		await page.getByRole('checkbox', { name: ACK_BOX }).check();
		await page.getByRole('button', { name: CONTINUE }).click();

		await expect(page.locator(STEP).getByRole('alert')).toHaveText(
			'Too many attempts. Wait 45 seconds and try again.'
		);
		await expect(page.locator('.portal-header')).toHaveCount(0);
	});

	test('a tenant with a product name and no logo is named on the step, with its support link', async ({
		page
	}) => {
		const SUPPORT = 'https://support.e2e.test/help';
		// The public brand read answered here, so the worker tenant's brand is
		// never written.
		await page.route(
			(url) => url.pathname === '/api/portal/branding',
			(route) =>
				route.fulfill({
					json: {
						product_name: 'Monogram Payables',
						logo_url: '',
						accent_color: '',
						accent_strong_color: '',
						support_url: SUPPORT,
						legal_url: ''
					}
				})
		);
		await forceStep(page);
		await portalSignIn(page);

		const step = page.locator(STEP);
		await expect(step.getByTestId('legal-step-product')).toHaveText('Monogram Payables');
		// The heading names the platform's policy, not the tenant's.
		await expect(page.getByRole('heading', { level: 1, name: HEADING })).toBeVisible();
		const support = step.getByRole('link', { name: 'Questions? Contact support' });
		await expect(support).toHaveAttribute('href', SUPPORT);
		await expect(support).toHaveAttribute('target', '_blank');
	});

	test('on the platform brand the step shows no product name and no support link', async ({
		page
	}) => {
		await page.route(
			(url) => url.pathname === '/api/portal/branding',
			(route) =>
				route.fulfill({
					json: {
						product_name: '',
						logo_url: '',
						accent_color: '',
						accent_strong_color: '',
						support_url: '',
						legal_url: ''
					}
				})
		);
		await forceStep(page);
		await portalSignIn(page);

		const step = page.locator(STEP);
		await expect(page.getByRole('heading', { level: 1, name: HEADING })).toBeVisible();
		await expect(step.getByTestId('legal-step-product')).toHaveCount(0);
		await expect(step.getByTestId('legal-step-support')).toHaveCount(0);
	});

	test('Log out leaves the step for the portal sign-in page', async ({ page }) => {
		await forceStep(page);
		await portalSignIn(page);
		await expect(page.locator(STEP)).toBeVisible();
		await page.getByRole('button', { name: 'Log Out' }).click();
		await page.waitForURL(/\/portal\/login/);
	});
});
