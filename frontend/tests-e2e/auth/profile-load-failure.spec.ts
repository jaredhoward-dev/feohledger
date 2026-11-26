import type { Page } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';

/**
 * `/profile` → Sessions and Notifications, when their list can't be loaded:
 * each card states the failure with `ui/LoadError`, and Retry loads it. The
 * passkey card's case is `passkey-list-failure.spec.ts`, beside this.
 *
 * Only the FIRST GET of the exact pathname is failed (the module-request trap
 * in tests-e2e/README.md § Stubbing an API route), so Retry reaches the real
 * backend and the recovery is real.
 */
async function failFirstGet(page: Page, pathname: string): Promise<void> {
	let failed = false;
	await page.route(
		(url) => url.pathname === pathname,
		async (route) => {
			if (failed || route.request().method() !== 'GET') return route.fallback();
			failed = true;
			return route.fulfill({
				status: 500,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'boom' })
			});
		}
	);
}

test.describe('/profile — a failed card load offers Retry', () => {
	test('sessions: fail → Retry → loaded', async ({ page }) => {
		await failFirstGet(page, '/api/auth/sessions');
		await page.goto('/profile?section=sessions');

		const failed = page.getByTestId('profile-sessions-load-failed');
		await expect(failed).toHaveAttribute('role', 'alert');
		await expect(failed).toContainText("Couldn't load your sessions");
		const retry = failed.getByRole('button', { name: 'Retry', exact: true });
		await retry.click();

		await expect(failed).toHaveCount(0);
		// The signed-in browser running this test is itself a session, so the
		// loaded list is never empty.
		await expect(page.locator('ul.entry-list li').first()).toBeVisible();
		// The block stayed mounted through the retry and unmounted when it
		// landed, under the focused button: focus resumes in <main>, not at the
		// top of the document.
		await expect(page.locator('main')).toBeFocused();
	});

	test('notifications: fail → Retry → loaded', async ({ page }) => {
		await failFirstGet(page, '/api/notifications/preferences');
		await page.goto('/profile?section=notifications');

		const failed = page.getByTestId('profile-notifications-load-failed');
		await expect(failed).toHaveAttribute('role', 'alert');
		await expect(failed).toContainText('Could not load notification preferences.');
		await failed.getByRole('button', { name: 'Retry', exact: true }).click();

		await expect(failed).toHaveCount(0);
		await expect(
			page.getByRole('checkbox', { name: /^Email/ }).first()
		).toBeVisible();
		await expect(page.locator('main')).toBeFocused();
	});
});
