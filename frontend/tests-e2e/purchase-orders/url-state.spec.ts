import { expect, test } from '../fixtures/helpers';
import type { Page, Route } from '@playwright/test';

/**
 * `/purchase-orders` URL state.
 *
 * The search box, the status chip and the open PO lived in component state
 * only: a reload, a copied link or a return to the page reset the list to
 * "All" and closed the PO. They are now `?search=`, `?status=` and `?id=`,
 * written by one `syncUrl()` and read back on load. Every response is
 * intercepted, so the file writes nothing and is parallel-safe.
 */

function po(n: number, status = 'open') {
	return {
		id: `00000000-0000-4000-a000-${String(n).padStart(12, '0')}`,
		po_number: `E2E-URL-PO-${n}`,
		vendor_id: null,
		vendor_name: 'E2E Vendor',
		total: 100,
		total_unverifiable_reason: null,
		currency: 'USD',
		status,
		line_items: [],
		created_at: '2026-01-01T00:00:00Z'
	};
}

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

async function mockApi(page: Page) {
	const listRequests: URL[] = [];
	await page.route('**/api/purchase-orders?*', async (route) => {
		const url = new URL(route.request().url());
		if (url.pathname !== '/api/purchase-orders') return route.continue();
		listRequests.push(url);
		const items = url.searchParams.get('status') === 'closed' ? [po(2, 'closed')] : [po(1), po(2, 'closed')];
		return json(route, { items, total: items.length });
	});
	await page.route('**/api/purchase-orders/counts*', (route) =>
		json(route, { total: 2, by_status: { open: 1, closed: 1 } })
	);
	await page.route(/\/api\/purchase-orders\/[0-9a-f-]{36}$/, async (route) => {
		const id = new URL(route.request().url()).pathname.split('/').pop()!;
		return json(route, { ...po(Number(id.slice(-12))), id, linked_invoices: [] });
	});
	return listRequests;
}

const modal = (page: Page) => page.getByRole('dialog', { name: 'Purchase order' });

test.describe('/purchase-orders — URL state', () => {
	test('status and search survive a reload', async ({ page }) => {
		const listRequests = await mockApi(page);
		await page.goto('/purchase-orders');
		await expect(page.getByRole('button', { name: 'View purchase order E2E-URL-PO-1' })).toBeVisible();

		await page.getByRole('button', { name: /^Closed/ }).click();
		await expect(page).toHaveURL(/[?&]status=closed/);
		await page.getByPlaceholder('Search PO number...').fill('URL-PO');
		await expect(page).toHaveURL(/[?&]search=URL-PO/);

		listRequests.length = 0;
		await page.reload();
		await expect(page.getByRole('button', { name: /^Closed/ })).toHaveAttribute('aria-pressed', 'true');
		await expect(page.getByPlaceholder('Search PO number...')).toHaveValue('URL-PO');
		await expect(page.getByRole('button', { name: 'View purchase order E2E-URL-PO-2' })).toBeVisible();
		await expect(page.getByRole('button', { name: 'View purchase order E2E-URL-PO-1' })).toBeHidden();
		// The FIRST load after the reload already carries both filters.
		expect(listRequests[0]?.searchParams.get('status')).toBe('closed');
		expect(listRequests[0]?.searchParams.get('search')).toBe('URL-PO');
	});

	test('an opened PO is in the URL; closing it takes it out', async ({ page }) => {
		await mockApi(page);
		await page.goto('/purchase-orders');
		await page.getByRole('button', { name: 'View purchase order E2E-URL-PO-1' }).click();
		await expect(modal(page)).toContainText('E2E-URL-PO-1');
		await expect(page).toHaveURL(new RegExp(`[?&]id=${po(1).id}`));

		await page.reload();
		await expect(modal(page)).toContainText('E2E-URL-PO-1');

		await modal(page).getByRole('button', { name: 'Close' }).click();
		await expect(modal(page)).toBeHidden();
		await expect(page).not.toHaveURL(/[?&]id=/);
		await page.reload();
		await expect(page.getByRole('button', { name: 'View purchase order E2E-URL-PO-1' })).toBeVisible();
		await expect(modal(page)).toBeHidden();
	});

	test('a ?id= deep link opens that PO', async ({ page }) => {
		await mockApi(page);
		await page.goto(`/purchase-orders?id=${po(2).id}`);
		await expect(modal(page)).toContainText('E2E-URL-PO-2');
	});
});
