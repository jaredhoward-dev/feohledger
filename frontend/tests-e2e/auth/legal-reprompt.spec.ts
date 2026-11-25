import type { Page, Route } from '@playwright/test';
import {
	API_BASE,
	authedTenantHeaders,
	createUserWithKnownPassword,
	expect,
	signInAndWait,
	signOut,
	test
} from '../fixtures/helpers';

/**
 * The blocking Terms / Privacy re-prompt (`layout/LegalAcceptStep`,
 * `docs/decisions.md` §495, §497).
 *
 * While `GET /api/auth/me` says `legal_acceptance_required`, the root layout
 * draws the accept step in place of the app shell: no sidebar, no page, the
 * URL left as asked. Ticking the box and continuing posts the versions this
 * build carries to `POST /api/auth/legal-acceptance`, which answers with the
 * account; the shell then renders the page the user was going to.
 *
 * The first test drives the real endpoint with a real unaccepted account
 * (`createUserWithKnownPassword(…, { acceptLegal: false })` — every seeded and
 * helper-created account is otherwise recorded as accepted). The others stub
 * only the two answers a live backend cannot be made to give on demand: the
 * versions-changed 422 (needs a deploy between page load and submit) and the
 * per-account 429; `/me` is the real response with the flag forced on, so the
 * worker admin's acceptance rows are never touched.
 */

const STEP = '[data-testid="legal-accept-step"]';
const HEADING = 'Review and accept our terms';
const ACCEPT_BOX = /I agree to the Terms of Service/;

function isAcceptPost(url: string, method: string): boolean {
	return new URL(url).pathname === '/api/auth/legal-acceptance' && method === 'POST';
}

/** The real `/me`, with `legal_acceptance_required` forced on. */
async function forceStep(page: Page): Promise<void> {
	await page.route(
		(url) => url.pathname === '/api/auth/me',
		async (route: Route) => {
			const real = await route.fetch();
			const body = (await real.json()) as Record<string, unknown>;
			await route.fulfill({ response: real, json: { ...body, legal_acceptance_required: true } });
		}
	);
}

async function stubAccept(page: Page, status: number, detail: unknown): Promise<void> {
	await page.route(
		(url) => url.pathname === '/api/auth/legal-acceptance',
		(route) => route.fulfill({ status, json: { detail } })
	);
}

