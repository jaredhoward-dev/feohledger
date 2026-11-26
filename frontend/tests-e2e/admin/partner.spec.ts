import {
	API_BASE,
	currentTenantSlug,
	expect,
	sectionTabHrefs,
	signInAndWait,
	stubPartnerWorkspace,
	TENANT_ROOT_URL,
	test
} from '../fixtures/helpers';

/**
 * /admin/partner — Partner / reseller multi-tenant admin.
 *
 * Surfaces the backend `/api/partner` endpoints (`backend/app/api/partner.py`),
 * every one of which requires the admin role AND the operator-granted partner
 * capability (`Organization.is_partner`, decisions §574). The Partner Admin
 * row and the page check the same two things (`#lib/nav.ts`), so they agree.
 *
 * No e2e tenant is a partner — the realistic default — so the "shown" side
 * stubs the capability on `/api/auth/me` (`stubPartnerCapability`), and the
 * real backend is asserted to refuse the unstubbed admin with the coded 403.
 * The partner-side behaviour (children, branding, isolation) is held by the
 * backend pytest suite against two real tenants.
 */

const SETTINGS_TAB = '/organization';

test.describe('/admin/partner (admin of a workspace that is not a partner)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test.beforeEach(async ({ page }) => {
		await signInAndWait(page);
	});

	test('the Partner Admin row is hidden, the page bounces, and the API refuses', async ({
		page
	}) => {
		await page.goto(SETTINGS_TAB);
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		expect(await sectionTabHrefs(page)).not.toContain('/admin/partner');

		await page.goto('/admin/partner');
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
		await expect(page.getByRole('heading', { name: 'Partner Admin' })).toHaveCount(0);

		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		const resp = await page.request.get(`${API_BASE}/api/partner`, {
			headers: {
				Authorization: `Bearer ${token}`,
				'X-Tenant-Slug': currentTenantSlug()
			}
		});
		expect(resp.status()).toBe(403);
		const body = (await resp.json()) as { detail: { code: string } };
		expect(body.detail.code).toBe('partner_capability_required');
	});
});

test.describe('/admin/partner (admin of a partner workspace)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('the Partner Admin row is shown and the page opens', async ({ page }) => {
		await stubPartnerWorkspace(page);
		await signInAndWait(page);

		await page.goto(SETTINGS_TAB);
		await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
		expect(await sectionTabHrefs(page)).toContain('/admin/partner');

		await page.goto('/admin/partner');
		await expect(page.getByRole('heading', { name: 'Partner Admin' })).toBeVisible();
		await expect(page.getByTestId('attach-child-btn')).toBeVisible();
		await expect(page.getByTestId('table-empty')).toHaveText(/No child tenants/, {
			timeout: 10_000
		});
		// Consenting to be adopted is no longer here: it is in Organization settings.
		await expect(page.getByTestId('link-code-panel')).toHaveCount(0);
	});
});

test.describe('/admin/partner (clerk — not authorized)', () => {
	test.use({ storageState: { cookies: [], origins: [] } });

	test('ap_clerk is redirected away and the API 403s them', async ({ page, tenantClerk }) => {
		await signInAndWait(page, tenantClerk);

		await page.goto('/admin/partner');
		// admin-only — the page waits for /me then bounces the clerk to root.
		await page.waitForURL(TENANT_ROOT_URL, { timeout: 15_000 });
		await expect(page.getByRole('heading', { name: 'Partner Admin' })).toHaveCount(0);

		// The API itself 403s a non-admin.
		const token = await page.evaluate(() => localStorage.getItem('auth_token'));
		const resp = await page.request.get(`${API_BASE}/api/partner`, {
			headers: {
				Authorization: `Bearer ${token}`,
				'X-Tenant-Slug': currentTenantSlug()
			}
		});
		expect(resp.status()).toBe(403);
	});
});
