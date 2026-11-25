import type { Page, Route } from '@playwright/test';
import { API_BASE, authedTenantHeaders, expect, signInAndWait, test } from '../fixtures/helpers';

/**
 * /organization → SCIM provisioning (decisions §510).
 *
 * The panel generates, rotates and revokes the tenant's SCIM bearer token
 * through `POST` / `DELETE /api/organization/sso/scim-token`. The server side
 * (audit-first rows, hash-only storage, revoke clearing both copies, the
 * admin gate) is proven in `backend/tests/test_credential_mint_hygiene.py`.
 * What this spec holds is the page's side: the plaintext is shown once and is
 * gone after the dialog closes and after a reload, rotating and revoking each
 * ask first, and a non-admin gets the notice and no request at all.
 *
 * Minting demands a second factor (decisions §510). MFA is off in the e2e
 * stack, where `require_sensitive_step_up` skips the gate, so the step-up
 * test STUBS the mint to answer `sensitive_step_up_required` the way a
 * deployed server does (the server half is in the pytest file above).
 *
 * Each test starts from no token. The SCIM provisioning spec mints its own
 * token per test, so leaving none behind costs it nothing.
 */

const TOKEN_PATH = '/api/organization/sso/scim-token';

async function revoke(page: Page): Promise<void> {
	const resp = await page.request.delete(`${API_BASE}${TOKEN_PATH}`, {
		headers: await authedTenantHeaders(page)
	});
	expect(resp.status()).toBe(204);
}

function tokenCall(page: Page, method: 'POST' | 'DELETE') {
	return page.waitForResponse(
		(r) => new URL(r.url()).pathname === TOKEN_PATH && r.request().method() === method
	);
}

