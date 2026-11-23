import { expect, test } from '../fixtures/helpers';

/**
 * `/find-workspace` on a workspace host, signed out. Two navigations used to
 * race here: the page's own `onMount` redirect to `/`, and the root layout's
 * auth wall sending the visitor to `/login?next=/find-workspace`. Whichever
 * SvelteKit settled last won — a detour through `/`, or a sign-in that
 * returned the user to `/find-workspace` only to bounce them again. The root
 * layout now owns the decision (`#lib/utils/tenantHostRoute.ts`): one
 * navigation, straight to a bare `/login`.
 *
 * Every main-frame URL the tab passes through is recorded (client-side
 * `replaceState` navigations included), so a detour cannot hide behind the
 * settled page.
 *
 * Recording starts on a blank document. The shared `page` fixture opens the
 * tenant root (`/`) before the test body runs, and `page.goto` returns at that
 * document's `load` event, which can come before SvelteKit has finished
 * starting. Its start-up `history.replaceState` (the router's initial history
 * entry, on the URL it is already at) then fires `framenavigated` for `/` after
 * the listener is attached. That read as a detour through `/` that this
 * navigation never made, in most local runs and intermittently in CI.
 * Navigating to `about:blank` first retires that document, so everything
 * recorded belongs to `/find-workspace` and what it leads to.
 */
test.use({ storageState: { cookies: [], origins: [] } });

test('signed-out find-your-workspace on a workspace host goes straight to sign-in', async ({ page }) => {
	await page.goto('about:blank');
	const visited: string[] = [];
	page.on('framenavigated', (frame) => {
		if (frame === page.mainFrame()) visited.push(frame.url());
	});

	await page.goto('/find-workspace');
	await page.waitForURL((url) => url.pathname === '/login');
	await expect(page.locator('form.login-form')).toBeVisible();

	const settled = new URL(page.url());
	expect(settled.search).toBe('');
	const paths = visited.map((u) => {
		const url = new URL(u);
		return url.pathname + url.search;
	});
	// Never `/` (the page's old redirect) and never `?next=/find-workspace`
	// (the auth wall's): only the page asked for, then the sign-in form.
	expect(paths.filter((p) => p !== '/find-workspace' && p !== '/login')).toEqual([]);
	// And the record is of this navigation: it opens on the page asked for.
	expect(paths[0]).toBe('/find-workspace');
});
