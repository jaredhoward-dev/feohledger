import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';

// Start unauthenticated — the point is the signed-out first paint.
test.use({ storageState: { cookies: [], origins: [] } });

/**
 * A browser that blocks site storage for the origin (decisions §514,
 * `docs/known-issues.md` § The app renders nothing when the browser blocks
 * site storage).
 *
 * In that state merely READING `window.localStorage` throws a
 * `SecurityError`, and the auth store reads the token at module load — so
 * `/login` used to stay on the empty shell with an uncaught error in the
 * console. The init script below reproduces exactly that, before any app code
 * runs: both storage properties become throwing getters. The sign-in page must
 * still draw, and must say why a sign-in cannot last.
 */
async function blockSiteStorage(page: Page) {
	await page.addInitScript(() => {
		const deny = () => {
			throw new DOMException('The operation is insecure.', 'SecurityError');
		};
		Object.defineProperty(window, 'localStorage', { get: deny, configurable: true });
		Object.defineProperty(window, 'sessionStorage', { get: deny, configurable: true });
	});
}

/** Uncaught page errors — a storage throw escaping the boot path lands here. */
function collectPageErrors(page: Page): Error[] {
	const errors: Error[] = [];
	page.on('pageerror', (err) => errors.push(err));
	return errors;
}

const NOTICE = /blocking this site from storing data/;

test.describe('sign-in with site storage blocked', () => {
	test('/login draws the form and says storage is blocked', async ({ page }) => {
		const errors = collectPageErrors(page);
		await blockSiteStorage(page);
		await page.goto('/login');

		await expect(page.getByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible();
		await expect(page.locator('input[type="email"]')).toBeVisible();
		await expect(page.locator('input[type="password"]')).toBeVisible();
		await expect(page.getByRole('button', { name: /Sign in/ })).toBeVisible();
		await expect(page.getByRole('status').getByText(NOTICE)).toBeVisible();

		expect(errors.map((e) => e.message)).toEqual([]);
	});

	test('the supplier portal sign-in draws and says the same', async ({ page }) => {
		const errors = collectPageErrors(page);
		await blockSiteStorage(page);
		await page.goto('/portal/login');

		await expect(page.locator('input[type="email"]')).toBeVisible();
		await expect(page.locator('input[type="password"]')).toBeVisible();
		await expect(page.getByText(NOTICE)).toBeVisible();

		expect(errors.map((e) => e.message)).toEqual([]);
	});

	test('with storage available, neither sign-in page shows the notice', async ({ page }) => {
		await page.goto('/login');
		await expect(page.locator('input[type="email"]')).toBeVisible();
		await expect(page.getByText(NOTICE)).toHaveCount(0);

		await page.goto('/portal/login');
		await expect(page.locator('input[type="email"]')).toBeVisible();
		await expect(page.getByText(NOTICE)).toHaveCount(0);
	});
});