test.describe('/organization — SCIM provisioning', () => {
	test.beforeEach(async ({ page }) => {
		await revoke(page);
	});

	test('generate shows the token once; a reload says it is set but never shows it', async ({
		page
	}) => {
		await page.goto('/organization?section=scim');
		const panel = page.getByTestId('scim-panel');
		await expect(panel.getByTestId('scim-status')).toHaveAttribute('data-configured', 'false');
		await expect(panel.getByTestId('scim-endpoint')).toHaveValue(/\/api\/scim\/v2$/);

		const minted = tokenCall(page, 'POST');
		await panel.getByTestId('scim-generate').click();
		const body = (await (await minted).json()) as { token: string };
		expect(body.token.length).toBeGreaterThan(20);

		const dialog = page.getByRole('dialog', { name: 'New SCIM token' });
		await expect(dialog.getByTestId('scim-token-value')).toHaveText(body.token);
		await expect(dialog.getByRole('alert')).toContainText('You won’t see this token again');
		// The copy control is a real, focusable button.
		await expect(dialog.getByRole('button', { name: 'Copy token' })).toBeEnabled();

		await dialog.getByRole('button', { name: 'Done' }).click();
		await expect(page.getByTestId('scim-token-value')).toHaveCount(0);
		await expect(panel.getByTestId('scim-status')).toHaveAttribute('data-configured', 'true');

		await page.reload();
		await expect(page.getByTestId('scim-status')).toHaveAttribute('data-configured', 'true');
		await expect(page.getByText(body.token)).toHaveCount(0);
		expect(await page.content()).not.toContain(body.token);
		expect(page.url()).not.toContain(body.token);
	});

	test('rotate asks first; Cancel keeps the live token, confirming replaces it', async ({
		page
	}) => {
		const headers = await authedTenantHeaders(page);
		const first = await page.request.post(`${API_BASE}${TOKEN_PATH}`, { headers });
		expect(first.status()).toBe(200);

		let posts = 0;
		page.on('request', (r) => {
			if (new URL(r.url()).pathname === TOKEN_PATH && r.method() === 'POST') posts += 1;
		});
		await page.goto('/organization?section=scim');
		const panel = page.getByTestId('scim-panel');

		await panel.getByTestId('scim-rotate').click();
		const confirm = page.getByRole('dialog', { name: 'Rotate SCIM token' });
		await expect(confirm.getByTestId('scim-confirm-body')).toContainText('stops working at once');
		await confirm.getByRole('button', { name: 'Cancel' }).click();
		await expect(confirm).toHaveCount(0);
		expect(posts).toBe(0);

		await panel.getByTestId('scim-rotate').click();
		const rotated = tokenCall(page, 'POST');
		await page.getByRole('dialog', { name: 'Rotate SCIM token' }).getByTestId('scim-confirm').click();
		expect((await rotated).status()).toBe(200);
		await expect(
			page.getByRole('dialog', { name: 'New SCIM token' }).getByTestId('scim-token-value')
		).toBeVisible();
	});

	test('revoke asks first, then the panel says no token is set', async ({ page }) => {
		const headers = await authedTenantHeaders(page);
		expect((await page.request.post(`${API_BASE}${TOKEN_PATH}`, { headers })).status()).toBe(200);

		await page.goto('/organization?section=scim');
		const panel = page.getByTestId('scim-panel');
		await panel.getByTestId('scim-revoke').click();
		const revoked = tokenCall(page, 'DELETE');
		await page.getByRole('dialog', { name: 'Revoke SCIM token' }).getByTestId('scim-confirm').click();
		expect((await revoked).status()).toBe(204);
		await expect(panel.getByTestId('scim-status')).toHaveAttribute('data-configured', 'false');
		await expect(panel.getByTestId('scim-generate')).toBeVisible();
	});

	test('generating asks for a second factor and resends with the proof', async ({ page }) => {
		const posted: Record<string, unknown>[] = [];
		await page.route(
			(url) => url.pathname === TOKEN_PATH,
			async (route: Route) => {
				if (route.request().method() !== 'POST') return route.fallback();
				const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
				posted.push(body);
				if (!body.step_up) {
					return route.fulfill({
						status: 403,
						contentType: 'application/json',
						body: JSON.stringify({
							detail: {
								code: 'sensitive_step_up_required',
								message: 'Confirm a current authenticator code or a registered passkey to continue.',
								params: {}
							}
						})
					});
				}
				return route.fulfill({
					status: 200,
					contentType: 'application/json',
					headers: { 'Cache-Control': 'no-store' },
					body: JSON.stringify({ token: 'scim_stubbed_token_value', bearer_hash_prefix: 'abcd1234' })
				});
			}
		);
		await page.goto('/organization?section=scim');
		const panel = page.getByTestId('scim-panel');
		await panel.getByTestId('scim-generate').click();

		const prompt = page.getByRole('dialog', { name: 'Confirm it’s you' });
		await expect(prompt).toBeVisible();
		await expect(prompt).toContainText('A SCIM token can create users and grant roles.');
		await prompt.getByTestId('step-up-code').fill('123456');
		await prompt.getByRole('button', { name: 'Confirm' }).click();

		const reveal = page.getByRole('dialog', { name: 'New SCIM token' });
		await expect(reveal.getByTestId('scim-token-value')).toHaveText('scim_stubbed_token_value');
		expect(posted).toEqual([{}, { step_up: { code: '123456' } }]);
	});

	test('the new primary action takes focus after Generate and after Revoke', async ({ page }) => {
		await page.goto('/organization?section=scim');
		const panel = page.getByTestId('scim-panel');

		await panel.getByTestId('scim-generate').click();
		await page.getByRole('dialog', { name: 'New SCIM token' }).getByRole('button', { name: 'Done' }).click();
		// The Generate button is gone; focus lands on its replacement, not the page.
		await expect(panel.getByTestId('scim-rotate')).toBeFocused();

		await panel.getByTestId('scim-revoke').click();
		await page.getByRole('dialog', { name: 'Revoke SCIM token' }).getByTestId('scim-confirm').click();
		await expect(panel.getByTestId('scim-status')).toHaveAttribute('data-configured', 'false');
		await expect(panel.getByTestId('scim-generate')).toBeFocused();
	});

	test('a non-admin sees the notice and the page asks the server nothing', async ({
		page,
		tenantClerk
	}) => {
		let asked = 0;
		page.on('request', (r) => {
			const path = new URL(r.url()).pathname;
			if (path === TOKEN_PATH || path === '/api/organization/sso') asked += 1;
		});
		await signInAndWait(page, tenantClerk);
		await page.goto('/organization?section=scim');

		const panel = page.getByTestId('scim-panel');
		await expect(panel.getByTestId('scim-admin-only')).toBeVisible();
		await expect(panel.getByTestId('scim-generate')).toHaveCount(0);
		await expect(panel.getByTestId('scim-status')).toHaveCount(0);
		expect(asked).toBe(0);
	});
});