test.describe('Terms / Privacy re-prompt', () => {
	test.describe('an account with no acceptance', () => {
		test.use({ storageState: { cookies: [], origins: [] } });

		test('is held at the step, cannot reach the page behind it, and continues there once it accepts', async ({
			page
		}) => {
			await signInAndWait(page);
			const created = await createUserWithKnownPassword(page, {
				mustChange: false,
				emailPrefix: 'e2e-legal',
				acceptLegal: false
			});
			try {
				await signOut(page);
				// A deep link: the sign-in carries it as ?next=, and the step must
				// keep it rather than send the user to the dashboard afterwards.
				await page.goto('/invoices');
				await page.waitForURL(/\/login\?next=/);
				// Filled in place (not `signIn`, which reloads a bare /login and
				// would drop the ?next= this test is about).
				await page.locator('input[type="email"]').fill(created.email);
				await page.locator('input[type="password"]').fill(created.password);
				await page.locator('form button[type="submit"]').click();

				const step = page.locator(STEP);
				await expect(step).toBeVisible();
				await expect(page).toHaveURL(/\/invoices$/);
				// Focus lands on the step's heading (WCAG 2.4.3).
				await expect(page.getByRole('heading', { level: 1, name: HEADING })).toBeFocused();
				// Nothing of the app behind it is rendered: no shell, no page.
				await expect(page.locator('.app-shell')).toHaveCount(0);
				await expect(page.locator('.profile-btn')).toHaveCount(0);

				// A reload does not get past it either.
				await page.reload();
				await expect(step).toBeVisible();
				await expect(page.locator('.app-shell')).toHaveCount(0);

				// Continue without ticking: the box is marked and focused, no request.
				let posts = 0;
				page.on('request', (r) => {
					if (isAcceptPost(r.url(), r.method())) posts++;
				});
				await page.getByRole('button', { name: 'Accept and continue' }).click();
				const box = page.getByRole('checkbox', { name: ACCEPT_BOX });
				await expect(box).toBeFocused();
				await expect(box).toHaveAttribute('aria-invalid', 'true');
				await expect(step.getByRole('alert')).toHaveText(
					'Tick the box to accept the Terms of Service and Privacy Policy.'
				);
				expect(posts).toBe(0);

				// Log out is the next stop by keyboard after the accept button.
				await page.getByRole('button', { name: 'Accept and continue' }).focus();
				await page.keyboard.press('Tab');
				await expect(page.getByRole('button', { name: 'Log Out' })).toBeFocused();

				await box.check();
				const accepted = page.waitForResponse((r) =>
					isAcceptPost(r.url(), r.request().method())
				);
				await page.getByRole('button', { name: 'Accept and continue' }).click();
				const resp = await accepted;
				expect(resp.status()).toBe(200);
				expect(((await resp.json()) as { legal_acceptance_required: boolean }).legal_acceptance_required).toBe(false);

				// The page the user asked for, at the address they asked for.
				await expect(step).toHaveCount(0);
				await expect(page.locator('.profile-btn')).toBeVisible();
				await expect(page).toHaveURL(/\/invoices$/);

				// Recorded server-side: a reload does not ask again.
				await page.reload();
				await expect(page.locator('.profile-btn')).toBeVisible();
				await expect(page.locator(STEP)).toHaveCount(0);
			} finally {
				// Signed in as the created clerk here; the delete needs the admin.
				await page.goto('/login');
				await page.evaluate(() => localStorage.removeItem('auth_token'));
				await signInAndWait(page);
				await page.request.delete(`${API_BASE}/api/admin/users/${created.id}`, {
					headers: await authedTenantHeaders(page)
				});
			}
		});
	});

	test('a ticked box refused as stale offers the reload, not another tick', async ({ page }) => {
		await forceStep(page);
		await stubAccept(page, 422, {
			code: 'legal_acceptance_required',
			message: 'Please accept the current Terms of Service and Privacy Policy to continue.',
			params: {}
		});
		await page.goto('/');
		await expect(page.locator(STEP)).toBeVisible();

		const box = page.getByRole('checkbox', { name: ACCEPT_BOX });
		await box.check();
		await page.getByRole('button', { name: 'Accept and continue' }).click();

		const changed = page.locator(STEP).getByRole('alert');
		await expect(changed).toContainText(
			'The Terms of Service or Privacy Policy changed since this page loaded.'
		);
		await expect(changed.getByRole('button', { name: 'Reload page' })).toBeVisible();
		await expect(box).toBeFocused();
		// Still held at the step.
		await expect(page.locator('.app-shell')).toHaveCount(0);
	});

	test('a rate-limited accept says how long to wait and keeps the step', async ({ page }) => {
		await forceStep(page);
		await stubAccept(page, 429, {
			code: 'rate_limited',
			message: 'Too many requests. Please wait 45 seconds before trying again.',
			params: { retry_after_seconds: 45 }
		});
		await page.goto('/');
		await page.getByRole('checkbox', { name: ACCEPT_BOX }).check();
		await page.getByRole('button', { name: 'Accept and continue' }).click();

		await expect(page.locator(STEP).getByRole('alert')).toHaveText(
			'Too many attempts. Wait 45 seconds and try again.'
		);
		await expect(page.locator('.app-shell')).toHaveCount(0);
	});

	test('Log out leaves the step for the sign-in page', async ({ page }) => {
		await forceStep(page);
		await page.goto('/');
		await expect(page.locator(STEP)).toBeVisible();
		// Not the real logout: its POST would revoke the worker admin's shared
		// stored session for every later spec in this worker.
		await page.route(
			(url) => url.pathname === '/api/auth/logout',
			(route) => route.fulfill({ status: 200, json: {} })
		);
		await page.getByRole('button', { name: 'Log Out' }).click();
		await page.waitForURL(/\/login/);
	});
});
