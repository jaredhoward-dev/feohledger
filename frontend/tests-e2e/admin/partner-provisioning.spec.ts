import { API_BASE, currentTenantSlug, expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * Partner link provisioning — the consent side and the privilege boundary.
 *
 * Two-sided consent: a partner can only attach an org whose own admin minted a
 * link code. Minting is NOT a partner act, so it lives in Organization settings
 * (`POST /api/organization/partner-link-code`, any admin — decisions §574);
 * redeeming it (`POST /api/partner/children`) needs the operator-granted
 * partner capability on top of the admin role.
 *
 * These specs assert (a) any admin — the e2e tenant is not a partner — mints a
 * code from Organization → Join a partner, and (b) a workspace that is not a
 * partner cannot adopt anyone, even with a valid code. The partner-side
 * round-trip (attach, re-parent guard, single use, detach, self-adopt) is held
 * by the backend pytest suite against two real tenants.
 */

test.describe('partner link provisioning (admin)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async ({ page }) => {
		await signInAndWait(page);
	});

	test('an admin mints a link code from Organization settings', async ({ page }) => {
		await page.goto('/organization?section=partner-link');
		await expect(page.getByRole('heading', { name: 'Join a partner' })).toBeVisible();
		await expect(page.getByTestId('link-code-panel')).toBeVisible();

		// The dev signing key is set in .env.development.
		const generate = page.getByRole('button', { name: 'Generate link code' });
		await generate.click();
		// Shown once in the shared SecretReveal dialog: focus moves into it, the
		// copy is acknowledged, and the code leaves the page on Done.
		const dialog = page.getByRole('dialog', { name: 'New link code' });
		await expect(dialog).toBeVisible();
		const minted = dialog.getByTestId('minted-link-code');
		await expect(minted).not.toBeEmpty();
		await expect(dialog.locator(':focus')).toHaveCount(1);
		await dialog.getByRole('button', { name: 'Done' }).click();
		await expect(page.getByTestId('minted-link-code')).toHaveCount(0);
		await expect(generate).toBeFocused();
	});

	test('a refused mint is stated from its code, not the server English', async ({ page }) => {
		await page.route(
			(url) => url.pathname === '/api/organization/partner-link-code',
			(route) =>
				route.fulfill({
					status: 503,
					json: {
						detail: {
							code: 'partner_linking_disabled',
							message: 'server English',
							params: {}
						}
					}
				})
		);
		await page.goto('/organization?section=partner-link');
		const generate = page.getByRole('button', { name: 'Generate link code' });
		await generate.click();
		const sentence =
			'Partner linking is not enabled on this platform yet, so no link code can be generated. Contact support.';
		await expect(page.getByTestId('partner-link-failure')).toHaveText(sentence);
		await expect(generate).toHaveAccessibleDescription(sentence);
		await expect(page.getByTestId('minted-link-code')).toHaveCount(0);
	});

	test('a workspace that is not a partner cannot redeem a code', async ({ page }) => {
		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		const headers = {
			Authorization: `Bearer ${token}`,
			'X-Tenant-Slug': currentTenantSlug(),
			'Content-Type': 'application/json'
		};
		const mint = await page.request.post(`${API_BASE}/api/organization/partner-link-code`, {
			headers,
			data: {}
		});
		expect(mint.status()).toBe(200);
		const { link_code } = (await mint.json()) as { link_code: string };

		const attach = await page.request.post(`${API_BASE}/api/partner/children`, {
			headers,
			data: { link_code }
		});
		expect(attach.status()).toBe(403);
		const body = (await attach.json()) as { detail: { code: string } };
		expect(body.detail.code).toBe('partner_capability_required');
	});
});

test.describe('partner link provisioning (clerk — not authorized)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('a clerk cannot mint a link code or attach a child (403)', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);
		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		const headers = {
			Authorization: `Bearer ${token}`,
			'X-Tenant-Slug': currentTenantSlug(),
			'Content-Type': 'application/json'
		};
		const mint = await page.request.post(`${API_BASE}/api/organization/partner-link-code`, {
			headers,
			data: {}
		});
		expect(mint.status()).toBe(403);
		const attach = await page.request.post(`${API_BASE}/api/partner/children`, {
			headers,
			data: { link_code: 'x.y' }
		});
		expect(attach.status()).toBe(403);
	});
});
