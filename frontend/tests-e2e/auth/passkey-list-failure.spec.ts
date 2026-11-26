import { expect, test } from '../fixtures/helpers';

/**
 * `/profile` → Passkeys, when the passkey list can't be loaded.
 *
 * `GET /api/auth/mfa/passkey` failing used to be swallowed into
 * `passkeys = []`, which is a *claim*: the card said "No passkeys yet", and —
 * worse — the step-up decision is built on that same array. With
 * `needsPasskeyStepUp` reading false, Add/Remove submitted with no proof at
 * all, and `POST /auth/mfa/passkey/register` answered the opaque step-up 400
 * for any account that actually does hold a factor.
 *
 * The unknown list now stays unknown: the card says so and offers a retry, and
 * the step-up requirement fails CLOSED (a proof is asked for, and the passkey
 * ceremony stays on offer because the server — not this list — owns the
 * credential set).
 */
test.describe('/profile — passkey list failure', () => {
	test('an unloadable passkey list reports itself instead of claiming "none"', async ({
		page
	}) => {
		let fail = true;
		await page.route('**/api/auth/mfa/passkey', async (route) => {
			if (route.request().method() !== 'GET') {
				await route.continue();
				return;
			}
			if (fail) {
				await route.fulfill({
					status: 500,
					contentType: 'application/json',
					body: JSON.stringify({ detail: 'boom' })
				});
				return;
			}
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify([])
			});
		});

		await page.goto('/profile?section=passkeys');

		const card = page.locator('section.card', { hasText: 'Passkeys' }).first();
		await expect(card.getByText(/Couldn't load your passkeys/)).toBeVisible();
		await expect(card.getByText('No passkeys yet')).toHaveCount(0);

		// Retry succeeds → the honest empty state finally appears.
		fail = false;
		await card.getByRole('button', { name: 'Retry', exact: true }).click();
		await expect(card.getByText('No passkeys yet')).toBeVisible();
		await expect(card.getByText(/Couldn't load your passkeys/)).toHaveCount(0);
	});

	test('Retry keeps focus while it runs, and a second failure is announced again', async ({
		page
	}) => {
		// The card keeps its LoadError mounted through the retry (`retrying`),
		// so the button the user pressed must keep focus — not jump to <main>
		// while it reads "Retrying…" (WCAG 2.4.3) — and a retry that fails again
		// must re-render the alert, not leave unchanged text that a screen
		// reader never re-reads (WCAG 4.1.3).
		let holdNext = false;
		let release: () => void = () => {};
		const held = new Promise<void>((resolve) => (release = resolve));
		await page.route('**/api/auth/mfa/passkey', async (route) => {
			if (route.request().method() !== 'GET') {
				await route.continue();
				return;
			}
			// The Retry's GET is held so the in-flight state can be read, then
			// fails like the first.
			if (holdNext) {
				holdNext = false;
				await held;
			}
			await route.fulfill({
				status: 500,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'boom' })
			});
		});

		await page.goto('/profile?section=passkeys');
		const failed = page.getByTestId('profile-passkeys-load-failed');
		await expect(failed).toHaveAttribute('role', 'alert');
		const message = failed.getByText(/Couldn't load your passkeys/);
		await expect(message).toBeVisible();
		// Mark the first rendering, to tell a re-rendered (re-announced)
		// message from the same node left in place.
		await message.evaluate((el) => el.setAttribute('data-first-render', ''));

		const retry = failed.getByRole('button', { name: /^Retry/ });
		holdNext = true;
		await retry.click();
		await expect(retry).toHaveText('Retrying…');
		await expect(retry).toHaveAttribute('aria-disabled', 'true');
		await expect(retry).toBeFocused();

		release();
		await expect(retry).toHaveText('Retry');
		await expect(retry).toBeFocused();
		await expect(message).toBeVisible();
		await expect(failed.locator('[data-first-render]')).toHaveCount(0);
	});
});
