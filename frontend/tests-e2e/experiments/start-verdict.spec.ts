import { expect, test } from '../fixtures/helpers';
import type { Page } from '@playwright/test';

/**
 * `/experiments` — every row action reads the row's server verdict
 * (`actions.{start,stop,conclude,delete}`, backend
 * `api/workflow_experiments.{start,stop,conclude,delete}_refusal`), never a
 * restated `status === …` + role rule (frontend/docs/ui-patterns.md § Whether
 * to offer an action).
 *
 * The page explains no refusal (each is about the status, the assignments a
 * stopped draft keeps, or the reader's role), so a refused control is hidden,
 * and a row whose verdict has not arrived offers none (fail closed).
 *
 * Stubbed, exact-pathname guarded: `**\/api/experiments*` also matches Vite's
 * module URL for `/src/lib/api/experiments.ts` (see load-states.spec.ts).
 */

function experiment(name: string, overrides: Record<string, unknown> = {}) {
	return {
		id: `e0000000-0000-4000-8000-${name.replace(/\D/g, '').padStart(12, '0')}`,
		name,
		description: null,
		workflow_definition_id: 'w0000000-0000-4000-8000-000000000001',
		workflow_definition_name: 'Default approval',
		config_a: {},
		config_b: {},
		split_a_pct: 50,
		primary_metric: 'time_to_approval_days',
		min_sample_per_variant: 30,
		status: 'draft',
		started_at: null,
		ended_at: null,
		assigned_count: 0,
		entity_id: null,
		created_at: '2026-11-01T10:00:00+00:00',
		updated_at: '2026-11-01T10:00:00+00:00',
		...overrides
	};
}

function refused(code: string, params: Record<string, unknown> = {}) {
	return { code, message: `Refused: ${code}`, params };
}

async function stubList(page: Page, experiments: unknown[]) {
	await page.route('**/api/experiments*', async (route) => {
		const req = route.request();
		if (req.method() !== 'GET' || new URL(req.url()).pathname !== '/api/experiments') {
			return route.continue();
		}
		return route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ experiments })
		});
	});
}

function startIn(page: Page, name: string) {
	return page
		.locator('tbody tr', { hasText: name })
		.getByRole('button', { name: new RegExp(`^Start\\b`) });
}

function actionIn(page: Page, name: string, action: string) {
	return page
		.locator('tbody tr', { hasText: name })
		.getByRole('button', { name: new RegExp(`^${action} for `) });
}

test.describe('/experiments — Start follows the server verdict', () => {
	test('an allowed verdict offers Start', async ({ page }) => {
		await stubList(page, [experiment('Exp verdict 0001', { actions: { start: null } })]);
		await page.goto('/experiments');

		await expect(startIn(page, 'Exp verdict 0001')).toBeVisible({ timeout: 15_000 });
		await expect(startIn(page, 'Exp verdict 0001')).toBeEnabled();
	});

	test('a status refusal hides Start, even on a draft the old rule would have offered', async ({
		page
	}) => {
		await stubList(page, [
			experiment('Exp verdict 0002', {
				status: 'draft',
				actions: { start: refused('experiment_not_startable', { status: 'running' }) }
			})
		]);
		await page.goto('/experiments');

		const r = page.locator('tbody tr', { hasText: 'Exp verdict 0002' });
		await expect(r).toBeVisible({ timeout: 15_000 });
		await expect(startIn(page, 'Exp verdict 0002')).toHaveCount(0);
		await expect(r).not.toContainText('Refused:');
	});

	test('a role refusal hides Start rather than explaining it', async ({ page }) => {
		await stubList(page, [
			experiment('Exp verdict 0003', { actions: { start: refused('role_not_permitted') } })
		]);
		await page.goto('/experiments');

		const r = page.locator('tbody tr', { hasText: 'Exp verdict 0003' });
		await expect(r).toBeVisible({ timeout: 15_000 });
		await expect(startIn(page, 'Exp verdict 0003')).toHaveCount(0);
		await expect(r).not.toContainText('Refused:');
	});

	test('a row whose verdict has not arrived offers no Start (fail closed)', async ({ page }) => {
		await stubList(page, [experiment('Exp verdict 0004')]);
		await page.goto('/experiments');

		const r = page.locator('tbody tr', { hasText: 'Exp verdict 0004' });
		await expect(r).toBeVisible({ timeout: 15_000 });
		await expect(startIn(page, 'Exp verdict 0004')).toHaveCount(0);
	});
});

test.describe('/experiments — Stop / Conclude / Delete follow the server verdict', () => {
	test('a stopped draft holding assignments offers Start and Conclude, not Delete', async ({
		page
	}) => {
		// The old rule (`isAdmin && status === 'draft'`) offered Delete here; the
		// endpoint 409s it (`experiment_has_assignments`).
		await stubList(page, [
			experiment('Exp verdict 0005', {
				status: 'draft',
				assigned_count: 3,
				actions: {
					start: null,
					stop: refused('experiment_not_stoppable', { status: 'draft' }),
					conclude: null,
					delete: refused('experiment_has_assignments', { count: 3 })
				}
			})
		]);
		await page.goto('/experiments');

		const r = page.locator('tbody tr', { hasText: 'Exp verdict 0005' });
		await expect(r).toBeVisible({ timeout: 15_000 });
		await expect(actionIn(page, 'Exp verdict 0005', 'Conclude')).toBeVisible();
		await expect(actionIn(page, 'Exp verdict 0005', 'Delete')).toHaveCount(0);
		await expect(actionIn(page, 'Exp verdict 0005', 'Stop')).toHaveCount(0);
		await expect(r).not.toContainText('Refused:');
	});

	test('a running row offers Stop and Conclude by its verdicts', async ({ page }) => {
		await stubList(page, [
			experiment('Exp verdict 0006', {
				status: 'running',
				actions: {
					start: refused('experiment_not_startable', { status: 'running' }),
					stop: null,
					conclude: null,
					delete: refused('experiment_not_deletable', { status: 'running' })
				}
			})
		]);
		await page.goto('/experiments');

		await expect(actionIn(page, 'Exp verdict 0006', 'Stop')).toBeVisible({ timeout: 15_000 });
		await expect(actionIn(page, 'Exp verdict 0006', 'Conclude')).toBeVisible();
		await expect(actionIn(page, 'Exp verdict 0006', 'Delete')).toHaveCount(0);
	});

	test('a role refusal on every action leaves the row with no controls', async ({ page }) => {
		const no = refused('role_not_permitted');
		await stubList(page, [
			experiment('Exp verdict 0007', {
				status: 'running',
				actions: { start: no, stop: no, conclude: no, delete: no }
			})
		]);
		await page.goto('/experiments');

		const r = page.locator('tbody tr', { hasText: 'Exp verdict 0007' });
		await expect(r).toBeVisible({ timeout: 15_000 });
		for (const action of ['Start', 'Stop', 'Conclude', 'Delete']) {
			await expect(actionIn(page, 'Exp verdict 0007', action)).toHaveCount(0);
		}
	});
});
