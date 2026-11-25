import { PRIVACY_VERSION } from '#lib/legal/legalVersions.generated.ts';
import { expect, test } from '../fixtures/helpers';

// Signed out: the page is where a supplier with no password yet lands.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * /portal/reset-password — where the AP team's supplier-portal invite and reset
 * links land (decisions §283; `POST /api/portal/auth/reset-password[/check]`).
 *
 * Like `auth/forgot-password.spec.ts`, this proves the page is wired to the
 * right endpoints and states; it does not mint a real token, which needs the
 * email the invite sends. The full round trip — mint, tenant binding, single
 * use, sign-in with the new password — is pinned against real Postgres + Redis
 * in `backend/tests/test_vendor_portal_user_password_reset.py`. The 2xx paths
 * stub their endpoints on exact pathnames.
 */

test.describe('/portal/reset-password', () => {
	test('is reachable signed out and flags a missing token', async ({ page }) => {
		await page.goto('/portal/reset-password');
		// Not bounced to /portal/login: the link's token is the credential.
		await expect(page.getByText(/missing its token/i)).toBeVisible();
		await expect(page).toHaveURL(/\/portal\/reset-password$/);
		await expect(page.locator('input[type="password"]')).toHaveCount(0);
	});

	test('a bogus invite link is refused up front and says how to get a new one', async ({
		page
	}) => {
		const checked = page.waitForResponse(
			(r) => new URL(r.url()).pathname === '/api/portal/auth/reset-password/check'
		);
		await page.goto('/portal/reset-password?token=not-a-real-token&mode=set');
		expect((await checked).status()).toBe(400);

		await expect(page.getByRole('heading', { name: 'Set your password' })).toBeVisible();
		await expect(page.getByText(/invalid or has expired/i)).toBeVisible();
		await expect(page.getByText(/Request a new one below/i)).toBeVisible();
		// The supplier can ask for a new link themselves, on the portal's own
		// request page.
		await expect(page.getByRole('link', { name: 'Reset your password' })).toHaveAttribute(
			'href',
			'/portal/forgot-password'
		);
		await expect(page.locator('input[type="password"]')).toHaveCount(0);
		// The way back is the portal's own sign-in — never the employee
		// forgot-password page, which cannot reset a supplier account.
		await expect(page.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute(
			'href',
			'/portal/login'
		);
	});

	test('a set password lands focus on the confirmation and offers the portal sign-in', async ({
		page
	}) => {
		await page.route(
			(url) => url.pathname === '/api/portal/auth/reset-password/check',
			(route) => route.fulfill({ json: { email: 'supplier@vendor.example' } })
		);
		let posted: unknown = null;
		await page.route(
			(url) => url.pathname === '/api/portal/auth/reset-password',
			(route) => {
				posted = route.request().postDataJSON();
				return route.fulfill({ json: { detail: 'ok' } });
			}
		);
		await page.goto('/portal/reset-password?token=stubbed-token&mode=set');

		// The hidden username field files the new password under the account.
		await expect(page.locator('form input[autocomplete="username"]')).toHaveValue(
			'supplier@vendor.example'
		);
		await page.getByLabel('New password', { exact: true }).fill('Supplier-Chosen9');
		await page.getByLabel('Confirm new password', { exact: true }).fill('Supplier-Chosen9');
		await page.getByRole('button', { name: 'Set password' }).click();

		const success = page.getByText(/your password is set/i);
		await expect(success).toBeVisible();
		await expect(success).toBeFocused();
		expect(posted).toEqual({ token: 'stubbed-token', new_password: 'Supplier-Chosen9' });
		await expect(page.getByRole('link', { name: 'Go to sign in' })).toHaveAttribute(
			'href',
			'/portal/login?email=supplier%40vendor.example'
		);
	});

	test('a supplier with no password yet acknowledges the Privacy Policy, and is never asked to accept the Terms', async ({
		page
	}) => {
		// An invited supplier's link is the account's front door, so the check
		// says the box is required. A Supplier User is not a party to our Terms,
		// so the box names the Privacy Policy alone and the redeem carries only
		// its version (decisions §499).
		const redeemed: Record<string, unknown>[] = [];
		await page.route(
			(url) => url.pathname === '/api/portal/auth/reset-password/check',
			(route) =>
				route.fulfill({
					json: { email: 'supplier@vendor.example', legal_acceptance_required: true }
				})
		);
		await page.route(
			(url) => url.pathname === '/api/portal/auth/reset-password',
			(route) => {
				redeemed.push(route.request().postDataJSON());
				return route.fulfill({ json: { detail: 'ok' } });
			}
		);
		await page.goto('/portal/reset-password?token=stubbed-token&mode=set');

		const box = page.getByRole('checkbox', { name: /I have read the Privacy Policy/ });
		await expect(box).not.toBeChecked();
		const consent = page.locator('.legal-consent');
		await expect(consent.getByRole('link', { name: 'Privacy Policy' })).toHaveAttribute(
			'href',
			'/legal/privacy'
		);
		await expect(consent.getByRole('link', { name: 'Terms of Service' })).toHaveCount(0);

		await page.getByLabel('New password', { exact: true }).fill('Supplier-Chosen9');
		await page.getByLabel('Confirm new password', { exact: true }).fill('Supplier-Chosen9');
		await page.getByRole('button', { name: 'Set password' }).click();
		// Unticked: said beside the box, which takes focus; nothing is sent.
		await expect(
			page.getByText('Tick the box to confirm you have read the Privacy Policy.')
		).toBeVisible();
		await expect(box).toBeFocused();
		expect(redeemed).toHaveLength(0);

		await box.check();
		await page.getByRole('button', { name: 'Set password' }).click();
		await expect(page.getByText(/your password is set/i)).toBeVisible();
		expect(redeemed).toEqual([
			{
				token: 'stubbed-token',
				new_password: 'Supplier-Chosen9',
				accepted_privacy_version: PRIVACY_VERSION
			}
		]);
	});

	test('a box the server still refuses after ticking offers a reload naming the Privacy Policy', async ({
		page
	}) => {
		await page.route(
			(url) => url.pathname === '/api/portal/auth/reset-password/check',
			(route) =>
				route.fulfill({
					json: { email: 'supplier@vendor.example', legal_acceptance_required: true }
				})
		);
		await page.route(
			(url) => url.pathname === '/api/portal/auth/reset-password',
			(route) =>
				route.fulfill({
					status: 422,
					json: {
						detail: {
							code: 'privacy_acknowledgement_required',
							message: 'Please confirm you have read the current Privacy Policy to continue.'
						}
					}
				})
		);
		await page.goto('/portal/reset-password?token=stubbed-token&mode=set');
		const box = page.getByRole('checkbox', { name: /I have read the Privacy Policy/ });
		await page.getByLabel('New password', { exact: true }).fill('Supplier-Chosen9');
		await page.getByLabel('Confirm new password', { exact: true }).fill('Supplier-Chosen9');
		await box.check();
		await page.getByRole('button', { name: 'Set password' }).click();

		const message = page.locator('.legal-consent + [role="alert"]');
		await expect(message).toContainText('The Privacy Policy changed since this page loaded.');
		await expect(box).toBeChecked();
		await expect(box).toBeFocused();
		await expect(message.getByRole('button', { name: 'Reload page' })).toBeVisible();
	});
});
