import { expect, test } from '../fixtures/helpers';

/**
 * /vendors column sort (issue #328, power-user/Medium) — see
 * invoices/sort.spec.ts for the full rationale. Uses the seed's existing
 * vendors; only asserts the request/URL contract.
 */

test.describe('/vendors column sort (acme admin)', () => {
	test('clicking Vendor sorts ascending, then descending on a second click', async ({
		page
	}) => {
		await page.goto('/vendors');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		const ascRequest = page.waitForResponse(
			(r) =>
				r.url().includes('/api/vendors?') &&
				r.url().includes('sort=name') &&
				r.url().includes('order=asc')
		);
		await page.getByRole('button', { name: 'Vendor', exact: true }).click();
		await ascRequest;
		await expect(page).toHaveURL(/sort=name/);
		await expect(page).toHaveURL(/order=asc/);

		const descRequest = page.waitForResponse(
			(r) =>
				r.url().includes('/api/vendors?') &&
				r.url().includes('sort=name') &&
				r.url().includes('order=desc')
		);
		await page.getByRole('button', { name: 'Vendor', exact: true }).click();
		await descRequest;
		await expect(page).toHaveURL(/order=desc/);
	});

	test('switching to Status starts it ascending', async ({ page }) => {
		await page.goto('/vendors');
		await expect(page.locator('table tbody tr').first()).toBeVisible();

		await page.getByRole('button', { name: 'Vendor', exact: true }).click();
		await page.waitForResponse((r) => r.url().includes('sort=name'));

		const statusAsc = page.waitForResponse(
			(r) =>
				r.url().includes('/api/vendors?') &&
				r.url().includes('sort=status') &&
				r.url().includes('order=asc')
		);
		await page.getByRole('button', { name: 'Status', exact: true }).click();
		await statusAsc;
		await expect(page).toHaveURL(/sort=status/);
	});

	// The URL is editable and outlives renames. An unknown `sort=` used to be
	// sent as-is, the backend's allowlist 422'd it, and the page sat on its
	// load error with a Retry that could only fail again.
	test('an unknown sort in the URL falls back to the default order', async ({ page }) => {
		const listRequest = page.waitForRequest((r) => r.url().includes('/api/vendors?'));
		await page.goto('/vendors?sort=not_a_column&order=desc');
		const url = new URL((await listRequest).url());
		expect(url.searchParams.has('sort')).toBe(false);

		await expect(page.locator('table tbody tr').first()).toBeVisible();
		await expect(page.getByTestId('vendors-load-error')).toHaveCount(0);
		await expect(page).not.toHaveURL(/sort=not_a_column/);
	});
});
